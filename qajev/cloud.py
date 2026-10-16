"""QAJev Cloud: send a run to a cloud machine (cloud.qajev.com) instead of this Mac, with `--runner cloud|local|auto`.

Standard library only. The agent key comes from QAJEV_CLOUD_KEY, else macOS Keychain (service "qajev-cloud-agent"). It
is read in this process, goes out only as the Bearer header to the dashboard, and is never printed or given to a child
(`security` only prints it to us). The dashboard is QAJEV_CLOUD_URL, default https://cloud.qajev.com.

The rules (José, 11 Oct):
- local (the default): today's run on this Mac, unchanged.
- cloud: the run goes to the cloud or fails with the reason. It never falls back to this Mac. A run that cannot go
  (NotAllowed: a signed-in profile, a target on this Mac's 127.0.0.1 without --serve, a game) is refused.
- auto: cloud when allowed; a run that is NotAllowed runs on this Mac with one line saying why. A cloud failure
  (CloudError: no key, no answer, a refused upload) still fails: it never falls back either.
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from . import config

RUNNERS, MODELS = config.RUNNERS, config.CLOUD_MODELS
# QAJEV_RUNNER may also say `dashboard`: follow the Settings page's "Where runs go by default" (`qajev runner`).
RUNNER_SETTINGS = (*RUNNERS, "dashboard")
DASHBOARD_TTL_S = 60  # one look at the dashboard serves a minute of runs
_dashboard_cache: dict = {}
DEFAULT_URL = "https://cloud.qajev.com"
KEYCHAIN_SERVICE = "qajev-cloud-agent"
KINDS = ("check", "run", "smoke")  # websites; play (games, native and mobile apps) stays on this Mac for now
EXIT_CONFIG, EXIT_TROUBLE = 3, 4  # as cli.py: a setting to fix; the dashboard could not take or finish the run
POLL_S = 10  # seconds between two looks at a cloud run
WAIT_S = 2 * 3600  # how long a run is waited for (QAJEV_CLOUD_WAIT, seconds); the run itself goes on
# How long a run that waits for credits is followed before QAJev says so and returns (QAJEV_CLOUD_CREDIT_WAIT,
# seconds). The run stays queued: a top-up starts it, and QAJev Cloud ends it after 2 hours with no charge (the
# Orchestrator, 13 Oct).
CREDIT_WAIT_S = 60
DEFAULT_PORT = 8877
# A run takes at most 200 files of at most 50 MB, 500 MB in all (worker runs.ts). A bigger build ships as one tar in
# 45 MB parts (site/qcloud-archive.NNN.part), which the cloud runner joins and unpacks.
MAX_RUN_BYTES = 500 * 1024 * 1024
PACK_OVER_FILES = 150
PART_BYTES = 45 * 1024 * 1024
LIVE_GATES = ("queued", "running", None)
# Flags that mean something on this Mac only: left out of the arguments the cloud machine runs. The machine adds its
# own --headless --ephemeral and output folder; a test plan named with --plan is shipped and named again.
LOCAL_ONLY = {"runner", "serve", "model", "out", "env_file", "json", "events", "quiet", "background", "headless",
              "ephemeral", "load_high", "load_ok", "load_wait", "jev_provider", "plan"}


class CloudError(RuntimeError):
    """The cloud could not take or finish the run (or a cloud setting is wrong). Never a reason to run it here."""

    def __init__(self, message, code=EXIT_TROUBLE, run_id=None, url=None):
        super().__init__(message)
        self.code, self.run_id, self.url = code, run_id, url


class ApiError(CloudError):
    def __init__(self, status, message):
        super().__init__(f"the dashboard answered {status}: {message}")
        self.status = status


class NotAllowed(Exception):
    """This run cannot go to the cloud (yet): --runner cloud refuses it, --runner auto runs it on this Mac."""


# ---------------------------------------------------------------------------------------------------------------------
# Settings and the key


def setting(name, env_file=None):
    """-> (value, where): the shell's, else the first of QAJev's own env files that sets it (config.env_files)."""
    if os.environ.get(name):
        return os.environ[name], "the shell"
    for path in config.env_files(env_file):
        if path.is_file():
            value = config._read(path).get(name)
            if value:
                return value, str(path).replace(str(Path.home()), "~", 1)
    return None, None


def runner_for(args):
    """Where a run goes, and why: --runner, the project's `runner` (project runs), this Mac's mode (QAJEV_RUNNER: the
    shell, then ~/.qajev/.env; `dashboard` follows the dashboard's Settings page), else this Mac. -> (runner, source)"""
    if getattr(args, "runner", None):
        return args.runner, "--runner"
    if getattr(args, "command", None) in ("run", "smoke") and getattr(args, "project", None):
        from . import project as project_mod

        try:
            proj = project_mod.load(args.project)
        except project_mod.ProjectError:
            proj = None  # the run itself reports the broken project
        if proj and proj.runner:
            return proj.runner, f"runner in {proj.config_path}"
    value, where = setting("QAJEV_RUNNER", getattr(args, "env_file", None))
    if value:
        value = value.strip().lower()
        if value not in RUNNER_SETTINGS:
            raise CloudError(f"QAJEV_RUNNER={value!r} ({where}): use local, cloud, auto or dashboard", EXIT_CONFIG)
        if value == "dashboard":
            return dashboard_runner(getattr(args, "env_file", None))
        return value, f"QAJEV_RUNNER from {where}"
    return "local", "default"


def dashboard_url(env_file=None):
    return (setting("QAJEV_CLOUD_URL", env_file)[0] or DEFAULT_URL).rstrip("/")


def held_by(texts, env_file=None):
    """The QAJEV_CLOUD_HOLD word (comma-separated, any case) that a suite path, project or target holds on this Mac,
    or None. A held run goes to the cloud only when the command itself says --runner cloud."""
    words = [w.strip().lower() for w in (setting("QAJEV_CLOUD_HOLD", env_file)[0] or "").split(",") if w.strip()]
    return next((w for w in words for t in texts if w in t.lower()), None)


def dashboard_runner(env_file=None):
    """The dashboard's "Where runs go by default", for QAJEV_RUNNER=dashboard: one look a minute. A dashboard that
    cannot say keeps the run on this Mac, and the source says why. -> (runner, source)"""
    url = dashboard_url(env_file)
    hit = _dashboard_cache.get(url)
    if hit and time.monotonic() - hit[0] < DASHBOARD_TTL_S:
        return hit[1]
    key, _ = agent_key()
    if not key:
        found = ("local", "QAJEV_RUNNER=dashboard, but no QAJev Cloud key: this Mac")
    else:
        try:
            value = (Client(url, key, timeout=5).get("/api/v1/me") or {}).get("default_runner")
            found = ((value, f"the dashboard's default ({url})") if value in RUNNERS
                     else ("local", f"the dashboard ({url}) names no default runner: this Mac"))
        except CloudError as e:
            found = ("local", f"the dashboard did not answer ({e}): this Mac")
    _dashboard_cache[url] = (time.monotonic(), found)
    return found


def _security(args):
    """macOS `security`: the only child the key passes through, as its output to us (never as an argument)."""
    return subprocess.run(["security", *args], capture_output=True, text=True, timeout=10)


def agent_key():
    """-> (key, where) or (None, None)."""
    if os.environ.get("QAJEV_CLOUD_KEY"):
        return os.environ["QAJEV_CLOUD_KEY"], "QAJEV_CLOUD_KEY"
    try:
        out = _security(["find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"])
    except (OSError, subprocess.SubprocessError):
        return None, None
    key = out.stdout.strip() if out.returncode == 0 else ""
    return (key, f"Keychain ({KEYCHAIN_SERVICE})") if key else (None, None)


def key_found():
    """Where the key is, without reading its value (`qajev doctor`). -> a name, or None."""
    if os.environ.get("QAJEV_CLOUD_KEY"):
        return "QAJEV_CLOUD_KEY"
    try:
        out = _security(["find-generic-password", "-s", KEYCHAIN_SERVICE])
    except (OSError, subprocess.SubprocessError):
        return None
    return f"Keychain ({KEYCHAIN_SERVICE})" if out.returncode == 0 else None


NO_KEY = ("no QAJev Cloud agent key: set QAJEV_CLOUD_KEY, or add it to macOS Keychain as qajev-cloud-agent "
          "(`qajev doctor` checks it)")


# ---------------------------------------------------------------------------------------------------------------------
# The HTTP client (as qajev-cloud's qcloud/client.py)


class Client:
    def __init__(self, base_url, key, timeout=30):
        u = urllib.parse.urlsplit(base_url)
        if u.scheme != "https" and u.hostname not in ("127.0.0.1", "localhost"):
            # a key never travels in clear text, except to this machine (tests)
            raise CloudError(f"QAJEV_CLOUD_URL must be https:// (got {base_url})", EXIT_CONFIG)
        self.base = base_url.rstrip("/")
        self._key = key
        self.timeout = timeout

    def timeout_for(self, size):
        """A file upload gets its own time: the usual timeout, or 30 s plus 1 s per 256 KB (a 45 MB part: 210 s)."""
        return max(self.timeout, 30 + size // (256 * 1024))

    def _hide(self, text):
        return text.replace(self._key, "[redacted]") if self._key else text

    def redact(self, obj):
        """The dashboard's answer without the key in it, whatever it says: QAJev prints what it gets."""
        return config.redact_tree(obj, [self._key]) if self._key else obj

    def _call(self, method, path, body=None, raw=None, headers=None, timeout=None, as_bytes=False):
        import urllib.error  # noqa: PLC0415 (deferred: `qajev --help` imports this module for its choices)
        import urllib.request  # noqa: PLC0415

        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"User-Agent": "qajev-cloud/1"}
        if self._key:  # None only for the doctor's GET /api/v1/config
            h["Authorization"] = f"Bearer {self._key}"
        if raw is None and body is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                payload = r.read()
                if r.status == 204 or not payload:
                    return None
                if as_bytes:
                    return payload
                return json.loads(payload) if "json" in (r.headers.get("Content-Type") or "") else payload
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error") or e.reason
            except (ValueError, AttributeError):
                msg = e.reason
            raise ApiError(e.code, self._hide(str(msg))) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            reason = getattr(e, "reason", None) or e
            raise CloudError(f"the dashboard at {self.base} does not answer: {self._hide(str(reason))}") from None

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, body=None):
        return self._call("POST", path, body=body if body is not None else {})

    def put_file(self, run_id, remote_path, data):
        if remote_path.startswith("/") or ".." in remote_path.split("/"):
            raise CloudError(f"unsafe file path: {remote_path}", EXIT_CONFIG)
        quoted = urllib.parse.quote(remote_path)
        return self._call("PUT", f"/api/v1/runs/{urllib.parse.quote(run_id)}/files/{quoted}", raw=data,
                          headers={"x-content-sha256": hashlib.sha256(data).hexdigest(),
                                   "x-content-size": str(len(data)),
                                   "Content-Type": "application/octet-stream"},
                          timeout=self.timeout_for(len(data)))


