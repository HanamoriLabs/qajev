"""Projects: a product's QA objectives, where they run, and where their reports go.

A project is `.qajev/project.toml` inside the product's repo, or a central `<name>.toml` (with `repo = "..."`) in a
projects folder while the repo has not adopted one. Objectives are plain goals with checks; they become an ordinary
suite, so everything a suite guarantees (read-only production, local-dev-only mutate, the guard) applies unchanged.

    name = "shop"
    default_env = "prod"
    [env.prod]
    base_url = "https://shop.example"
    hosts = ["shop.example"]
    [budget]
    cost_cap_usd = 0.50
    [[objective]]
    name = "pricing is clear"
    url = "/pricing"
    goal = "Find what the Team plan costs per month. Stop when that price is visible."
    expect = { text = ["Team"] }
    tags = ["core"]
"""

import fcntl
import json
import os
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import vault
from .config import HOME

CONFIG_NAME = Path(".qajev") / "project.toml"
PROJECT_KEYS = {"name", "repo", "default_env", "env", "accounts", "account", "budget", "guard", "persona", "device",
                "objective", "known", "motion", "devices", "real_devices"}
KNOWN_KEYS = {"kind", "url", "note"}
ENV_KEYS = {"base_url", "hosts", "mode", "device", "devices", "real_devices", "persona", "smoke_start", "motion",
            "account", "allow_destructive"}
OBJECTIVE_KEYS = {"name", "env", "url", "goal", "expect", "tags", "before", "after", "budget", "device", "persona",
                  "mode", "settle", "depends_on", "speech", "vision", "about"}
# email and password are vault references (vault.py) or, for email, the plain address; never a password value.
ACCOUNT_KEYS = {"email", "password", "login", "email_env", "password_env", "seed", "profile", "note", "totp", "cookie"}


class ProjectError(ValueError):
    pass


def _source_root():
    root = Path(__file__).resolve().parent.parent
    return root if (root / "pyproject.toml").exists() and (root / "qajev").is_dir() else None


def search_dirs():
    dirs = [Path(p).expanduser() for p in os.environ.get("QAJEV_PROJECTS", "").split(os.pathsep) if p]
    dirs.append(HOME / "projects")
    root = _source_root()
    if root:
        dirs.append(root / "projects")
    return dirs


def reports_root():
    """Where the cross-project index (and reports of projects whose repo has not adopted .qajev/) live."""
    if os.environ.get("QAJEV_REPORTS"):
        return Path(os.environ["QAJEV_REPORTS"]).expanduser()
    return (_source_root() or HOME) / "reports"


@dataclass
class Project:
    name: str
    config_path: Path
    repo: Path | None
    in_repo: bool
    default_env: str
    envs: dict
    accounts: dict
    budget: dict
    guard: dict
    persona: str | None
    device: object
    objectives: list = field(default_factory=list)
    known: list = field(default_factory=list)
    motion: str | None = None
    devices: list | None = None  # every objective runs on each (default desktop and phone)
    real_devices: list | None = None  # opt-in: also in a real device browser (ios, android)
    account: str | None = None  # the account every env signs in with, unless the env names its own

    @property
    def reports_dir(self):
        if self.in_repo and self.repo is not None:  # in_repo always comes with its repo
            return self.repo / ".qajev" / "reports"
        return reports_root() / self.name


def find(ref):
    """A project by path (repo dir, .qajev dir or .toml file) or by name in the projects folders."""
    path = Path(ref).expanduser()
    for candidate in (path / CONFIG_NAME, path / "project.toml", path):
        if candidate.is_file() and candidate.suffix == ".toml":
            return candidate
    for folder in search_dirs():
        if (folder / f"{ref}.toml").is_file():
            return folder / f"{ref}.toml"
    for folder in (Path.cwd(), *Path.cwd().parents):  # inside its repository, a project answers to its own name
        here = folder / CONFIG_NAME
        if here.is_file():
            try:
                if tomllib.loads(here.read_text()).get("name") == ref:
                    return here
            except tomllib.TOMLDecodeError:
                pass  # load() reports a broken file when it is asked for by path
            break
    known = sorted({p.stem for d in search_dirs() if d.is_dir() for p in d.glob("*.toml")})
    raise ProjectError(f"no project {ref!r}: give a repo path with {CONFIG_NAME}, a .toml file, or one of {known}")


