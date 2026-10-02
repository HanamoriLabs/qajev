"""Suite and scenario model. A suite is YAML or JSON; `qajev check` builds a one-scenario suite from flags."""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

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
HOOK_KINDS = {"js", "fill", "click", "navigate", "wait_for", "key", "sleep", "command"}
EXPECT_KEYS = {"url", "url_regex", "text", "absent", "visible", "js", "fetch", "command", "ignore_case"}
SCENARIO_KEYS = {
    "name", "url", "goal", "expect", "settle", "budget", "before", "after", "depends_on", "mode", "device",
    "persona", "speech",
}
SUITE_KEYS = {
    "name", "base_url", "mode", "persona", "device", "budget", "cost_cap_usd", "guard", "speech", "hosts",
    "scenarios", "settle", "motion", "devices", "real_devices", "account",
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
    return hooks


def _expect(value, where):
    value = value or {}
    if not isinstance(value, dict):
        raise SuiteError(f"{where} must be a mapping")
    _unknown(where, value, EXPECT_KEYS)
    out: dict[str, Any] = dict(value)
    out["text"] = _list(value.get("text"), f"{where}.text")
    out["absent"] = _list(value.get("absent"), f"{where}.absent")
    out["visible"] = _list(value.get("visible"), f"{where}.visible")
    if "url_regex" in out:
        try:
            re.compile(out["url_regex"])
        except re.error as e:
            raise SuiteError(f"{where}.url_regex is not a valid regex: {e}") from None
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


def page_checks(expect):
    return bool(expect.get("url") or expect.get("url_regex") or expect.get("text") or expect.get("absent")
                or expect.get("visible") or expect.get("js"))


def has_checks(expect):
    return page_checks(expect) or bool(expect.get("fetch") or expect.get("command"))


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
        if not goal and not has_checks(expect):
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
        ))

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
    )
    check_safety(suite)
    return suite


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