def connect(env_file=None):
    """A client with the agent key. Raises CloudError (exit 3) when there is no key."""
    key, _ = agent_key()
    if not key:
        raise CloudError(NO_KEY, EXIT_CONFIG)
    return Client(dashboard_url(env_file), key)


def run_url(base, run_id):
    return f"{base}/run.html?id={urllib.parse.quote(run_id)}"


# ---------------------------------------------------------------------------------------------------------------------
# What goes to the cloud


@dataclass
class Plan:
    kind: str
    args: list  # the QAJev arguments the cloud machine runs, starting with the command
    name: str
    files: list = field(default_factory=list)  # [(remote path, local Path)]
    serve_root: Path | None = None
    port: int | None = None
    tunnel_port: int | None = None  # this Mac's dev server port, reached through the dispatcher's read-only tunnel
    tunnel: dict | None = None  # the full tunnel: ports with their commands, root, data, writes, test_keys
    inline: list = field(default_factory=list)  # [(remote path, bytes)]: a suite copy and its seed fixtures
    project: str | None = None  # the project's name, sent when the dashboard knows it
    model: str | None = None
    sees_screen: bool | None = None
    warnings: list = field(default_factory=list)


def _subparser(command):
    from .cli import build_parser

    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return sub.choices[command]


def _action(sub, name):
    """The option a token names, as argparse reads it (an unambiguous --prefix too)."""
    found = sub._option_string_actions.get(name)
    if found is None and name.startswith("--"):
        hits = {id(a): a for opt, a in sub._option_string_actions.items() if opt.startswith(name)}
        found = next(iter(hits.values())) if len(hits) == 1 else None
    return found


