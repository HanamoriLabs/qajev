"""Suite and scenario model. A suite is YAML or JSON; `qajev check` builds a one-scenario suite from flags."""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from . import keys
from .config import host_of, is_loopback
from .guard import MODES

DEVICES = {
    "desktop": {"width": 1280, "height": 900, "mobile": False, "scale": 1},
    # Jev scrolls only the window; apps that scroll inside a panel need everything on one tall screen.
    "tall": {"width": 1280, "height": 2400, "mobile": False, "scale": 1},
    # Phones and tablets also say so (user agent, platform), as a browser's device mode does: many sites serve a
    # different page by user agent, not only by width.
    "phone": {"width": 390, "height": 844, "mobile": True, "scale": 3, "platform": "iPhone",
              "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/18.5 Mobile/15E148 Safari/604.1"},
    "tablet": {"width": 820, "height": 1180, "mobile": True, "scale": 2, "platform": "iPad",
               "ua": "Mozilla/5.0 (iPad; CPU OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                     "Version/18.5 Mobile/15E148 Safari/604.1"},
}
# Every website test runs on each of these unless a suite, a scenario or the run pins one device.
DEFAULT_DEVICES = ("desktop", "phone")


REAL_DEVICES = ("ios", "android")


def _real_devices(value):
    """Opt-in: also run each website scenario in a real device browser (Safari on an iOS simulator, Chrome on an
    Android emulator). Off unless a suite, a project, --real-devices or $QAJEV_REAL_DEVICES asks."""
    names = [d.strip().lower() for d in (value.split(",") if isinstance(value, str) else value or []) if str(d).strip()]
    bad = [d for d in names if d not in REAL_DEVICES]
    if bad:
        raise SuiteError(f"real_devices: {bad} unknown; use {list(REAL_DEVICES)}")
    return names


def wanted_devices(explicit=None):
    """The devices a run tests on: explicit (a list or "a,b"), else $QAJEV_DEVICES, else desktop and phone."""
    value = explicit or os.environ.get("QAJEV_DEVICES") or list(DEFAULT_DEVICES)
    names = [d.strip() for d in (value.split(",") if isinstance(value, str) else value) if str(d).strip()]
    for name in names:
        _device(name, "devices")
    return names or list(DEFAULT_DEVICES)
HOOK_KINDS = {"js", "fill", "click", "navigate", "wait_for", "key", "react", "sleep", "command", "reload"}
EXPECT_KEYS = {"url", "url_regex", "status", "text", "absent", "visible", "js", "fetch", "command", "ignore_case",
               "looks", "across", "says"}
# The checks only their author can describe, by what `says` gives them in plain words (the test plan, 6 Oct)
SAYS_KEYS = {"js", "url_regex", "command", "fetch"}
SCENARIO_KEYS = {
    "name", "url", "goal", "expect", "settle", "budget", "before", "after", "depends_on", "mode", "device",
    "persona", "speech", "vision", "about", "clients", "steps", "state", "seed",
}
# A multiplayer scenario (clients.py): N players, each in a browser context of its own, driven by steps.
MAX_CLIENTS = 12
CLIENT_EXPECT_KEYS = {"url", "url_regex", "status", "text", "absent", "visible", "js", "ignore_case", "across"}
CLIENT_HOOK_KINDS = HOOK_KINDS - {"command"}  # a shell command is not a player's move
STEP_KEYS = {"all", "client", "stagger", "jitter", "snapshot", "until", "timeout", "settle", "expect"}
SUITE_KEYS = {
    "name", "base_url", "mode", "persona", "device", "budget", "cost_cap_usd", "guard", "speech", "hosts",
    "scenarios", "settle", "motion", "devices", "real_devices", "account", "vision", "about",
}
# A test account QAJev signs in with before the scenarios (see signin.py); the password is a vault reference.
ACCOUNT_KEYS = {"name", "email", "password", "login"}
LOGIN_KEYS = {"url", "email_field", "password_field", "next", "submit", "signed_in"}
SIGNED_IN_KEYS = {"url_not", "text", "js"}
# reduce: the tab tells pages the visitor prefers reduced motion (CSS media query and matchMedia), so sites that
# honour it stop scroll-scrubbing, carousels and counters that otherwise keep changing under Jev. full: as-is.
MOTIONS = ("reduce", "full")
GUARD_KEYS = {"deny", "allow", "block_urls", "allow_requests", "redact_emails", "allow_secret_fields"}
DEFAULT_BUDGET = {"actions": 20, "seconds": 90}


class SuiteError(ValueError):
    """The suite is malformed or asks for something QAJev refuses to do."""


@dataclass
class Scenario:
    name: str
    url: str | None
    goal: str | None
    expect: dict
    settle: float
    budget: dict
    before: list
    after: list
    depends_on: list
    mode: str
    device: dict
    persona: str | None
    speech: str | None
    vision: bool = False  # Clef sees the screenshot with every decision (vision.py)
    about: str | None = None  # what the test proves and why, in plain words, for the person reading the report
    clients: list | None = None  # a multiplayer scenario: [{name, url}], one browser context each (clients.py)
    steps: list = field(default_factory=list)  # its steps, in order (see _steps)
    state: str | None = None  # JS read on every client for the across checks and snapshots
    seed: int | None = None  # for `jitter`: the same seed gives the same offsets

    @property
    def task(self):
        if not self.goal:
            return None
        return f"{self.persona}\n\nWhat you want now: {self.goal}" if self.persona else self.goal

    @property
    def uses_commands(self):
        hooks = [*self.before, *self.after]
        return any("command" in h for h in hooks) or "command" in self.expect


@dataclass
class Suite:
    name: str
    base_url: str | None
    hosts: list
    guard: dict
    cost_cap_usd: float
    scenarios: list = field(default_factory=list)
    path: Path | None = None
    motion: str = "reduce"
    real_devices: list = field(default_factory=list)  # also run in a real device browser: "ios", "android"
    account: dict | None = None  # signed in once before the scenarios; its password is a vault reference
    about: str | None = None  # what the run as a whole proves

    @property
    def mutates(self):
        return any(s.mode == "mutate" for s in self.scenarios)


def _unknown(where, data, allowed):
    extra = set(data) - allowed
    if extra:
        raise SuiteError(f"{where}: unknown key(s) {sorted(extra)}; allowed: {sorted(allowed)}")


def _list(value, where):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise SuiteError(f"{where} must be a string or a list")
    return value


def _device(value, where):
    if value is None:
        return dict(DEVICES["desktop"])
    if isinstance(value, str):
        match = re.fullmatch(r"(\d{3,4})x(\d{3,4})", value)
        if match:
            return {"width": int(match[1]), "height": int(match[2]), "mobile": False, "scale": 1}
        if value not in DEVICES:
            raise SuiteError(f"{where}: device must be one of {sorted(DEVICES)} or WIDTHxHEIGHT")
        return dict(DEVICES[value])
    if isinstance(value, dict) and {"width", "height"} <= set(value):
        return {"mobile": False, "scale": 1, **value}
    raise SuiteError(f"{where}: device must be a name, WIDTHxHEIGHT, or {{width, height}}")


def _hooks(value, where):
    hooks = _list(value, where)
    for i, hook in enumerate(hooks):
        if not isinstance(hook, dict) or len(hook) != 1 or next(iter(hook)) not in HOOK_KINDS:
            raise SuiteError(f"{where}[{i}] must be one of {sorted(HOOK_KINDS)}, e.g. {{js: '...'}}")
        _key_hook(hook, f"{where}[{i}]")
    return hooks


def _key_hook(hook, where):
    """A key hook names real keys within its bounds (keys.py), checked here so a bad one fails before the run."""
    for kind, check in (("key", keys.plan), ("react", keys.react_plan)):
        if kind in hook:
            try:
                check(hook[kind])
            except ValueError as e:
                raise SuiteError(f"{where}.{kind}: {e}") from None


def _expect(value, where):
    value = value or {}
    if not isinstance(value, dict):
        raise SuiteError(f"{where} must be a mapping")
    _unknown(where, value, EXPECT_KEYS)
    out: dict[str, Any] = dict(value)
    out["text"] = _list(value.get("text"), f"{where}.text")
    out["absent"] = _list(value.get("absent"), f"{where}.absent")
    out["visible"] = _list(value.get("visible"), f"{where}.visible")
    out["looks"] = [str(s) for s in _list(value.get("looks"), f"{where}.looks")]  # judged from the screenshot
    out["across"] = _across(value.get("across"), f"{where}.across")  # a multiplayer scenario's (clients.py)
    out["says"] = _says(value.get("says"), f"{where}.says")
    if "url_regex" in out:
        try:
            re.compile(out["url_regex"])
        except re.error as e:
            raise SuiteError(f"{where}.url_regex is not a valid regex: {e}") from None
    if "status" in out and (isinstance(out["status"], bool) or not isinstance(out["status"], int)):
        raise SuiteError(f"{where}.status must be an HTTP status number, such as 404")
    out["fetch"] = [{"url": p, "status": 200} if isinstance(p, str) else p
                    for p in _list(value.get("fetch"), f"{where}.fetch")]
    for i, probe in enumerate(out["fetch"]):
        if not isinstance(probe, dict) or "url" not in probe:
            raise SuiteError(f"{where}.fetch[{i}] needs a url")
    if "command" in out:
        cmd = out["command"]
        out["command"] = {"run": cmd} if isinstance(cmd, str) else cmd
        if not isinstance(out["command"], dict) or "run" not in out["command"]:
            raise SuiteError(f"{where}.command needs run")
    return out


def _says(value, where):
    """`says`: what a check proves, in plain words, for the test plan. A string is the `js` check's; a mapping gives
    each check only its author can describe ({js, url_regex, command, fetch}) its own. -> {kind: words}"""
    if value is None:
        return {}
    if isinstance(value, str):
        value = {"js": value}
    if not isinstance(value, dict) or set(value) - SAYS_KEYS or not all(
            isinstance(v, str) and v.strip() for v in value.values()):
        raise SuiteError(f"{where} must be plain words for the js check, or a mapping of "
                         f"{', '.join(sorted(SAYS_KEYS))} to plain words")
    return {k: v.strip() for k, v in value.items()}


def _across(value, where):
    """Checks judged over every client's state: "js" or {check, js}. -> [{check, js}]."""
    out = []
    for i, item in enumerate(_list(value, where)):
        if isinstance(item, str):
            item = {"js": item}
        if not isinstance(item, dict) or not isinstance(item.get("js"), str) or set(item) - {"check", "js", "says"}:
            raise SuiteError(f"{where}[{i}] must be a JS expression, or {{check: name, js: expression, says: words}}")
        # a check named apart from its expression is named in the author's words
        says = item.get("says") or (item["check"] if item.get("check") and item["check"] != item["js"] else None)
        entry = {"check": str(item.get("check") or item["js"]), "js": item["js"]}
        out.append({**entry, "says": says} if says else entry)
    return out


def _clients(value, url, where):
    """clients: N, [names], or [{name, url}] -> [{name, url}] (url may hold {client}, {i}, {run})."""
    if isinstance(value, bool) or not isinstance(value, (int, list)):
        raise SuiteError(f"{where}.clients must be a number, a list of names, or a list of {{name, url}}")
    items = [f"p{i}" for i in range(1, value + 1)] if isinstance(value, int) else value
    if not 2 <= len(items) <= MAX_CLIENTS:
        raise SuiteError(f"{where}.clients: from 2 to {MAX_CLIENTS} clients")
    out = []
    for i, item in enumerate(items):
        item = {"name": item} if isinstance(item, str) else item
        if not isinstance(item, dict) or set(item) - {"name", "url"} or not re.fullmatch(r"[\w-]{1,32}",
                                                                                           str(item.get("name"))):
            raise SuiteError(f"{where}.clients[{i}] must be a name (letters, digits, _ or -) or {{name, url}}")
        out.append({"name": str(item["name"]), "url": item.get("url") or url})
        if not out[-1]["url"]:
            raise SuiteError(f"{where}.clients[{i}] needs a url (or give the scenario one)")
    if len({c["name"] for c in out}) != len(out):
        raise SuiteError(f"{where}.clients: names must be different")
    return out


def _steps(value, names, where):
    """A multiplayer scenario's steps, each one of:
    {all: hook, stagger?: ms, jitter?: ms}   every client, at the same instant (or staggered, or jittered)
    {client: name | [names], <hook>: ...}    some clients
    {snapshot: name, until?: js, timeout?: s, settle?: s, expect?: [across]}   every client's state, at once"""
    steps = []
    items: list[Any] = _list(value, where)
    for i, step in enumerate(items):
        at = f"{where}[{i}]"
        if not isinstance(step, dict):
            raise SuiteError(f"{at} must be a mapping")
        hook_keys = [k for k in step if k in HOOK_KINDS]
        _unknown(at, {k: v for k, v in step.items() if k not in HOOK_KINDS}, STEP_KEYS)
        kinds = [k for k in ("all", "client", "snapshot") if k in step]
        if len(kinds) != 1:
            raise SuiteError(f"{at} needs exactly one of all, client or snapshot")
        kind = kinds[0]
        out: dict[str, Any] = {"kind": kind}
        if kind == "snapshot":
            if hook_keys or set(step) & {"stagger", "jitter"}:
                raise SuiteError(f"{at}: a snapshot reads state; it takes until, timeout, settle and expect only")
            if not re.fullmatch(r"[A-Za-z_]\w{0,31}", str(step["snapshot"])):
                raise SuiteError(f"{at}.snapshot must be a name (letters, digits, _), used as snapshots.<name>")
            out.update(name=str(step["snapshot"]), until=step.get("until"), timeout=float(step.get("timeout", 10)),
                       settle=float(step.get("settle", 0)), expect=_across(step.get("expect"), f"{at}.expect"))
        else:
            hook = step["all"] if kind == "all" else {k: step[k] for k in hook_keys}
            if kind == "client" and set(step) & {"stagger", "jitter"}:
                raise SuiteError(f"{at}: stagger and jitter belong to an `all` step")
            if kind == "all" and hook_keys:
                raise SuiteError(f"{at}: put the hook under all: (all: {{js: ...}})")
            if not isinstance(hook, dict) or len(hook) != 1 or next(iter(hook)) not in CLIENT_HOOK_KINDS:
                raise SuiteError(f"{at} needs one hook, one of {sorted(CLIENT_HOOK_KINDS)}")
            _key_hook(hook, at)
            who = names if kind == "all" else _list(step["client"], f"{at}.client")
            unknown = [w for w in who if w not in names]
            if unknown:
                raise SuiteError(f"{at}.client: no client named {unknown}; the clients are {names}")
            for key in ("stagger", "jitter"):
                if isinstance(step.get(key, 0), bool) or not isinstance(step.get(key, 0), (int, float)) \
                        or step.get(key, 0) < 0:
                    raise SuiteError(f"{at}.{key} must be milliseconds (0 or more)")
            out.update(hook=hook, clients=list(who), stagger=float(step.get("stagger", 0)),
                       jitter=float(step.get("jitter", 0)))
        steps.append(out)
    return steps


def page_checks(expect):
    return bool(expect.get("url") or expect.get("url_regex") or expect.get("text") or expect.get("absent")
                or expect.get("visible") or expect.get("js") or expect.get("status"))


def has_checks(expect):
    # looks is not a page check: it costs a model call, so it is judged once, at the end, not while Jev moves.
    return page_checks(expect) or bool(expect.get("fetch") or expect.get("command") or expect.get("looks")
                                       or expect.get("across"))


def _account(raw, base):
    from . import vault

    if not isinstance(raw, dict):
        raise SuiteError("account must be a mapping: email, password, login")
    _unknown("account", raw, ACCOUNT_KEYS)
    if not isinstance(raw.get("email"), str) or not raw["email"].strip():
        raise SuiteError("account.email is required (the test account's sign-in name, or an env:/keychain:/op:// "
                         "reference to it)")
    try:
        vault.parse(raw.get("password"))
    except vault.VaultError as e:
        raise SuiteError(f"account.password: {e}") from None
    login = raw.get("login")
    if not isinstance(login, dict) or not login.get("url"):
        raise SuiteError("account.login.url is required (the sign-in page)")
    _unknown("account.login", login, LOGIN_KEYS)
    _unknown("account.login.signed_in", login.get("signed_in") or {}, SIGNED_IN_KEYS)
    url = urljoin(base, login["url"]) if base else login["url"]
    scheme = urlsplit(url).scheme
    if scheme != "https" and not (scheme == "http" and is_loopback(url)):
        raise SuiteError("account.login.url must be https (http only on localhost): a password never travels "
                         "unencrypted")
    return {"name": str(raw.get("name") or raw["email"]), "email": raw["email"], "password": raw["password"],
            "login": {**login, "url": url}}


def parse(data, path=None, devices=None):
    if not isinstance(data, dict):
        raise SuiteError("a suite must be a mapping with a scenarios list")
    _unknown("suite", data, SUITE_KEYS)
    base = data.get("base_url")
    if base and urlsplit(base).scheme not in {"http", "https"}:
        raise SuiteError("base_url must be http(s)")
    guard = data.get("guard") or {}
    _unknown("guard", guard, GUARD_KEYS)
    suite_mode = data.get("mode", "readonly")
    motion = data.get("motion", "reduce")
    if motion not in MOTIONS:
        raise SuiteError(f"motion must be one of {MOTIONS}")
    suite_budget = {**DEFAULT_BUDGET, **(data.get("budget") or {})}
    raw = data.get("scenarios")
    if not isinstance(raw, list) or not raw:
        raise SuiteError("scenarios must be a non-empty list")

    # One device pinned for the whole suite, or each scenario on every wanted device (desktop and phone by default).
    try:
        wanted = [None] if data.get("device") else wanted_devices(devices or data.get("devices"))
    except SuiteError as e:
        raise SuiteError(f"devices: {e}") from None
    scenarios, names, pinned = [], set(), set()
    for i, item in enumerate(raw):
        where = f"scenarios[{i}]"
        if not isinstance(item, dict):
            raise SuiteError(f"{where} must be a mapping")
        _unknown(where, item, SCENARIO_KEYS)
        name = str(item.get("name") or f"scenario-{i + 1}")
        if name in names:
            raise SuiteError(f"{where}: duplicate name {name!r}")
        names.add(name)
        url = item.get("url")
        if url:
            url = urljoin(base, url) if base else url
            if urlsplit(url).scheme not in {"http", "https"}:
                raise SuiteError(f"{where}: url must be http(s) or relative to base_url (got {item.get('url')!r})")
        elif i == 0:
            if not base:
                raise SuiteError(f"{where}: the first scenario needs a url (or set base_url)")
            url = base
        if item.get("device"):
            pinned.add(name)
        mode = item.get("mode", suite_mode)
        if mode not in MODES:
            raise SuiteError(f"{where}: mode must be one of {MODES}")
        expect = _expect(item.get("expect"), f"{where}.expect")
        goal = item.get("goal")
        clients = None
        if "clients" in item:
            if goal or item.get("before") or item.get("after") or item.get("persona"):
                raise SuiteError(f"{where}: a scenario with clients has steps, not a goal, persona or before/after "
                                 "hooks")
            if not item.get("url"):
                raise SuiteError(f"{where}: a scenario with clients needs its own url (it does not continue a page)")
            _unknown(f"{where}.expect", {k: v for k, v in (item.get("expect") or {}).items()}, CLIENT_EXPECT_KEYS)
            clients = _clients(item["clients"], url, where)
            if not item.get("steps") and not item.get("state") and not has_checks(expect):
                raise SuiteError(f"{where}: give the clients steps and an expect block")
        else:
            for key in ("steps", "state", "seed"):
                if key in item:
                    raise SuiteError(f"{where}.{key} belongs to a scenario with clients")
            if expect["across"]:
                raise SuiteError(f"{where}.expect.across belongs to a scenario with clients")
            if not url and scenarios and scenarios[-1].clients:
                raise SuiteError(f"{where}: give it a url; the scenario before it ran in its own clients' tabs")
        if expect["across"] and not item.get("state"):
            raise SuiteError(f"{where}: expect.across reads each client's state: give state: <JS expression>")
        if not goal and not has_checks(expect) and clients is None:
            raise SuiteError(f"{where}: give a goal, an expect block, or both")
        depends = _list(item.get("depends_on"), f"{where}.depends_on")
        if not url and not depends:
            depends = [scenarios[-1].name]  # a continuation needs its predecessor's page
        for dep in depends:
            if dep not in names or dep == name:
                raise SuiteError(f"{where}: depends_on {dep!r} must name an earlier scenario")
        budget = {**suite_budget, **(item.get("budget") or {})}
        if int(budget["actions"]) > 60:
            raise SuiteError(f"{where}: budget.actions is capped at 60 (Jev's own limit)")
        scenarios.append(Scenario(
            name=name,
            url=url,
            goal=goal.strip() if isinstance(goal, str) else None,
            expect=expect,
            settle=float(item.get("settle", data.get("settle", 10))),
            budget={"actions": int(budget["actions"]), "seconds": float(budget["seconds"])},
            before=_hooks(item.get("before"), f"{where}.before"),
            after=_hooks(item.get("after"), f"{where}.after"),
            depends_on=depends,
            mode=mode,
            device=_device(item.get("device", data.get("device") or wanted[0]), where),
            persona=item.get("persona", data.get("persona")),
            speech=item.get("speech", data.get("speech")),
            vision=bool(item.get("vision", data.get("vision", False))),
            about=about(item.get("about"), f"{where}.about"),
            clients=clients,
            steps=_steps(item.get("steps"), [c["name"] for c in clients], f"{where}.steps") if clients else [],
            state=item.get("state"),
            seed=item.get("seed"),
        ))
        if any(s["kind"] == "snapshot" for s in scenarios[-1].steps) and not item.get("state"):
            raise SuiteError(f"{where}: a snapshot reads each client's state: give state: <JS expression>")

    copies = []
    for device in wanted[1:]:  # the same scenarios again on each further device, each with its own chain
        label = device if isinstance(device, str) else f"{device}"
        moved = {s.name for s in scenarios if s.name not in pinned}
        for s in scenarios:
            if s.name in pinned:
                continue
            copies.append(Scenario(**{**s.__dict__, "name": f"{s.name} ({label})", "device": _device(device, "devices"),
                                      "depends_on": [f"{d} ({label})" if d in moved else d for d in s.depends_on]}))
    scenarios += copies

    hosts = {host_of(s.url) for s in scenarios if s.url}
    if base:
        hosts.add(host_of(base))
    hosts |= set(_list(data.get("hosts"), "hosts"))
    account = _account(data["account"], base) if data.get("account") else None
    if account:
        hosts.add(host_of(account["login"]["url"]))
    suite = Suite(
        name=str(data.get("name") or (path.stem if path else "qajev")),
        base_url=base,
        hosts=sorted(hosts),
        guard=guard,
        cost_cap_usd=float(data.get("cost_cap_usd", 1.0)),
        scenarios=scenarios,
        path=path,
        motion=motion,
        real_devices=_real_devices(data.get("real_devices") or os.environ.get("QAJEV_REAL_DEVICES")),
        account=account,
        about=about(data.get("about"), "about"),
    )
    check_safety(suite)
    return suite


def about(value, where):
    """`about`: what a test (or a run) proves and why, in plain words. -> the text, or None."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise SuiteError(f"{where} must be text: what the test proves and why")
    return " ".join(value.split()) or None


def check_safety(suite):
    """Mutating runs are loopback-only, with no exceptions and no override flag."""
    for s in suite.scenarios:
        if s.mode != "mutate":
            continue
        remote = [h for h in suite.hosts if not is_loopback(f"http://{h}")]
        if remote:
            raise SuiteError(
                f"scenario {s.name!r} is mode: mutate, but the suite reaches non-loopback host(s) {remote}. "
                "Mutating runs go against localhost and a throwaway database only."
            )
    if suite.guard.get("allow_secret_fields") and any(not is_loopback(f"http://{h}") for h in suite.hosts):
        raise SuiteError("guard.allow_secret_fields is only allowed when every host is loopback")


def load(path, devices=None):
    path = Path(path)
    if not path.is_file():
        raise SuiteError(f"suite not found: {path}")
    text = path.read_text()
    if path.suffix == ".json":
        data = json.loads(text)
    else:
        import yaml  # deferred: keeps `qajev --help` fast

        data = yaml.safe_load(text)
    return parse(data, path, devices=devices)


def loads(text, name="inline", devices=None):
    text = text.strip()
    if text.startswith("{"):
        data = json.loads(text)
    else:
        import yaml

        data = yaml.safe_load(text)
    suite = parse(data, devices=devices)
    if suite.name == "qajev":
        suite.name = name
    return suite
