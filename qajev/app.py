"""App: QA any native app that carries a QAJev bridge, started from a play suite's `app:`.

The app speaks the Godot bridge's protocol (bridges/godot/qajev_bridge.gd): one JSON line per request on
127.0.0.1:$QAJEV_BRIDGE_PORT, with observe, act, shot and quit. So everything after the launch is the Godot driver's:
Jev's choices, the checks on the app's own state, shots, relaunch. Only how the app starts differs: the suite names
its command, and QAJev gives it a throwaway home folder (Twinbox Desktop, 13 Oct).

    app:
      command: ["target/debug/twinbox-desktop"]   # relative to the project folder, or absolute
      env: {TWINBOX_API: "http://127.0.0.1:8790"}
      home_env: TWINBOX_DESKTOP_HOME               # also named the throwaway folder, for an app that reads its own
      start_wait: 60                               # seconds until the bridge must answer
      bridge_key: true                             # a new QAJEV_BRIDGE_KEY each launch, sent as the first line
      test_domains: [twinbox.invalid]              # the test accounts' email domains, for files:
      files:                                       # written into home_env's folder before the app starts
        .twinbox/token@staging.example: seed:fixtures/staging.json#token

`files` is a narrow exception (the Orchestrator, 14 Oct): it hands a seeded TEST account's token to the app under test,
and never lets QAJev's browser sign in anywhere. Each value comes only from a seed fixture that says "test_account":
true and names its `account`, an email in one of `test_domains`. The file is written 0600 inside the run's throwaway
home and goes with it. The value is redacted from the report, the logs and the JSON. The run's approved test plan must
name app.files.
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import chrome, config, vault
from .config import HOME, is_test_email, remember_secret
from .lowprio import niced
from .native import STATE, GodotGame, NativeError, free_port, save_isolation

APP_KEYS = {"command", "env", "home_env", "start_wait", "bridge_key", "files", "test_domains"}
# A file's place in the app's home: names of these characters joined by /, never absolute, never . or .. (_inside).
FILE_NAME = re.compile(r"[A-Za-z0-9._@+-]+")
DOMAIN = re.compile(r"(?:[a-z0-9-]+\.)+[a-z0-9-]+")
HOME_PREFIX = re.compile(r"qajev-app-(\d+)-")
# An app's own log: a line logged at ERROR or FATAL (Rust's tracing and log, most loggers), and a Rust panic. Not a
# line that only says "error" in its text.
APP_ERROR = re.compile(r"(^|\s)(ERROR|FATAL)(\s|:|$)|\bpanicked at\b")
NOTHING = re.compile(r"(?!)")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# What the app gets from QAJev's own environment: these names only, never the rest. QAJev's environment holds the
# agent's keys (AWS, Stripe, model keys, an app's own token): none of it may reach the app (redteam on #138, 13 Oct).
PASSED = {"PATH", "LANG", "TERM", "TMPDIR", "USER", "LOGNAME", "SHELL",
          "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY"}  # the last 4: a Linux app's display
PASSED_PREFIX = ("LC_",)
# Names only QAJev sets: the throwaway home and the bridge. A suite cannot point the app back at a real folder.
RESERVED = re.compile(r"HOME|XDG_[A-Z_]+_HOME|QAJEV_[A-Z0-9_]*")


def passed_env(environ):
    """The part of `environ` an app may see: the names in PASSED, and LC_*."""
    return {k: v for k, v in environ.items() if k in PASSED or k.startswith(PASSED_PREFIX)}


def reserved(env, where):
    taken = sorted(k for k in env if RESERVED.fullmatch(str(k)))
    if taken:
        raise NativeError(f"{where}: QAJev sets {', '.join(taken)} itself (the throwaway home and the bridge); "
                          "do not set it")


def app_spec(value):
    """A suite's `app:` checked, or NativeError naming what is wrong."""
    if not isinstance(value, dict):
        raise NativeError("app: must be a mapping with command: (and env:, home_env:, start_wait:)")
    unknown = sorted(set(value) - APP_KEYS)
    if unknown:
        raise NativeError(f"app: unknown key(s) {unknown}; known: {sorted(APP_KEYS)}")
    command = value.get("command")
    if isinstance(command, str):
        command = [command]
    if not isinstance(command, list) or not command or not all(isinstance(c, (str, int, float)) for c in command):
        raise NativeError("app.command: the program and its arguments, as a list of strings")
    env = value.get("env") or {}
    if not isinstance(env, dict) or not all(ENV_NAME.fullmatch(str(k)) for k in env):
        raise NativeError("app.env: a mapping of environment variable names to values")
    reserved(env, "app.env")
    home_env = value.get("home_env")
    if home_env is not None and not ENV_NAME.fullmatch(str(home_env)):
        raise NativeError(f"app.home_env: {home_env!r} is not an environment variable name")
    if home_env and str(home_env) in env:
        raise NativeError(f"app.home_env: QAJev sets {home_env} to the throwaway folder; do not also set it in app.env")
    try:
        start_wait = float(value.get("start_wait", 60))
    except (TypeError, ValueError):
        start_wait = 0
    if not 1 <= start_wait <= 600:
        raise NativeError("app.start_wait: seconds, from 1 to 600")
    bridge_key = value.get("bridge_key", False)
    if not isinstance(bridge_key, bool):
        raise NativeError("app.bridge_key: true or false")
    files, domains = _files_spec(value, home_env)
    return {"command": [str(c) for c in command], "env": {str(k): str(v) for k, v in env.items()},
            "home_env": str(home_env) if home_env else None, "start_wait": start_wait, "bridge_key": bridge_key,
            "files": files, "test_domains": domains}