def split_args(command, argv):
    """argv (without the command) as [(dest or None, tokens)]: each option with its value, each positional alone."""
    sub, out, i = _subparser(command), [], 0
    while i < len(argv):
        token = argv[i]
        action = _action(sub, token.partition("=")[0]) if token.startswith("-") and len(token) > 1 else None
        if action is None:
            out.append((None, [token]))
            i += 1
            continue
        n = 0 if action.nargs == 0 or "=" in token else 1
        out.append((action.dest, argv[i:i + 1 + n]))
        i += 1 + n
    return out


# Files a served build never ships (Orchestrator, 11 Oct): env files, keys, credentials and a git folder.
SECRET_NAMES = re.compile(r"^(\.env(\..+)?|\.dev\.vars(\..+)?|\.npmrc|\.netrc|\.pypirc|"
                          r"id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|.*\.(pem|key|p12|pfx|jks|keystore)|"
                          r".*service-account.*\.json|credentials.*\.json)$", re.I)


def secrets_in(root):
    """The paths under a served folder that look like secrets, relative to it (a .git folder counts once)."""
    found = []
    for f in sorted(root.rglob("*")):
        rel = f.relative_to(root)
        if ".git" in rel.parts:
            if rel.parts[-1] == ".git":
                found.append(rel.as_posix() + "/")
            continue
        if f.is_file() and SECRET_NAMES.match(f.name):
            found.append(rel.as_posix())
    return found


def _serve_spec(serve, local_targets):
    """--serve DIR[:PORT] -> (folder, port). The port defaults to the port of the target on this Mac, else 8877."""
    folder, port = serve, None
    head, sep, tail = serve.rpartition(":")
    if sep and tail.isdigit():
        folder, port = head, int(tail)
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise CloudError(f"--serve {folder}: no such folder", EXIT_CONFIG)
    found = secrets_in(root)
    if found:
        more = f" and {len(found) - 5} more" if len(found) > 5 else ""
        raise CloudError(f"--serve {folder}: it holds {', '.join(found[:5])}{more}; a served folder goes to a cloud "
                         "machine, so it must hold no secrets: ship a clean build (git archive), not a working copy",
                         EXIT_CONFIG)
    ports = {urllib.parse.urlsplit(t).port for t in local_targets}
    if port is None:
        only = next(iter(ports)) if len(ports) == 1 else None
        port = only if only and only >= 1024 else DEFAULT_PORT
    if not 1024 <= port <= 65535:
        raise CloudError(f"--serve {serve}: the port must be from 1024 to 65535", EXIT_CONFIG)
    wrong = [t for t in local_targets if (urllib.parse.urlsplit(t).port or 80) != port]
    if wrong:
        raise CloudError(f"--serve serves the build on the cloud machine's 127.0.0.1:{port}, but the run opens "
                         f"{wrong[0]}: use port {port} there, or give --serve DIR:PORT", EXIT_CONFIG)
    return root.resolve(), port


