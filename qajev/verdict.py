"""Judging a scenario. Pure functions: no browser, no model.

Jev's DONE / BLOCKED is a hint, never the verdict. The page and side-effect checks decide; the stop reason
only says who is to blame when they fail (the product, or the harness).
"""

import re
from urllib.parse import urlsplit

OUTCOMES = ("pass", "fail", "stuck", "harness", "unverified", "skipped")
# Stop reasons that mean the harness, not the product, ended the scenario.
HARNESS_STOPS = {
    "budget_actions", "budget_seconds", "stale", "cost_cap", "guard_missing", "model_error", "browser_error",
    "left_site", "hook_failed", "interrupted", "machine_busy",
}
PRODUCT_STOPS = {"done", "reached", "checked", "unreachable"}
SEVERITY_ORDER = {"S1": 0, "S2": 1, "S3": 2}
BLANK_MS = 10_000


def page_checks(expect, observed):
    """Checks that can be read off the page. `observed` comes from session.PROBE_JS."""
    checks = []
    url = observed.get("url") or ""
    if expect.get("url"):
        checks.append(_check(f"url contains {expect['url']!r}", expect["url"] in url, url))
    if expect.get("url_regex"):
        checks.append(_check(f"url matches /{expect['url_regex']}/", bool(re.search(expect["url_regex"], url)), url))
    if expect.get("status"):
        have = observed.get("status")
        checks.append(_check(f"the page answered HTTP {expect['status']}", have == expect["status"],
                             None if have == expect["status"] else f"HTTP {have}"))
    case = " (any case)" if expect.get("ignore_case") else ""
    near = observed.get("near") or []
    for i, (needle, seen) in enumerate(zip(expect.get("text", []), observed.get("text") or [])):
        closest = near[i] if i < len(near) else None
        miss = f"closest on the page: {closest}" if closest else f"not on the page; it begins: {observed.get('says')}"
        checks.append(_check(f"page shows {needle!r}{case}", bool(seen), None if seen else miss))
    for needle, seen in zip(expect.get("absent", []), observed.get("absent") or []):
        checks.append(_check(f"page lacks {needle!r}{case}", not seen, "still shown" if seen else None))
    for needle, seen in zip(expect.get("visible", []), observed.get("visible") or []):
        checks.append(_check(f"on screen: {needle!r}{case}", bool(seen),
                             None if seen else "in the page but not visible in the viewport, or absent"))
    if expect.get("js"):
        value = observed.get("js")
        ok = value is True
        checks.append(_check(f"js {expect['js']!r}", ok, None if ok else f"returned {value!r}"))
    return checks


def _check(name, ok, detail=None):
    return {"check": name, "ok": bool(ok), "detail": detail}


def fetch_check(probe, result):
    want = int(probe.get("status", 200))
    status = result.get("status")
    ok = status == want
    detail = f"HTTP {status}" if status is not None else result.get("error")
    if ok and probe.get("contains"):
        ok = probe["contains"] in (result.get("body") or "")
        detail = None if ok else f"body lacks {probe['contains']!r}"
    return _check(f"fetch {probe.get('method', 'GET')} {probe['url']} -> {want}", ok, detail if not ok else None)


def command_check(spec, result):
    ok = result.get("exit") == int(spec.get("exit", 0))
    detail = None if ok else f"exit {result.get('exit')}: {(result.get('stderr') or '').strip()[:200]}"
    if ok and "stdout" in spec:
        ok = str(spec["stdout"]) in (result.get("stdout") or "")
        detail = None if ok else f"stdout {(result.get('stdout') or '').strip()[:200]!r} lacks {spec['stdout']!r}"
    return _check(f"command {spec['run']!r}", ok, detail)


def all_ok(checks):
    return bool(checks) and all(c["ok"] for c in checks)