def _files_spec(value, home_env):
    """app.files and app.test_domains checked for their form; the fixtures are read by app_files."""
    files, domains = value.get("files") or {}, value.get("test_domains")
    if not files:
        if domains is not None:
            raise NativeError("app.test_domains: only with app.files")
        return {}, []
    if not isinstance(files, dict):
        raise NativeError("app.files: a mapping of file paths in the app's home to seed:FILE#KEY")
    if not home_env:
        raise NativeError("app.files: needs home_env (the files go in that throwaway folder)")
    for path, ref in files.items():
        if not _inside(path):
            raise NativeError(f"app.files: {path!r} must be a path inside the app's home: names joined by /, not "
                              "absolute, and no . or ..")
        if not (isinstance(ref, str) and ref.startswith("seed:")):
            raise NativeError(f"app.files.{path}: comes only from a test fixture, as seed:FILE#KEY")
        try:
            vault.parse(ref)
        except vault.VaultError as e:
            raise NativeError(f"app.files.{path}: {e}") from None
    if not (isinstance(domains, list) and domains and all(isinstance(d, str) and DOMAIN.fullmatch(d)
                                                           for d in domains)):
        raise NativeError("app.test_domains: the email domains of the test accounts app.files may use, in lowercase "
                          "(such as [twinbox.invalid])")
    wider = [d for d in domains if not test_domain(d)]
    if wider:
        raise NativeError(f"app.test_domains: {wider} is not a test domain. A suite may only narrow the test domains: "
                          "names reserved for testing (.invalid, .test, .example, example.com, .net, .org), or one "
                          "listed in QAJEV_TEST_DOMAINS in ~/.qajev/.env")
    return {str(k): v for k, v in files.items()}, list(domains)


def test_domain(domain):
    """A domain no real person has mail at (RFC 2606 and 6761), or one the person lists in ~/.qajev/.env as
    QAJEV_TEST_DOMAINS (comma-separated). Only that file widens it: never a suite, a project or its env file
    (redteam on #145, the Orchestrator, 14 Oct)."""
    if is_test_email(f"x@{domain}"):
        return True
    try:
        listed = config._read(config.HOME / ".env").get("QAJEV_TEST_DOMAINS", "")
    except OSError:
        return False
    return domain in {d.strip().lower() for d in listed.split(",") if d.strip()}


def _inside(path):
    parts = str(path).split("/")
    return all(FILE_NAME.fullmatch(p) and p not in (".", "..") for p in parts)


def app_files(spec):
    """Each app.files value, from its seed fixture. -> {path: (value, fixture file name)}, or NativeError naming the
    file and what is wrong (never the value). The fixture must say "test_account": true and name its `account`, an
    email in one of app.test_domains."""
    out = {}
    for path, ref in spec["files"].items():
        file = str(vault.parse(ref)[1])
        try:
            data = vault.seed(file)
        except vault.VaultError as e:
            raise NativeError(f"app.files.{path}: {e}") from None
        account = data.get("account")
        domain = account.rpartition("@")[2].lower() if isinstance(account, str) and "@" in account else None
        if domain not in spec["test_domains"]:
            raise NativeError(f"app.files.{path}: seed:{Path(file).name} must name its test account (\"account\": an "
                              f"email at {' or '.join(spec['test_domains'])})")
        try:
            out[path] = (vault.resolve(ref), Path(file).name)  # resolve remembers it, so it is redacted everywhere
        except vault.VaultError as e:
            raise NativeError(f"app.files.{path}: {e}") from None
    return out


def write_private(home, path, value):
    """`value` and a newline into home/path, 0600, its folders 0700. Refused: a symlink on the way, or a file there."""
    home = Path(home)
    folder = home
    for part in Path(path).parts[:-1]:
        folder = folder / part
        if folder.is_symlink():
            raise NativeError(f"app.files.{path}: {folder.name} is a symbolic link; nothing was written")
        folder.mkdir(mode=0o700, exist_ok=True)
    target = home / path
    if not target.parent.resolve().is_relative_to(home.resolve()):
        raise NativeError(f"app.files.{path}: outside the app's home; nothing was written")
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise NativeError(f"app.files.{path}: cannot write it ({type(e).__name__})") from None
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")


OWNER = "owner.json"  # in each throwaway home: when the QAJev run that made it started, so a reused pid is not it


def _owner_gone(folder, pid):
    """The run that made `folder` is gone: its pid is free, or another process now has it (a later start). A live pid
    whose start cannot be read, or a home with no owner record, counts as still running: nothing is removed on a
    guess (redteam's note on #145)."""
    if not chrome.alive(pid):
        return True
    try:
        known = json.loads((folder / OWNER).read_text()).get("proc_start")
    except (OSError, ValueError, AttributeError):
        return False
    now = chrome.started(pid)
    return bool(known) and now is not None and now != known


