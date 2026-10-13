"""Handoff: a step a person does by hand while the run waits (Twinbox's journey, 13 Oct).

Some steps are a person's alone: a GitHub device login, a sign-in to their own Claude account. QAJev never signs in
for anyone, so a play suite hands that step over and waits:

    - name: GitHub sign-in on the box
      about: The box's setup waits for its owner to authorize GitHub, then goes on by itself.
      handoff:
        who: José                                # the person who does it
        does: Authorize GitHub on the box with the code it shows
        until: {github_signed_in: true}          # the app's state once it is done (as a play step's until)
        minutes: 10                              # how long to wait, 1 to 30

The step sends no input at all, and takes no `expect:` or `open:` (refused when the suite loads). It tells the
run's watchers (a `handoff` event: the live page, `qajev top`, qa_job), then reads the app's state until `until`
holds. Done: pass. Not done in time: stuck, never fail, because a person who did not come proves nothing about the
product. QAJev never reads, types or records a code or a password here.
"""

import time

from .native import NativeError, _shown, _step, _step_result, _until_text, _until_value, compare

HANDOFF_KEYS = {"who", "does", "until", "minutes"}
MAX_MINUTES = 30
# A handoff step only waits: anything that would make QAJev act goes in a step of its own. `open` switches the app
# or page before the step, which is an action too (redteam on #139).
ACTING = ("goal", "play", "js", "pad", "idle", "crash_renderer", "relaunch", "open")


def check_handoff(steps):
    """Each handoff step's spec, checked when the suite loads. NativeError names the step and what is wrong."""
    for i, step in enumerate(steps or []):
        if not isinstance(step, dict) or "handoff" not in step:
            continue
        where = f"steps[{i}] ({step.get('name') or f'step {i + 1}'})"
        spec = step["handoff"]
        if not isinstance(spec, dict):
            raise NativeError(f"{where}: handoff: needs who:, does: and until:")
        unknown = sorted(set(spec) - HANDOFF_KEYS)
        if unknown:
            raise NativeError(f"{where}: handoff: unknown key(s) {unknown}; known: {sorted(HANDOFF_KEYS)}")
        for key in ("who", "does"):
            if not isinstance(spec.get(key), str) or not spec[key].strip():
                raise NativeError(f"{where}: handoff.{key}: " + ("the person who does it" if key == "who" else
                                                                 "what they do, in plain words"))
        until = spec.get("until")
        if not isinstance(until, dict) or not until:
            raise NativeError(f"{where}: handoff.until: the app's state once it is done, e.g. {{signed_in: true}}")
        try:
            minutes = float(spec.get("minutes", 10))
        except (TypeError, ValueError):
            minutes = 0
        if not 1 <= minutes <= MAX_MINUTES:
            raise NativeError(f"{where}: handoff.minutes: from 1 to {MAX_MINUTES}")
        acting = [k for k in ACTING if step.get(k)]
        if acting:
            raise NativeError(f"{where}: a handoff step only waits, and QAJev acts nowhere in it: put "
                              f"{', '.join(acting)} in a step of its own")
        if step.get("expect"):  # it would never be checked: until: is the step's one check (redteam on #139)
            raise NativeError(f"{where}: a handoff step's only check is handoff.until; put expect: in the next step")


def handoff_step(game, *, name, spec, emit=None, look=2.0, every=10.0):
    """Wait for a person to do `spec["does"]`, reading the app's state until `spec["until"]` holds. No input."""
    who, does, until = spec["who"].strip(), spec["does"].strip(), spec["until"]
    minutes = float(spec.get("minutes", 10))
    want = _until_text(until)
    check = f"{who} did it: {want}"
    record = {"who": who, "does": does, "minutes": minutes}
    if callable(emit):
        emit({"event": "handoff", "scenario": name, "who": who, "does": does, "minutes": minutes, "at": time.time()})
    _step(emit, name, None, f"waiting for {who}: {does}")
    started = told = time.monotonic()
    obs = {}
    while True:
        try:
            obs = game.observe()
        except NativeError as e:
            waited = time.monotonic() - started
            return {"name": name, "outcome": "harness", "stop": "browser_error", "checks": [], "screens": [],
                    "findings": [], "handoff": {**record, "waited_s": round(waited, 1)},
                    "reason": f"the app stopped answering after {waited:.0f} s of waiting for {who}: {e}"}
        waited = time.monotonic() - started
        if all(compare(_until_value(obs, k), v) for k, v in until.items()):
            return _step_result(name, "reached", [{"check": check, "ok": True, "detail": None}],
                                handoff={**record, "waited_s": round(waited, 1)})
        left = minutes * 60 - waited
        if left <= 0:
            break
        if time.monotonic() - told >= every:
            told = time.monotonic()
            _step(emit, name, None, f"waiting for {who}: {does} ({waited / 60:.0f} of {minutes:g} min)")
        time.sleep(min(look, left))
    seen = ", ".join(f"{k} was {_shown(_until_value(obs, k))}" for k in until)
    return {"name": name, "outcome": "stuck", "stop": "handoff_timeout", "screens": [], "findings": [],
            "checks": [{"check": check, "ok": False, "detail": f"not within {minutes:g} min: {seen}"}],
            "handoff": {**record, "waited_s": round(time.monotonic() - started, 1)},
            "reason": f"waited {minutes:g} min for {who} (“{does}”), and it was not done ({seen}). A person who "
                      "did not come says nothing about the product"}
