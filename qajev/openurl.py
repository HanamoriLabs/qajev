"""Open URL: a play step that opens a URL the app opened in QAJev's own Chrome, and checks that page (Twinbox, 14 Oct).

An app that sends its person to a web page (a Stripe checkout, a sign-in, a help page) records the URL in its state.
The step reads it there, refuses it unless it matches `url_regex`, then checks the page as a suite's scenario would:

    - name: The checkout is a test session for Twinbox Hobby
      about: The app opened a Stripe test checkout for the Hobby plan at $10.00 a month.
      open_url:
        from: opened_url                                       # the state field with the URL (a.b for a nested one)
        url_regex: ^https://checkout\\.stripe\\.com/c/pay/cs_test_  # the URL must match it, or nothing opens
        says: the app opened a Stripe test checkout             # what the URL check proves (optional)
        expect:                                                 # a scenario's expect: text, js, url_regex, says...
          text: ["Twinbox Hobby: 3,700 credits a month", "$10.00"]
        guard:                                                  # a suite's guard (optional)
          allow_requests: ["^https://api\\.stripe\\.com/v1/payment_pages/{last}/init$"]
        settle: 3

`{last}` in `guard.allow_requests` is the URL path's last part, escaped: one session's own request, never another's.
The page check is a whole QAJev run of one scenario (`qajev run`, desktop, a throwaway profile), with every rule a
suite has: production stays read-only, and a POST goes only where `allow_requests` names it. Its report sits in the
play run's folder. The play run already holds this machine's browser slot, so the inner run takes it from its parent.
"""

import contextlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .config import user_info_refusal
from .native import NativeError, _step, _step_result

SPEC_KEYS = {"from", "url_regex", "says", "expect", "guard", "settle"}
# The only guard key an open_url step may set. Payment and password fields stay disabled, the page stays read-only,
# and each allowed request is one https address pinned to this URL's own {last} (the Orchestrator, 14 Oct).
GUARD_KEYS = {"allow_requests"}
OTHER_ACTS = ("goal", "play", "js", "pad", "keys", "idle", "crash_renderer", "relaunch", "handoff")
PROBE_URL = "https://example.invalid/c/pay/probe"
TIMEOUT_S = 600