def reap_homes():
    """Throwaway app homes whose QAJev run died without removing them (a kill -9): removed, with any file in them."""
    removed = []
    for folder in (HOME / "tmp").glob("qajev-app-*") if (HOME / "tmp").is_dir() else []:
        m = HOME_PREFIX.match(folder.name)
        if m and int(m[1]) != os.getpid() and _owner_gone(folder, int(m[1])):
            shutil.rmtree(folder, ignore_errors=True)
            removed.append(f"app home {folder.name}")
    return removed


class AppProcess(GodotGame):
    """A native app started by QAJev from its command, with its bridge. Use as a context manager."""

    engine = "app"
    error_line, harmless = APP_ERROR, NOTHING

    def __init__(self, project, spec, *, size=(1280, 720), env=None, args=None, hide=None, allow=None):
        self.project = Path(project).expanduser().resolve()
        if not self.project.is_dir():
            raise NativeError(f"{self.project} is not a folder: give the app's project folder")
        self.spec = app_spec(spec)
        exe = Path(self.spec["command"][0]).expanduser()
        if not exe.is_absolute():
            exe = self.project / exe
        if not exe.is_file() or not os.access(exe, os.X_OK):
            raise NativeError(f"app.command: no program at {exe} (build the app first, or fix the path)")
        self.command = [str(exe), *self.spec["command"][1:], *(args or [])]
        self.adapter = self.seed = None
        self.hide, self.allow = list(hide or []), list(allow or [])
        self.headless, self.size, self.start_wait = False, size, self.spec["start_wait"]
        reserved(env or {}, "--game-env")
        self.env = {**self.spec["env"], **(env or {})}
        self.proc = self.sock = self.file = None
        self.errors, self.log = [], []
        self.user_dir = self._key = None
        self.file_notes = [f"{path}: test token from {Path(str(vault.parse(ref)[1])).name}"
                           for path, ref in self.spec["files"].items()]

    def start(self):
        self.port = free_port()
        STATE.mkdir(parents=True, exist_ok=True)
        (HOME / "tmp").mkdir(parents=True, exist_ok=True)
        # A throwaway home: the app never reads or writes the person's real one. A relaunch keeps it, so what the
        # first launch saved is what the next one reads (a setting that must survive a restart).
        first = self.user_dir is None
        if self.user_dir is None:
            self.user_dir = tempfile.mkdtemp(prefix=f"qajev-app-{os.getpid()}-", dir=HOME / "tmp")
            (Path(self.user_dir) / OWNER).write_text(json.dumps({"pid": os.getpid(),
                                                                 "proc_start": chrome.started(os.getpid())}))
        # The app's environment: the allowlist from QAJev's own, the suite's env, then the throwaway home and the
        # bridge last, so nothing before them can move them.
        env = {**passed_env(os.environ), **self.env, **save_isolation(self.user_dir),
               "QAJEV_BRIDGE_PORT": str(self.port),
               "QAJEV_WINDOW": f"{self.size[0]}x{self.size[1]}"}
        if self.spec["home_env"]:
            app_home = Path(self.user_dir) / "app"
            app_home.mkdir(exist_ok=True)
            env[self.spec["home_env"]] = str(app_home)
            if first:  # a relaunch keeps what the app made of them
                for path, (value, _fixture) in app_files(self.spec).items():
                    write_private(app_home, path, value)
        if self.spec["bridge_key"]:  # new for each launch; only the app and its bridge's first line see it
            self._key = secrets.token_hex(32)
            remember_secret(self._key)
            env["QAJEV_BRIDGE_KEY"] = self._key
        self.errors, self.log = [], []
        self.proc = subprocess.Popen(niced(self.command), cwd=self.project, env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, stdin=subprocess.DEVNULL,
                                     start_new_session=True)
        threading.Thread(target=self._read_output, daemon=True).start()
        self.record = {"pid": self.proc.pid, "owner_pid": os.getpid(), "engine": "app", "project": str(self.project),
                       "command": Path(self.command[0]).name, "adapter": None, "port": self.port, "headless": False,
                       "started_at": time.time(), "user_dir": self.user_dir,
                       "identity": chrome.identity(self.proc.pid)}  # the reaper kills only this very process
        (STATE / f"{self.proc.pid}.json").write_text(json.dumps(self.record))
        return self._wait_for_bridge()

    def __enter__(self):
        try:
            self.start()
        except BaseException:  # a start that fails part-way still removes the home and any file written into it
            self.close()
            raise
        return self

    def _wait_for_bridge(self):
        super()._wait_for_bridge()
        if self._key and self.file is not None:
            try:
                self.file.write(self._key + "\n")
                self.file.flush()
            except OSError as e:
                raise NativeError(f"lost the app's bridge while sending its key: {e}") from None
        return self

    def _exited(self, code):
        return (f"the app exited with {code} before its bridge came up: "
                + (" | ".join(self.log[-5:]) or "it printed nothing"))