def load(ref):
    path = find(ref)
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ProjectError(f"{path}: {e}") from None
    extra = set(data) - PROJECT_KEYS
    if extra:
        raise ProjectError(f"{path}: unknown key(s) {sorted(extra)}")
    in_repo = path.parent.name == ".qajev"
    repo = path.parent.parent if in_repo else (Path(data["repo"]).expanduser() if data.get("repo") else None)
    envs = data.get("env") or {}
    if not envs:
        raise ProjectError(f"{path}: needs at least one [env.<name>] with a base_url")
    for name, env in envs.items():
        if set(env) - ENV_KEYS:
            raise ProjectError(f"{path}: env.{name}: unknown key(s) {sorted(set(env) - ENV_KEYS)}")
        if not env.get("base_url"):
            raise ProjectError(f"{path}: env.{name} needs base_url")
    accounts = data.get("accounts") or {}
    for name, account in accounts.items():
        # Accounts are references (vault references, env var names, seed scripts, signed-in profiles), never
        # secrets in the file.
        if set(account) - ACCOUNT_KEYS:
            raise ProjectError(f"{path}: accounts.{name}: only {sorted(ACCOUNT_KEYS)} (names, never values)")
        refs = {"password": account.get("password"), "totp": account.get("totp"),
                "cookie.value": (account.get("cookie") or {}).get("value") if isinstance(account.get("cookie"), dict)
                else account.get("cookie")}
        for key, ref in refs.items():
            if ref is None:
                continue
            try:
                vault.parse(ref)
            except vault.VaultError as e:
                raise ProjectError(f"{path}: accounts.{name}.{key}: {e} (names, never values)") from None
        if "login" in account and not (isinstance(account["login"], dict) and account["login"].get("url")):
            raise ProjectError(f'{path}: accounts.{name}: write login = {{ url = "/login" }} (the sign-in page)')
    for where, chosen in [("account", data.get("account"))] + [(f"env.{n}.account", e.get("account"))
                                                                for n, e in envs.items()]:
        if chosen and chosen not in accounts:
            raise ProjectError(f"{path}: {where}: no account {chosen!r}; have {sorted(accounts)}")
    objectives = data.get("objective") or []
    for i, obj in enumerate(objectives):
        if set(obj) - OBJECTIVE_KEYS:
            raise ProjectError(f"{path}: objective[{i}]: unknown key(s) {sorted(set(obj) - OBJECTIVE_KEYS)}")
        if obj.get("env") and obj["env"] not in envs:
            raise ProjectError(f"{path}: objective[{i}] env {obj['env']!r} is not one of {sorted(envs)}")
    known = data.get("known") or []
    for i, item in enumerate(known):
        if set(item) - KNOWN_KEYS or not item.get("kind") or not item.get("note"):
            raise ProjectError(f"{path}: known[{i}] needs kind and note (optionally url); keys {sorted(KNOWN_KEYS)}")
    default_env = data.get("default_env") or next(iter(envs))
    if default_env not in envs:
        raise ProjectError(f"{path}: default_env {default_env!r} is not one of {sorted(envs)}")
    return Project(
        name=data.get("name") or (repo.name if repo else path.stem), config_path=path, repo=repo, in_repo=in_repo,
        default_env=default_env, envs=envs, accounts=data.get("accounts") or {}, budget=data.get("budget") or {},
        guard=data.get("guard") or {}, persona=data.get("persona"), device=data.get("device"),
        devices=data.get("devices"), real_devices=data.get("real_devices"), objectives=objectives,
        known=known, motion=data.get("motion"), account=data.get("account"),
    )


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "objective"