def _tunnel_port(local_targets, writes):
    """The one dev server port on this Mac a cloud run reaches through the dispatcher's read-only tunnel (QAJev Cloud,
    11 Oct). Raises NotAllowed for what the tunnel cannot carry: the run stays on this Mac."""
    if writes:
        raise NotAllowed("the run writes to this Mac's dev server, and the tunnel to this Mac is read-only")
    hosts = {(urllib.parse.urlsplit(t).hostname or "").lower() for t in local_targets}
    if hosts - {"127.0.0.1", "localhost"}:
        raise NotAllowed("the tunnel reaches this Mac's 127.0.0.1 or localhost only, not "
                         f"{sorted(hosts - {'127.0.0.1', 'localhost'})[0]}")
    ports = {urllib.parse.urlsplit(t).port or 80 for t in local_targets}
    if len(ports) > 1:
        raise NotAllowed(f"the run opens {len(ports)} ports on this Mac; the tunnel reaches one dev server per run")
    port = ports.pop()
    if not 1024 <= port <= 65535:
        raise NotAllowed(f"port {port}: the tunnel takes a dev server port from 1024 to 65535")
    return port


def full_tunnel(spec, local_targets, writes, root, source):
    """The full tunnel (QAJev Cloud, 12 Oct): a run on this machine's dev servers, several ports, writes with local
    data, WebSockets. The suite's (or project env's) `tunnel:` names each port and its command; the dispatcher on this
    machine checks that exactly that process answers before anything starts. `source` ({file, env?}) names the file on
    this machine that declares it: the dispatcher reads it again (`qajev tunnel`) and refuses a run whose tunnel
    differs. -> the run's tunnel. Raises NotAllowed."""
    hosts = {(urllib.parse.urlsplit(t).hostname or "").lower() for t in local_targets}
    if hosts - {"127.0.0.1", "localhost"}:
        raise NotAllowed("the tunnel reaches this machine's 127.0.0.1 or localhost only, not "
                         f"{sorted(hosts - {'127.0.0.1', 'localhost'})[0]}")
    named = {int(p) for p in spec["ports"]}
    needed = {urllib.parse.urlsplit(t).port or 80 for t in local_targets}
    missing = sorted(needed - named)
    if missing:
        raise NotAllowed(f"the run uses port {missing[0]} on this machine, and the suite's tunnel.ports does not "
                         "name it")
    if writes and spec.get("data") != "local":
        raise NotAllowed("the run writes to this machine's dev servers: the suite's tunnel needs data: local "
                         "(fixture accounts only, no production data)")
    if root is None or not Path(root).is_dir():
        raise NotAllowed("the tunnel needs the project's folder: run the suite from its git checkout (or set the "
                         "project's repo)")
    return {**spec, "root": str(Path(root).resolve()), "source": source, **({"writes": True} if writes else {})}


SEED_REF = re.compile(r"seed:([^\s\"'#,\]\}]+)#([A-Za-z0-9_.-]+)")


def seed_remote(file):
    """Where a seed fixture goes in a cloud run: in/seed/<hash of its absolute path on this Mac>.json. The cloud
    runner reads it there for that path (qcloud.runner), so the suite needs no other change."""
    return f"in/seed/{hashlib.sha256(os.path.abspath(file).encode()).hexdigest()[:16]}.json"


def seeded_account(account, tunnel, targets, source, folder):
    """A cloud run that signs in with a seeded TEST account (José via the Orchestrator, 12 Oct): only through the full
    tunnel with data: local, to this Mac's dev servers; only seed:FILE#KEY from a fixture that says "test_account":
    true; and only the values this run's account uses. -> [(remote, bytes)]: the source file with each seed reference
    made absolute, and one small fixture per file. Raises NotAllowed (never with a value in the message)."""
    from . import vault

    if not tunnel or tunnel.get("data") != "local":
        raise NotAllowed("a signed-in cloud run needs tunnel: with data: local (a seeded test account on this Mac's "
                         "dev servers); any other signed-in run stays on this Mac")
    login = urllib.parse.urljoin(targets[0] if targets else "", account["login"]["url"])  # a project's is relative
    away = [t for t in [*targets, login] if t and not config.is_loopback(t)]
    if away:
        raise NotAllowed(f"a signed-in cloud run reaches only this Mac's 127.0.0.1 or localhost, not "
                         f"{urllib.parse.urlsplit(away[0]).hostname}")
    refs = [("email", account.get("email")), ("password", account.get("password")), ("totp", account.get("totp")),
            ("cookie.value", (account.get("cookie") or {}).get("value"))]
    needed = {}
    for what, ref in refs:
        if ref is None or (what == "email" and not vault.is_ref(ref)):
            continue
        try:
            kind, *rest = vault.parse(ref)
        except vault.VaultError as e:
            raise NotAllowed(f"account.{what}: {e}") from None
        if kind != "seed":
            raise NotAllowed(f"account.{what}: a cloud run takes only seed:FILE#KEY from a test fixture, never a "
                             f"{kind}: reference")
        needed.setdefault(os.path.abspath(rest[0]), set()).add(rest[1])
    out = []
    for file, keys in needed.items():
        try:
            data = vault.seed(file)
        except vault.VaultError as e:
            raise NotAllowed(str(e)) from None
        if not data.get("allowed_hosts"):
            raise NotAllowed(f"seed:{file}: the fixture must list its allowed_hosts")
        values = {}
        for key in sorted(keys):
            value = vault.seed_value(data, key)
            if not isinstance(value, str) or not value:
                raise NotAllowed(f"seed:{file}: the fixture has no text value for {key!r}")
            values[key] = value
        fixture = {"test_account": True, "allowed_hosts": data["allowed_hosts"], **values}
        out.append((seed_remote(file), json.dumps(fixture).encode()))

    def absolute(m):
        file = m.group(1) if os.path.isabs(m.group(1)) else os.path.join(str(folder), m.group(1))
        return f"seed:{os.path.abspath(file)}#{m.group(2)}"

    return [(source[0], SEED_REF.sub(absolute, Path(source[1]).read_text(encoding="utf-8")).encode()), *out]