def classify(stop, checks, *, has_checks, stop_detail=None) -> tuple[str, str]:
    """-> (outcome, reason)."""
    failed = [c for c in checks if not c["ok"]]
    why = "; ".join(f"{c['check']}" + (f" ({c['detail']})" if c.get("detail") else "") for c in failed)
    if stop == "skipped":
        return "skipped", stop_detail or "skipped"
    if stop == "unreachable":
        return "fail", f"page did not load: {stop_detail}"
    if not has_checks:
        if stop in HARNESS_STOPS:
            return "harness", _stop_text(stop, stop_detail)
        return "unverified", f"no expectations to check; Jev ended with {stop}"
    if all_ok(checks):
        tail = f" (Jev ended with {stop})" if stop not in {"reached", "checked"} else ""
        return "pass", f"all {len(checks)} check(s) passed{tail}"
    if stop == "done":
        return "fail", f"Jev reported DONE but: {why}"
    if stop == "reached":
        return "fail", f"page reached but: {why}"
    if stop == "checked":
        return "fail", why
    if stop == "blocked":
        return "stuck", f"Jev found no way forward; unmet: {why}"
    return "harness", f"{_stop_text(stop, stop_detail)}; unmet: {why}"


def sign_in_wall(start_url, observed, *, account=None, profile=None):
    """The page a scenario ended on asks to sign in, and it is not the page the scenario set out to test (a scenario
    that starts on /login tests the sign-in page itself). -> {"url", "reason", "account"?, "profile"?} or None.
    Says nothing about the product: the run could not get past the door. If that page should be public, it is a bug."""
    probe = (observed or {}).get("probe") or {}
    end = (observed or {}).get("url")
    if not probe.get("sign_in") or not end:
        return None
    if start_url and urlsplit(start_url).path.rstrip("/") == urlsplit(end).path.rstrip("/"):
        return None
    where = urlsplit(end)
    page = f"{where.netloc}{where.path}"
    if account:
        why = f"signed in as account {account}, but that account cannot see this page"
    elif profile:
        why = f"profile {profile}'s sign-in did not hold (sign in to it again)"
    else:
        why = "not signed in"
    out = {"url": end, "reason": f"needs sign-in: the run ended on a sign-in page ({page}); {why}"}
    if account:
        out["account"] = account
    if profile:
        out["profile"] = profile
    return out


def sign_in_next_step(walls):
    """What an agent (or a person) does about scenarios or pages that met a sign-in page. -> the report's
    `needs_sign_in`: the pages, and the next step in words, the password never part of it."""
    pages = sorted({w["url"] for w in walls})
    login = pages[0]
    held = sorted({w["account"] for w in walls if w.get("account")})
    profiles = sorted({w["profile"] for w in walls if w.get("profile")})
    if held:
        step = (f"The run signed in as account {', '.join(held)}, and still met a sign-in page: ask the person "
                "whether that account should see these pages, or which account should.")
    elif profiles:
        step = (f"Profile {', '.join(profiles)} is no longer signed in there: ask the person to sign in to it again "
                f"(qa_browser action=login, profile={profiles[0]}, url={login}; CLI: qajev browser login --profile "
                f"{profiles[0]} --url {login}).")
    else:
        step = ("Ask the person how QAJev should get in, and never ask for, type or write down the password. Either "
                f"they sign in once in QAJev's own browser (qa_browser action=login, profile=NAME, url={login}; CLI: "
                f"qajev browser login --profile NAME --url {login}) and runs use profile NAME; or they keep a test "
                "account and run, in their own terminal (in Claude Code, after a !): qajev account add NAME --email "
                f"EMAIL --login-url {login} [--project PROJECT]. The Keychain asks them for the password, QAJev "
                "proves the sign-in, and the suite's account: block (or the project's account) does the rest.")
    return {"pages": pages, "next_step": step}


def _stop_text(stop, detail) -> str:
    text = {
        "budget_actions": "action budget spent", "budget_seconds": "time budget spent",
        "stale": "decisions went stale (page kept changing)", "cost_cap": "cost cap reached",
        "guard_missing": "guard not present on the page (fail closed)", "model_error": "model call failed",
        "browser_error": "browser/daemon error", "left_site": "left the allowed hosts",
        "hook_failed": "hook failed", "interrupted": "interrupted", "machine_busy": "machine too busy",
    }.get(stop, str(stop))
    return f"{text}: {detail}" if detail else text