def suite_data(project, *, env=None, tags=(), names=(), objective=None, url=None, expect=None, about=None):
    """The suite (as data for suite.parse) that runs the chosen objectives in one environment."""
    env_name = env or project.default_env
    if env_name not in project.envs:
        raise ProjectError(f"{project.name}: no env {env_name!r}; have {sorted(project.envs)}")
    target = project.envs[env_name]
    if objective:
        chosen = [{"name": slug(objective), "url": url or "/", "goal": objective, "expect": expect or {},
                   **({"about": about} if about else {})}]
    else:
        chosen = [o for o in project.objectives if (o.get("env") or project.default_env) == env_name]
        if tags:
            chosen = [o for o in chosen if set(tags) & set(o.get("tags") or [])]
        if names:
            missing = set(names) - {o["name"] for o in chosen}
            if missing:
                raise ProjectError(f"{project.name}: no objective named {sorted(missing)} in env {env_name!r}")
            chosen = [o for o in chosen if o["name"] in names]
        if not chosen:
            raise ProjectError(f"{project.name}: no objectives for env {env_name!r}"
                               + (f" tagged {sorted(tags)}" if tags else ""))
    scenarios = [{k: v for k, v in o.items() if k not in {"env", "tags"}} for o in chosen]
    for s in scenarios:
        s.setdefault("url", "/")  # objectives are independent unless they say depends_on
    budget = dict(project.budget)
    data = {
        "name": f"{project.name} {env_name}",
        "base_url": target["base_url"],
        "hosts": list(target.get("hosts") or []),
        "mode": target.get("mode", "readonly"),
        "budget": {k: budget[k] for k in ("actions", "seconds") if k in budget},
        "cost_cap_usd": float(budget.get("cost_cap_usd", 0.5)),
        "guard": project.guard,
        "scenarios": scenarios,
    }
    if "allow_destructive" in target:  # a local env only: the suite refuses it for a production host
        data["allow_destructive"] = target["allow_destructive"]
    for key in ("persona", "device", "devices", "real_devices", "motion"):
        value = target.get(key) or getattr(project, key, None)
        if value:
            data[key] = value
    account = target.get("account") or project.account
    if account:
        data["account"] = sign_in_account(project, account)
    return data


def sign_in_account(project, name):
    """A project account as a suite's account block (the older email_env/password_env become env: references)."""
    from . import vault

    acct = project.accounts[name]
    folder = project.repo or (project.config_path.parent if project.config_path else None)  # seed:FILE#KEY base
    email = acct.get("email") or (f"env:{acct['email_env']}" if acct.get("email_env") else None)
    password = acct.get("password") or (f"env:{acct['password_env']}" if acct.get("password_env") else None)
    if not (email and (password or acct.get("cookie")) and acct.get("login")):
        raise ProjectError(f"{project.name}: accounts.{name} needs email, password (or cookie) and login = "
                           "{ url = ... } to sign in")
    out = {"name": name, "email": vault.anchor(email, folder), "login": dict(acct["login"])}
    if password:
        out["password"] = vault.anchor(password, folder)
    if acct.get("totp"):
        out["totp"] = vault.anchor(acct["totp"], folder)
    if acct.get("cookie"):
        out["cookie"] = {**acct["cookie"], "value": vault.anchor(acct["cookie"].get("value"), folder)}
    return out


def run_dir_parent(project):
    folder = project.reports_dir / time.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    if project.in_repo:
        ignore = project.repo / ".qajev" / ".gitignore"
        if not ignore.exists():
            ignore.write_text("reports/\n")  # reports stay local; the project.toml itself is meant to be committed
    return folder