def plan(args):
    """What goes to the cloud for this parsed run (args.argv: its arguments, with the command first). Nothing is sent.
    Raises NotAllowed when the run must stay on this Mac, CloudError when a setting is wrong."""
    from . import planfile

    cmd = args.command
    if cmd not in KINDS:
        raise NotAllowed(f"the cloud does not take `{cmd}` runs yet (games, native and mobile apps stay on this Mac)")
    parts = split_args(cmd, list(args.argv[1:]))
    given = {dest for dest, _ in parts if dest}
    if "profile" in given or os.environ.get("QAJEV_PROFILE"):
        raise NotAllowed("signed-in runs stay on this Mac (--profile)")
    if "cdp_url" in given:
        raise NotAllowed("the run attaches to a Chrome on this Mac (--cdp-url)")
    if getattr(args, "real_devices", None):
        raise NotAllowed("real device browsers (--real-devices) run on this Mac's simulators")
    if getattr(args, "allow_commands", False):
        raise NotAllowed("a suite's shell commands (--allow-commands) run on this Mac only")
    files, targets, account, named_plan, plan_base, project_name, swap = [], [], None, None, Path.cwd(), None, {}
    owner = None  # a suite's project: filed with it on the dashboard, but never held by its name
    seed_base = None
    full, root, source, writes = None, None, None, getattr(args, "mode", None) == "mutate"
    if getattr(args, "project", None):
        from urllib.parse import urljoin

        from . import project as project_mod

        try:
            proj = project_mod.load(args.project)
        except project_mod.ProjectError as e:
            raise CloudError(str(e), EXIT_CONFIG) from None
        env = args.env or proj.default_env
        if env not in proj.envs:
            raise CloudError(f"{proj.name}: no env {env!r}; have {sorted(proj.envs)}", EXIT_CONFIG)
        target = proj.envs[env]
        targets = [args.url] if cmd == "smoke" and args.url else [
            urljoin(target["base_url"], target.get("smoke_start", "/")) if cmd == "smoke" else target["base_url"]]
        account = target.get("account") or proj.account
        if account:  # the account block as a suite gets it (seed references anchored to the project)
            try:
                account = project_mod.sign_in_account(proj, account)
            except project_mod.ProjectError as e:
                raise CloudError(str(e), EXIT_CONFIG) from None
            seed_base = proj.repo or proj.config_path.parent
        full = target.get("tunnel")
        root = proj.repo.expanduser() if proj.repo is not None else None
        source = {"file": str(proj.config_path.resolve()), "env": env}
        writes = writes or target.get("mode") == "mutate"
        remote = f"in/{proj.config_path.name}"
        files.append((remote, proj.config_path.resolve()))
        swap["project"] = remote
        project_name, plan_base = proj.name, proj.config_path.parent
    elif cmd == "run":
        from . import suite as suite_mod

        if not args.suite:
            raise CloudError("give a suite file or --project", EXIT_CONFIG)
        try:
            suite = suite_mod.load(args.suite)
        except (suite_mod.SuiteError, ValueError, OSError) as e:
            raise CloudError(f"{args.suite}: {e}", EXIT_CONFIG) from None
        targets = [f"http://{h}" for h in suite.hosts]
        account = suite.account
        seed_base = Path(args.suite).parent
        full = suite.tunnel
        root = planfile.project_root(args.suite.resolve().parent)
        source = {"file": str(args.suite.resolve())}
        writes = writes or suite.mutates
        remote = f"in/{args.suite.name}"
        files.append((remote, args.suite.resolve()))
        swap["suite"] = remote
        named_plan, plan_base = suite.plan, args.suite.resolve().parent
        from . import project as project_mod

        owner = project_mod.owner_of(args.suite)  # the dashboard files the run under it (José, 13 Oct)
    else:
        targets = [args.url]
        if not args.url:
            raise CloudError("give a URL or --project", EXIT_CONFIG)
    if not project_name and not owner:  # every cloud run names its project when one is known (the Orchestrator, 13 Oct)
        from . import project as project_mod

        # after --project and the suite's own: the folder the run starts from, then the one project on the URL's site
        owner = project_mod.owner_of(Path.cwd()) or next(
            (name for name in map(project_mod.owner_of_url, [t for t in targets if t]) if name), None)
    if account and not full:
        raise NotAllowed("signed-in runs stay on this Mac (the run signs in with an account; a seeded test account "
                         "goes to the cloud through tunnel: with data: local)")
    writes = writes or bool(account)  # signing in posts a form to this Mac's dev server
    named = getattr(args, "runner_named", False)
    held = held_by([str(p) for _, p in files] + [t for t in targets if t] + [project_name or ""])
    if held and not named:
        raise NotAllowed(f"held on this Mac: it matches '{held}' in QAJEV_CLOUD_HOLD; a held suite goes to the cloud "
                         "only when the command says --runner cloud (with its owner's OK)")
    local_targets = [t for t in targets if t and config.is_loopback(t)]
    serve = getattr(args, "serve", None)
    tunnel_port, tunnel = None, None
    inline = []
    if local_targets and not serve and full:
        tunnel = full_tunnel(full, local_targets, writes, root, source)
        if account:
            inline = seeded_account(account, tunnel, targets, files[0], seed_base)
            files = files[1:]  # the source goes as its copy, with each seed reference made absolute
    elif account:
        raise NotAllowed("a signed-in cloud run needs tunnel: to this Mac's dev servers, not --serve or a public site")
    elif local_targets and not serve and not named:
        raise NotAllowed("a run on this Mac's dev server goes to the cloud only with --runner cloud on the command: "
                         "the tunnel passes GET, HEAD and OPTIONS, so a page's POST or WebSocket would fail there")
    elif local_targets and not serve:
        tunnel_port = _tunnel_port(local_targets, writes)
    serve_root, port = _serve_spec(serve, local_targets) if serve else (None, None)
    warnings = []
    plan_path = args.plan.expanduser() if getattr(args, "plan", None) else (
        planfile.resolve(named_plan, plan_base) if named_plan else None)
    status = planfile.check(plan_path)
    if not status["approved"]:
        where = f" ({status['path']})" if status["path"] else ""
        if planfile.required():
            raise CloudError(f"needs an approved test plan: {status['problem']}{where}", EXIT_CONFIG)
        warnings.append(f"no approved test plan: {status['problem']}{where}")
    forwarded, positional = [cmd], False
    for dest, tokens in parts:
        if dest in LOCAL_ONLY:
            continue
        if dest is None and cmd == "run" and "suite" in swap and not positional and not tokens[0].startswith("-"):
            positional = True
            forwarded.append(swap["suite"])
        elif dest == "project":
            forwarded += [tokens[0].partition("=")[0], swap["project"]]
        else:
            forwarded += tokens
    if plan_path and Path(plan_path).is_file():
        remote = f"in/{Path(plan_path).name}"
        files.append((remote, Path(plan_path).resolve()))
        forwarded += ["--plan", remote]
    from .jobs import describe_argv

    return Plan(kind=cmd, args=forwarded, name=describe_argv(args.argv)[:160], files=files, serve_root=serve_root,
                port=port, tunnel_port=tunnel_port, tunnel=tunnel, inline=inline, project=project_name or owner,
                model=getattr(args, "model", None), sees_screen=True if getattr(args, "vision", None) else None,
                warnings=warnings)


