"""The test plan: what each test proves, in plain words, before it runs, ticked as it does (José, 5 Oct: "a clear
instruction with checklist that get clicked as it passes those tests, that clearly state what is being tested,
otherwise it could be anything random").

One item per test: its `about`, then each check in plain words. A check's words are the suite's `says` for it, or,
for a check that describes itself (a text on the page, the address, an HTTP status), a sentence made from it. A check
only its author can describe (a `js` expression, a URL pattern, a shell command) without `says`, or a test without an
`about`, is NOT DESCRIBED: the plan flags it and the gate cannot be PASS, so a pass always means something stated.
"""

import re

from . import verdict

NOT_DESCRIBED = "NOT DESCRIBED"

# A check that describes itself -> its sentence. Checks only their author can describe have no entry here.
_SELF = [
    (r"page shows (['\"])(.*)\1( \(any case\))?$", lambda m: f"the page shows “{m[2]}”"),
    (r"page lacks (['\"])(.*)\1( \(any case\))?$", lambda m: f"the page does not show “{m[2]}”"),
    (r"on screen: (['\"])(.*)\1( \(any case\))?$", lambda m: f"“{m[2]}” is on screen"),
    (r"url contains (['\"])(.*)\1$", lambda m: f"the address contains “{m[2]}”"),
    (r"the page answered HTTP (\d+)$", lambda m: f"the page answers with HTTP {m[1]}"),
    (r"looks: (.*)$", lambda m: f"on the screenshot: {m[1]}"),
    (r"reached (.*)$", lambda m: f"the game reaches {m[1]} in time"),
    (r"fetch (\w+) (\S+) -> (.*)$", lambda m: f"{m[1]} {m[2]} answers {m[3]}"),
    (r"(game|.+?) shows (['\"])(.*)\2$", lambda m: f"the {m[1]} shows “{m[3]}”"),
    (r"screen is (.*)$", lambda m: f"the screen is {m[1]}"),
    (r"state (\S+) (.*)$", lambda m: f"the game's {m[1]} is {m[2]}"),
    (r"at least (\d+) fps$", lambda m: f"the game runs at {m[1]} frames a second or more"),
    (r"(frame rate held: .*|memory grew at most .*)$", lambda m: m[1]),
    (r"(\d+) link\(s\) answer with HTTP < 400$", lambda m: f"its {m[1]} link(s) answer without an error"),
    (r"(document loads with HTTP < 400)$", lambda m: "the page loads without an HTTP error"),
    (r"(link answers with HTTP < 400)$", lambda m: "the link answers without an HTTP error"),
    (r"(no uncaught script errors|no engine or script errors)$", lambda m: "no script errors"),
    (r"(the app is still running|the app kept running|the game closed|the game started again on the same saves"
     r"|the game reported no problems \(its own checks\)|the game kept running \(no soft-lock\)|the renderer crashed"
     r"|the script ran)$", lambda m: m[1]),
    (r"(.+) made every decision \(strict_decisions\)$", lambda m: f"{m[1]} made every decision"),
]


def words(check):
    """A check's plain words: its `says`, or the sentence for a check that describes itself, or None (only its author
    can say what it proves). A multiplayer check's prefix ("[p1] ", "across: ", "snapshot x: ") is kept in front."""
    if check.get("says"):
        return str(check["says"])
    name = str(check.get("check") or "")
    m = re.match(r"(\[[^\]]+\] |across: |snapshot [^:]+: |at the start: )(.*)$", name, re.S)
    if m:
        inner = words({"check": m[2]})
        return f"{m[1]}{inner}" if inner else None
    for pattern, say in _SELF:
        hit = re.match(pattern, name, re.S)
        if hit:
            return say(hit)
    return None


def state(outcome):
    """A plan item's box: pass ticks green, fail is a red cross, stuck and harness are amber, else not run yet."""
    return {"pass": "pass", "fail": "fail", "stuck": "warn", "harness": "warn"}.get(outcome or "", "todo")


def item(n, name, about, checks, outcome=None):
    lines = [{"words": words(c), "check": c.get("check"), **({"ok": c["ok"]} if "ok" in c else {})} for c in checks]
    described = bool(about) and all(line["words"] for line in lines)
    return {"n": n, "name": name, "about": about or None, "checks": lines, "described": described,
            "state": state(outcome)}