def document_findings(status, expect, *, scenario, url):
    """The page's own HTTP error, unless the test set out to see that status (a removed page's 404)."""
    if not status or status < 400 or status == (expect or {}).get("status"):
        return []
    return [_finding("S1" if status >= 500 else "S2", f"HTTP {status}", "document response", scenario, url)]


def findings_from_probe(probe, *, scenario, url, first_party_hosts):
    """Product signals (errors, failed requests, blank screens) independent of the scenario outcome."""
    out = []
    for err in (probe or {}).get("errors", []):
        kind, detail = err.get("kind"), str(err.get("detail", ""))[:300]
        host = urlsplit(detail).netloc if detail.startswith("http") else None
        third_party = bool(host) and host not in first_party_hosts
        if kind in {"exception", "rejection"}:
            if "QAJev read-only" in detail:
                continue  # our own write block surfacing through the page
            out.append(_finding("S2", "page error", detail, scenario, url))
        elif kind == "http":
            if third_party:
                continue
            status = int(err.get("status", 0))
            out.append(_finding("S2" if status >= 500 else "S3", f"HTTP {status}", detail, scenario, url))
        elif kind == "resource" and not third_party:
            out.append(_finding("S3", "failed to load", detail, scenario, url))
        elif kind == "console":
            out.append(_finding("S3", "console error", detail, scenario, url))
        elif kind == "csp":  # third-party too: a blocked tag or pixel is the site's own policy at work
            what = f"{err.get('directive')}: {detail}"
            if err.get("disposition") == "report":
                out.append(_finding("S3", "CSP violation (report-only)", what, scenario, url))
            else:
                out.append(_finding("S2", "blocked by CSP", what, scenario, url))
    return out


def blank_finding(longest_ms, *, scenario, url):
    """One finding per scenario for the longest blank/spinner spell, not one per observation."""
    if longest_ms >= BLANK_MS:
        return [_finding("S2", "blank or spinner over 10 s", f"longest {longest_ms / 1000:.1f} s", scenario, url)]
    return []


def _finding(severity, kind, detail, scenario, url):
    return {"severity": severity, "kind": kind, "detail": detail, "scenario": scenario, "url": url}


def dedupe(findings):
    seen, out = set(), []
    for f in findings:
        key = (f["severity"], f["kind"], f["detail"], f["scenario"])
        if key not in seen:
            seen.add(key)
            out.append(f)
    return sorted(out, key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))


def screens(decisions):
    """Per decision: what Jev chose, how sure, and the runner-up ("one obvious next step?")."""
    out = []
    for i, d in enumerate(decisions, 1):
        operation = d.get("operation")
        head = f"{(operation or '').lower()}_target"
        criteria = (((d.get("request") or {}).get("questions") or {}).get(head) or {}).get("criteria") or {}
        probs = d.get("target_probabilities") or d.get("operation_probabilities") or {}
        ranked = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)
        name = (lambda k: (criteria.get(k) or {}).get("element", k)) if criteria else (lambda k: k)
        chosen = d.get("target") or operation
        runner = next(((k, p) for k, p in ranked if k != chosen), (None, None))
        out.append({
            "step": i,
            "operation": operation,
            "next_step": name(chosen) if chosen else operation,
            "p": round(probs.get(chosen, 0.0), 3) if chosen in probs else None,
            "runner_up": name(runner[0]) if runner[0] else None,
            "runner_up_p": round(runner[1], 3) if runner[1] is not None else None,
            "confidence": d.get("confidence"),
            "options": len(criteria) or len(d.get("operation_probabilities") or {}) or None,
            "ms": d.get("latency_ms"),
        })
    return out


def gate(outcomes, strict=False):
    """PASS only when every scenario passed; FAIL on any product failure; otherwise INCOMPLETE."""
    if not outcomes:
        return "INCOMPLETE"
    if "fail" in outcomes or (strict and "stuck" in outcomes):
        return "FAIL"
    if all(o == "pass" for o in outcomes):
        return "PASS"
    return "INCOMPLETE"


EXIT_CODES = {"PASS": 0, "FAIL": 1, "INCOMPLETE": 2}