def site_files(root, scratch):
    """A served folder's files as [(remote, local)]: as they are, or packed into tar parts when there are many."""
    site = [f for f in sorted(root.rglob("*")) if f.is_file() and not f.is_symlink()
            and not any(part.startswith(".") for part in f.relative_to(root).parts)]
    if len(site) > PACK_OVER_FILES or any(f.stat().st_size > PART_BYTES for f in site):
        return [(f"site/{p.name}", p) for p in pack_parts(root, site, scratch)]
    return [(f"site/{f.relative_to(root).as_posix()}", f) for f in site]


def pack_parts(root, site, scratch):
    """The served files as one uncompressed tar, cut into PART_BYTES parts. -> the part Paths, in order."""
    import tarfile  # noqa: PLC0415

    whole = scratch / "qcloud-archive.tar"
    with tarfile.open(whole, "w", format=tarfile.PAX_FORMAT) as tar:
        for f in site:
            tar.add(f, arcname=f.relative_to(root).as_posix(), recursive=False)
    parts = []
    with whole.open("rb") as fh:
        while chunk := fh.read(PART_BYTES):
            part = scratch / f"qcloud-archive.{len(parts):03d}.part"
            part.write_bytes(chunk)
            parts.append(part)
    whole.unlink()
    return parts


def _sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _known_project(client, name):
    """The project's name when the dashboard has it (synced from project.toml), so the run is filed with it."""
    try:
        return name if client.get(f"/api/v1/projects/{urllib.parse.quote(name)}") else None
    except CloudError:
        return None