def from_suite(scenarios):
    """The plan before the run, from the suite: each test with the checks it will run, none ticked yet. A multiplayer
    test's page checks run on every client ("[p1] ..."), and its snapshot steps check across them."""
    out = []
    for i, s in enumerate(scenarios, 1):
        checks = verdict.planned(s.expect)
        if s.clients:
            page = [c for c in checks if not c["check"].startswith(("across: ", "fetch ", "command "))]
            rest = [c for c in checks if c not in page]
            checks = [{**c, "check": f"[{cl['name']}] {c['check']}"} for cl in s.clients for c in page] + rest
            for step in s.steps:
                for c in step.get("expect") or [] if step.get("kind") == "snapshot" else []:
                    checks.append({"check": f"snapshot {step['name']}: {c['check']}",
                                   **({"says": c["says"]} if c.get("says") else {})})
        out.append(item(i, s.name, s.about, checks))
    return out


def from_steps(steps):
    """The plan of a game's `steps:` suite before it runs: each step with the checks its `expect` and `play.until` will
    make, named as native.py names them (they describe themselves; a step still needs its `about`)."""
    from .native import _until_text, before_names, judged_words, lasted_words

    out = []
    for i, step in enumerate(steps, 1):
        step = step if isinstance(step, dict) else {}
        expect, checks = step.get("expect") or {}, []
        if "screen" in expect:
            checks.append(f"screen is {expect['screen']}")
        checks += [f"game shows '{t}'" for t in expect.get("text") or []]
        checks += [f"state {k} {v}" for k, v in (expect.get("state") or {}).items()]
        if expect.get("min_fps") is not None:
            checks.append(f"at least {expect['min_fps']} fps")
        if expect.get("max_memory_growth_mb") is not None:
            checks.append(f"memory grew at most {expect['max_memory_growth_mb']} MB")
        if expect and expect.get("no_errors", True):
            checks.append("no engine or script errors")
        if expect.get("closed"):
            checks.append("the game closed")
        until = (step.get("play") or {}).get("until") if isinstance(step.get("play"), dict) else None
        if until:
            checks.insert(0, f"reached {_until_text(until)}")
        planned = [{"check": c} for c in checks]
        if step.get("pad") is not None:  # the input first, then the checks of what it did
            from .pad import describe

            planned.insert(0, {"check": "the pad input was sent", "says": describe(step["pad"])})
        before = step.get("before") if isinstance(step.get("before"), dict) else None
        if before and isinstance(step.get("lasted"), dict):  # checked first, then the idle's measured window
            planned.insert(0, lasted_words(before, step["lasted"]))
        if before:
            planned[:0] = [{"check": f"at the start: {c}"} for c in before_names(before)]
        judges = step.get("judged_by")
        if judges:  # the later steps that prove it (native.check_judged_by), in the words the result will carry
            judges = [judges] if isinstance(judges, str) else list(judges)
            planned.append({"check": judged_words(judges), "says": f"Jev acted, and the later steps "
                                                                   f"{', '.join(judges)} passed"})
        out.append(item(i, step.get("name") or f"step {i}", step.get("about"), planned))
    return out


def merge(planned, results):
    """The plan as a run goes: a finished test shows its own checks and box; the others stay as planned."""
    done = {r.get("name"): r for r in results}
    out = []
    for it in planned:
        r = done.get(it["name"])
        out.append(item(it["n"], it["name"], it["about"], r.get("checks") or [], r.get("outcome")) if r else it)
    return out


def from_results(scenarios):
    """The plan of a finished (or running) run, from its results: each test with its outcome and its checks."""
    return [item(i, s.get("name"), s.get("about"), s.get("checks") or [], s.get("outcome"))
            for i, s in enumerate(scenarios, 1)]


def not_described(plan):
    """The tests the plan flags: no `about`, or a check without plain words. -> [name]"""
    return [it["name"] for it in plan if not it["described"]]


def proved(it):
    """What a passed test proved, in plain words: its `about`, then each ticked check's words."""
    ticked = [line["words"] for line in it["checks"] if line.get("ok") and line["words"]]
    return " ".join([it["about"] or ""] + [f"✓ {w}." for w in ticked]).strip()