def apply_known(project, report):
    """Findings the owner already knows about (with their note) are listed as known, not raised again."""
    def match(f):
        for k in project.known:
            if k["kind"].lower() in f["kind"].lower() and (not k.get("url") or k["url"] == (f.get("url") or "")):
                return k
        return None

    known = []
    for s in report["scenarios"]:
        keep = []
        for f in s.get("findings") or []:
            k = match(f)
            if k:
                known.append({**f, "known": k["note"]})
            else:
                keep.append(f)
        s["findings"] = keep
    report["findings"] = [f for f in report["findings"] if not match(f)]
    report["known_findings"] = known
    return report


def findings_file(report):
    """findings.json: each finding with its kind (product or harness), severity, the steps Jev took, and evidence."""
    out = []
    for s in report["scenarios"]:
        steps = [f"{h.get('step')}. {h.get('kind')} {h.get('action')}" + (f" = {h['text']!r}" if h.get("text") else "")
                 + f"  ({h.get('url')})" for h in s.get("history") or []]
        failed = [c for c in s.get("checks") or [] if not c["ok"]]
        if s["outcome"] in {"fail", "stuck", "harness"}:
            out.append({
                "objective": s["name"], "kind": "product" if s["outcome"] in {"fail", "stuck"} else "harness",
                "outcome": s["outcome"], "severity": {"fail": "S1", "stuck": "S2"}.get(s["outcome"], "note"),
                "summary": s.get("reason"), "steps": steps, "url": s.get("end_url"),
                "evidence": {"failed_checks": failed, "page_says": s.get("page_says"), "shot": s.get("shot")},
            })
        for f in s.get("findings") or []:
            out.append({"objective": s["name"], "kind": "product", "outcome": s["outcome"], "severity": f["severity"],
                        "summary": f"{f['kind']}: {f['detail']}", "steps": steps, "url": f.get("url"),
                        "evidence": {"shot": s.get("shot")}})
    return out


def record(project, report, *, env):
    """Write findings.json next to the report and add the run to the cross-project index."""
    run_dir = Path(report["run_dir"])
    findings = findings_file(report)
    (run_dir / "findings.json").write_text(json.dumps(findings, indent=2, default=str))
    row = {
        "project": project.name, "env": env, "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "gate": report["gate"],
        "cost_usd": report["cost"]["usd"], "seconds": report["seconds"], "report": str(run_dir / "report.md"),
        "html": str(run_dir / "report.html"),
        "objectives": [{"name": s["name"], "outcome": s["outcome"], "reason": s.get("reason"),
                        "seconds": s.get("seconds"), "cost_usd": s.get("cost_usd")} for s in report["scenarios"]],
        "findings": {"product": sum(1 for f in findings if f["kind"] == "product"),
                     "harness": sum(1 for f in findings if f["kind"] == "harness")},
        "kind": "smoke" if report.get("smoke") else "objectives",
    }
    if report.get("changes"):
        row["changes"] = {"summary": report["changes"]["summary"], "changed": report["changes"]["changed"]}
    index = reports_root() / "index.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    with open(index, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)  # several sessions may finish runs at once
        handle.seek(0)
        text = handle.read()
        rows = json.loads(text) if text.strip() else []
        rows.append(row)
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(rows, indent=2))
    return row


def index_rows(project=None, limit=20):
    index = reports_root() / "index.json"
    rows = json.loads(index.read_text()) if index.exists() and index.read_text().strip() else []
    if project:
        rows = [r for r in rows if r["project"] == project]
    return rows[-limit:][::-1]


def listing():
    out = []
    for folder in search_dirs():
        for path in sorted(folder.glob("*.toml")) if folder.is_dir() else []:
            try:
                p = load(path)
            except ProjectError as e:
                out.append({"name": path.stem, "config": str(path), "error": str(e)})
                continue
            out.append({"name": p.name, "config": str(p.config_path), "repo": str(p.repo) if p.repo else None,
                        "envs": {k: v["base_url"] for k, v in p.envs.items()}, "objectives": len(p.objectives),
                        "reports": str(p.reports_dir)})
    return out