def submit(client, plan):
    """Queue the run and upload its files. -> the run id. An upload that fails stops the run (it would sit queued for
    ever) and raises CloudError with the run's id and address."""
    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="qajev-cloud-") as scratch:
        files = list(plan.files) + (site_files(plan.serve_root, Path(scratch)) if plan.serve_root else [])
        for i, (remote, data) in enumerate(plan.inline):  # written only into this scratch folder, removed after
            local = Path(scratch) / f"inline-{i}"
            local.write_bytes(data)
            local.chmod(0o600)
            files.insert(i, (remote, local))
        names = [r for r, _ in files]
        if len(set(names)) != len(names):
            raise CloudError("two files would have the same name on the cloud machine; rename one", EXIT_CONFIG)
        total = sum(p.stat().st_size for _, p in files)
        if total > MAX_RUN_BYTES:
            raise CloudError(f"the files are {total // 2**20} MB; a cloud run takes at most "
                             f"{MAX_RUN_BYTES // 2**20} MB", EXIT_CONFIG)
        # Each file is read again at upload time, one at a time: a big build never sits in memory whole.
        declared = [{"path": remote, "sha256": _sha256(local), "size": local.stat().st_size} for remote, local in files]
        body = {"kind": plan.kind, "args": plan.args, "runner": "cloud", "surface": "web", "files": declared,
                "name": plan.name}
        project = _known_project(client, plan.project) if plan.project else None
        for key, value in (("model", plan.model), ("sees_screen", plan.sees_screen), ("project", project),
                           ("serve", {"path": "site", "port": plan.port} if plan.serve_root else None),
                           ("tunnel", plan.tunnel or ({"port": plan.tunnel_port} if plan.tunnel_port else None))):
            if value is not None:
                body[key] = value
        created = client.post("/api/v1/runs", body) or {}
        run = created.get("run") if isinstance(created, dict) else None
        run_id = run.get("id") if isinstance(run, dict) else run
        if not run_id:
            raise CloudError("the dashboard did not return a run id")
        remote = None
        try:
            for remote, local in files:
                client.put_file(run_id, remote, local.read_bytes())
        except BaseException as e:
            with contextlib.suppress(CloudError):
                client.post(f"/api/v1/runs/{urllib.parse.quote(run_id)}/stop")
            if isinstance(e, (CloudError, OSError)):
                raise CloudError(f"the upload of {remote} failed, so the run was stopped: {e}", EXIT_TROUBLE,
                                 run_id, run_url(client.base, run_id)) from None
            raise
        return run_id


# ---------------------------------------------------------------------------------------------------------------------
# Following a cloud run


RUN_ID = re.compile(r"^r_[a-z0-9]{4,40}$")  # r_ and 16 characters today


def get_file(client, run_id, path):
    """One file of a cloud run, as its bytes (a .json one is not parsed)."""
    quoted = "/".join(urllib.parse.quote(p) for p in path.split("/"))
    return client._call("GET", f"/api/v1/runs/{urllib.parse.quote(run_id)}/files/{quoted}", as_bytes=True) or b""


def _plain_relative(path):
    """A path a run's report may name for a file of that run: relative, no "..", no backslash."""
    return isinstance(path, str) and path and not path.startswith("/") and "\\" not in path and \
        ".." not in path.split("/")


def fetch_report(client, run_id, folder):
    """A cloud run's report.json, report.md, report.html and the screenshots its report names, kept in `folder` (12-13
    Oct: an agent asked for Jev's steps and the last screen, and the report link asked it for a key). A named path that
    would leave the folder is skipped. -> (the report, the number of screenshots kept). Raises CloudError."""
    folder.mkdir(parents=True, exist_ok=True)
    raw = get_file(client, run_id, "report.json")
    data = json.loads(raw)
    (folder / "report.json").write_bytes(raw)
    for name in ("report.md", "report.html"):
        try:
            (folder / name).write_bytes(get_file(client, run_id, name))
        except ApiError as e:
            if e.status != 404:
                raise
    from .artifacts import PNG_MAGIC, manifest_path

    named, attachments = set(), {}
    for s in data.get("scenarios") or []:
        for path in [s.get("shot"), *[(r or {}).get("path") for r in s.get("react") or [] if isinstance(r, dict)]]:
            if _plain_relative(path):
                named.add(path)
        for entry in s.get("artifacts") or []:
            path = manifest_path(entry)
            if path is None:
                raise CloudError("the report contains an invalid artifact manifest; download is incomplete")
            if path in attachments and attachments[path] != entry:
                raise CloudError("the report contains conflicting artifact manifests; download is incomplete")
            attachments[path] = entry
            named.add(path)
    kept, root = 0, folder.resolve()
    for path in sorted(named):
        if path in attachments:
            target = root
            for part in path.split("/"):
                target /= part
                if target.is_symlink():
                    raise CloudError("artifact download refuses symbolic links")
        dest = (folder / path).resolve()
        if root not in dest.parents:
            continue
        try:
            body = get_file(client, run_id, path)
        except ApiError as e:
            if e.status == 404 and path not in attachments:
                continue
            raise
        if path in attachments:
            entry = attachments[path]
            if len(body) != entry["bytes"] or hashlib.sha256(body).hexdigest() != entry["sha256"]:
                raise CloudError("artifact bytes do not match the report manifest; download is incomplete")
            if entry["kind"] == "png" and not body.startswith(PNG_MAGIC):
                raise CloudError("the PNG artifact has a different image kind; download is incomplete")
            if entry["kind"] == "json":
                try:
                    json.loads(body)
                except (ValueError, UnicodeError):
                    raise CloudError("the JSON artifact is not valid JSON; download is incomplete") from None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        kept += path not in attachments or attachments[path]["kind"] == "png"
    return data, kept