def _scenario(spec, name, about, url):
    """The one-scenario suite the step runs, as `qajev run` reads it."""
    last = re.escape(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    guard = dict(spec.get("guard") or {})
    if guard.get("allow_requests"):
        guard["allow_requests"] = [str(p).replace("{last}", last) for p in guard["allow_requests"]]
    return {"name": name, "about": about, "device": "desktop", "settle": spec.get("settle", 3),
            **({"guard": guard} if guard else {}),
            "scenarios": [{"name": name, "about": about, "url": url, "expect": spec["expect"]}]}


def check_open_url(steps):
    """Each open_url step, checked when the suite loads: NativeError names the step and what is wrong."""
    from .suite import SuiteError, parse

    for i, step in enumerate(steps or []):
        if not isinstance(step, dict) or step.get("open_url") is None:
            continue
        name = step.get("name") or f"step {i + 1}"
        where = f"step {name!r}: open_url"
        spec = step["open_url"]
        if not isinstance(spec, dict):
            raise NativeError(f"{where}: needs from:, url_regex: and expect:")
        unknown = sorted(set(spec) - SPEC_KEYS)
        if unknown:
            raise NativeError(f"{where}: unknown key(s) {unknown}; known: {sorted(SPEC_KEYS)}")
        if not isinstance(spec.get("from"), str) or not spec["from"].strip():
            raise NativeError(f"{where}.from: the app's state field that holds the URL, such as opened_url")
        if not isinstance(spec.get("url_regex"), str) or not spec["url_regex"].startswith("^https://"):
            raise NativeError(f"{where}.url_regex: a regular expression from ^https:// to the site and path the URL "
                              "must have (nothing opens unless it matches)")
        try:
            re.compile(spec["url_regex"])
        except re.error as e:
            raise NativeError(f"{where}.url_regex: {e}") from None
        guard = spec.get("guard") or {}
        if not isinstance(guard, dict) or set(guard) - GUARD_KEYS:
            raise NativeError(f"{where}.guard: takes allow_requests only; payment fields stay disabled and the page "
                              "stays read-only")
        for pattern in guard.get("allow_requests") or []:
            if not (isinstance(pattern, str) and pattern.startswith("^https://") and pattern.endswith("$")
                    and "{last}" in pattern):
                raise NativeError(f"{where}.guard.allow_requests: {pattern!r} must be one https address from ^ to $ "
                                  "with {last} in it, so it lets through only this URL's own request")
        if not isinstance(spec.get("expect"), dict) or not spec["expect"]:
            raise NativeError(f"{where}.expect: what the page must show (text, js, url_regex...)")
        try:
            parse(_scenario(spec, name, step.get("about") or name, PROBE_URL))
        except SuiteError as e:
            raise NativeError(f"{where}: {e}") from None
        acting = [k for k in OTHER_ACTS if step.get(k)]
        if acting:
            raise NativeError(f"step {name!r}: an open_url step only checks a page; put {', '.join(acting)} in a "
                              "step of its own")
        if step.get("expect"):
            raise NativeError(f"step {name!r}: an open_url step's checks go in open_url.expect; put the app's "
                              "expect: in a step of its own")


def _value(state, path):
    for part in path.split("."):
        if not isinstance(state, dict) or part not in state:
            return None
        state = state[part]
    return state


def _run(args, timeout=TIMEOUT_S):
    """-> (exit code or None, output). The inner run's own env, plus the parent's pid for the browser slot."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "QAJEV_DEVICES": "desktop"})
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    return r.returncode, (r.stdout + r.stderr)[-2000:]


def open_url_step(game, *, name, spec, about=None, run_dir=None, emit=None, run=None):
    """Read the URL from the app's state, check it against url_regex, then run the page check in QAJev's Chrome."""
    run = run or _run
    want = spec["url_regex"]
    says = spec.get("says") or f"the app opened a URL matching {want}"
    try:
        obs = game.observe()
    except NativeError as e:
        return _step_result(name, "browser_error", [], f"could not read the app's state: {e}")
    url = _value(obs.get("state") or {}, spec["from"])
    ok = isinstance(url, str) and re.search(want, url) is not None
    shown = url if url is None or not isinstance(url, str) else url[:120]
    detail = None if ok else f"{spec['from']} was {shown!r}"
    if ok and (urlsplit(url).scheme != "https" or user_info_refusal(url)):  # whatever url_regex says
        ok, detail = False, user_info_refusal(url) or f"{spec['from']} is not an https URL"
    first = {"check": f"state {spec['from']} matches {want}", "says": says, "ok": ok, "detail": detail}
    if not ok:
        r = _step_result(name, "reached", [first])
        r["reason"] += "; nothing was opened"
        return r
    url = str(url)
    _step(emit, name, None, f"opening {spec['from']} in QAJev's Chrome: {urlsplit(url).netloc}")
    folder = Path(run_dir or ".") / "open-url" / re.sub(r"[^A-Za-z0-9._-]+", "-", name)[:60]
    folder.mkdir(parents=True, exist_ok=True)
    suite = folder / "suite.yaml"
    suite.write_text(yaml.safe_dump(_scenario(spec, name, about or name, url), sort_keys=False))
    code, out = run([sys.executable, "-m", "qajev", "run", str(suite), "--json", "--ephemeral", "--headless",
                     "--jobs", "1", "--runner", "local", "--out", str(folder)])  # on this machine, beside the app
    reports = sorted(folder.glob("*/report.json"))
    report = {}
    with contextlib.suppress(OSError, ValueError, IndexError):
        report = json.loads(reports[-1].read_text())
    inner = (report.get("scenarios") or [{}])[0]
    if not inner.get("outcome"):
        tail = out.strip().splitlines()[-1][:300] if out.strip() else f"exit {code}"
        return _step_result(name, "browser_error", [first], f"the page check did not run: {tail}")
    checks = [first, *({**c, "check": f"page: {c['check']}"} for c in inner.get("checks") or [])]
    if inner["outcome"] in ("harness", "stuck", "unverified", "skipped"):
        r = _step_result(name, "browser_error", checks, f"the page check was {inner['outcome']}: {inner.get('reason')}")
    else:
        r = _step_result(name, "reached", checks)
    r["opened_url"] = {"host": urlsplit(url).netloc, "report": str(reports[-1])}
    return r