def status(client, run_id):
    return client.redact(client.get(f"/api/v1/runs/{urllib.parse.quote(run_id)}") or {})


def stop(client, run_id):
    return client.post(f"/api/v1/runs/{urllib.parse.quote(run_id)}/stop") or {}


def done(run):
    return run.get("gate") not in LIVE_GATES


def wait_seconds(name="QAJEV_CLOUD_WAIT", default=WAIT_S):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def credit_wait_message(run_id, text):
    return (f"the run has not started: {text.rstrip()} It stays queued and starts after a top-up. After 2 hours "
            f"without one, QAJev Cloud ends it, with no charge. Run {run_id}. To stop it now: qajev stop {run_id}")


def wait(client, run_id, on_change=None, every=None, timeout=None, sleep=time.sleep, now=time.monotonic):
    """Look at the run until it ends. on_change(run) is called when its gate, progress, test or the reason it waits
    (QAJev Cloud's "waiting": credits, a cap, machines off) changes. Raises
    CloudError when the dashboard stops answering or the wait runs out (the run itself goes on in the cloud)."""
    every = POLL_S if every is None else every
    timeout = wait_seconds() if timeout is None else timeout
    credit_wait = wait_seconds("QAJEV_CLOUD_CREDIT_WAIT", CREDIT_WAIT_S)
    url, deadline, last, misses, credits_since = run_url(client.base, run_id), now() + timeout, None, 0, None
    while True:
        try:
            run = status(client, run_id)
            misses = 0
        except CloudError as e:
            misses += 1
            if isinstance(e, ApiError) and e.status in (401, 403, 404) or misses >= 3:
                raise CloudError(str(e), EXIT_TROUBLE, run_id, url) from None
            sleep(every)
            continue
        waiting = run.get("waiting") or {}
        seen = (run.get("gate"), run.get("progress"), run.get("now_test"), waiting.get("reason"), waiting.get("text"))
        if on_change and seen != last:
            on_change(run)
        last = seen
        if done(run):
            return run
        if waiting.get("reason") == "credits":  # not in silence for hours: say so, and leave the run queued
            credits_since = now() if credits_since is None else credits_since
            if now() - credits_since >= credit_wait:
                raise CloudError(credit_wait_message(run_id, waiting.get("text") or "it waits for credits."),
                                 EXIT_TROUBLE, run_id, url)
        else:
            credits_since = None
        if now() >= deadline:
            raise CloudError(f"waited {timeout / 60:.0f} min and the run has not finished; it goes on in the cloud: "
                             f"{url}", EXIT_TROUBLE, run_id, url)
        sleep(every)


def result(base, run):
    """One cloud run as QAJev's JSON: {runner, run_id, url, gate, counts, tests (with checks), report_url, reason},
    plus waiting
    ({reason, text}) while a queued run cannot start."""
    run_id = run.get("id") or ""
    tests = []
    for t in run.get("tests") or []:
        wrong = [str(w) for w in t.get("wrong") or []]
        # checks: {check, ok, value?} as a local run's JSON gives them, so a gate reads both the same way
        tests.append({"name": t.get("name"), "outcome": t.get("outcome"), "about": t.get("about"),
                      "reason": t.get("reason") or "; ".join(wrong) or None, "wrong": wrong,
                      "checks": [c for c in t.get("checks") or [] if isinstance(c, dict)]})
    report = (run.get("files") or {}).get("report_html")
    return {"runner": "cloud", "run_id": run_id, "url": run_url(base, run_id) if run_id else None,
            "gate": str(run.get("gate") or "").upper() or None, "counts": run.get("counts") or {}, "tests": tests,
            "report_url": (base + report if str(report).startswith("/") else report) if report else None,
            "reason": run.get("reason"), **({"waiting": run["waiting"]} if run.get("waiting") else {})}


def exit_code(gate):
    """As a run on this Mac: 0 only on PASS, 1 on FAIL, 2 for any other end."""
    return {"PASS": 0, "FAIL": 1}.get(gate, 2)


# ---------------------------------------------------------------------------------------------------------------------
# qajev doctor


def doctor(env_file=None, offline=False):
    """The doctor's cloud line: the key (found or not, never its value), the default runner, and whether the
    dashboard answers (GET /api/v1/config, no key). -> (ok, detail)"""
    from types import SimpleNamespace

    where = key_found()
    try:
        runner, source = runner_for(SimpleNamespace(runner=None, env_file=env_file, command="doctor"))
    except CloudError as e:
        runner, source = None, str(e)
    url = dashboard_url(env_file)
    answers = None
    if not offline:
        try:
            Client(url, None).get("/api/v1/config")
            answers = True
        except CloudError as e:
            answers = str(e)
    said = "not checked (--offline)" if answers is None else "answers" if answers is True else answers
    key = f"found in {where}" if where else f"not found: set QAJEV_CLOUD_KEY or add Keychain item {KEYCHAIN_SERVICE}"
    detail = f"key {key}; default runner {runner or 'unknown'} ({source}); {url} {said}"
    # Informational while runs stay here; with cloud or auto the default, a missing key or a silent dashboard is a fault
    ok = runner == "local" or (runner in RUNNERS and bool(where) and answers in (None, True))
    return ok, detail
