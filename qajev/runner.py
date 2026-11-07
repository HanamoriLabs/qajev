"""Runs a suite: one guarded tab per worker, scenarios in order, a verdict per scenario.

Parallelism is one process per worker: browser_harness reads BU_NAME at import, so a process can only
own one daemon. Every worker opens its own tab in the same QAJev Chrome and stops its own daemon.
"""

import base64
import binascii
import contextlib
import dataclasses
import json
import os
import re
import signal
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import browser_pool, chrome, keys, live, plan, providers, ux, verdict, vision
from . import pad as pad_mod
from .config import redact, redact_tree, remember_secret, remembered_secrets, secret_values
from .diagnostics import clean_data, keep_console
from .ledger import CostCapReached, Ledger
from .suite import has_checks, sweep_hooks
from .suite import page_checks as has_page_checks
from .ux_options import resolve as resolve_ux

MODEL_ERROR = re.compile(r"model|typesafe|text helper|text_model|provider", re.I)
STALE_LIMIT = 8  # decisions without an executed action before we call the page churning
# Clicks on one target in a row that leave the page in at most 2 states (nothing happens, or it toggles back and forth)
# before the scenario stops as a loop. #802 (30 Oct): Jev clicked one box 9 and 11 times and spent the action budget.
LOOP_CLICKS = 3
# The expectations a run that stopped early may never reach, as its reason names them.
NOT_RUN = {"looks": "expect.looks", "after": "the after hooks", "fetch": "expect.fetch", "command": "expect.command",
           "effect": "expect.effect", "load": "expect.load", "input": "expect.input", "motion": "expect.motion",
           "sweep": "expect.sweep"}
EFFECT_MAX_BYTES = 32 * 1024 * 1024  # below the Cloud report-file limit, so required evidence can be transferred


class ConfigError(RuntimeError):
    pass


@dataclass
class Options:
    cdp_url: str | None = None
    profile: str = "default"
    headless: bool = False
    ephemeral: bool = False
    out_dir: Path = field(default_factory=lambda: Path("qajev-runs"))
    jobs: int = 1
    strict: bool = False
    allow_commands: bool = False
    typesafe_usd_per_call: float = 0.0005
    cost_cap_usd: float | None = None
    load_high: float | None = 150.0
    load_ok: float = 100.0
    load_wait: float = 600.0
    only: list = field(default_factory=list)
    only_exact: bool = False  # --exact: each --only name alone, not its other devices' copies (rerun --failed)
    shots: bool = True
    ux: str | None = None  # None inherits suite; default advisory. No model calls.
    ux_checks: list | None = None
    ux_keyboard_scope: str | None = None
    ux_keyboard_ratio: float | None = None
    ux_keyboard_main: str | None = None
    ux_keyboard_press_return: bool | None = None
    ux_settle_ms: int | None = None
    ux_settle_timeout_ms: int | None = None
    ux_shift_px: float | None = None
    ux_tap_min_px: float | None = None
    ux_max_findings: int | None = None
    motion: str | None = None  # overrides the suite's motion (reduce | full)
    color_scheme: str | None = None  # light | dark for every test that names none (a test's own wins)
    console_keep: tuple = ()  # patterns for every test, beside each test's own console_keep
    cache: str | None = None  # cold: every test runs on a cold cache (a test's own cache: cold also does)
    cpu_throttle: int | None = None  # overrides the suite's cpu_throttle (1 to 8)
    emit: object = None
    load_waited: float = 0.0  # seconds already spent waiting; load_wait is a budget for the whole run
    account: str | None = None  # the stored test account this run signed in with


def _emit(opts, event, **data):
    if callable(opts.emit):
        opts.emit({"event": event, **data})


def wait_for_quiet(opts):
    """The machine is shared: never add a browser run on top of a load spike. Waiting is never 'stuck'.
    load_wait is one budget for the run: once spent, later scenarios are skipped at once, not waited for again."""
    load = os.getloadavg()[0]
    if opts.load_high is None or load < opts.load_high:
        return True, load
    remaining = opts.load_wait - opts.load_waited
    if remaining <= 0:
        return False, load
    _emit(opts, "waiting", reason=f"load1 {load:.0f} >= {opts.load_high:.0f}; waiting for < {opts.load_ok:.0f} "
                                  f"(up to {remaining:.0f} s more this run)")
    started = time.monotonic()
    try:
        while load >= opts.load_ok:
            if time.monotonic() - started > remaining:
                return False, load
            time.sleep(5)
            load = os.getloadavg()[0]
    finally:
        opts.load_waited += time.monotonic() - started
        _emit(opts, "waited", seconds=round(time.monotonic() - started, 1), quiet=load < opts.load_ok)
    return True, load


def device_copies(scenarios, name):
    """NAME and the copies suite.parse made of it for each further device ("NAME (phone)"): the same url and goal on
    another device. A scenario its author named "NAME (x)" is not one."""
    base = next(s for s in scenarios if s.name == name)
    return [name] + [s.name for s in scenarios
                     if s.name.startswith(f"{name} (") and s.name.endswith(")") and "(" not in s.name[len(name) + 2:-1]
                     and (s.url, s.goal) == (base.url, base.goal) and s.device != base.device]


def select(suite, only, exact=False):
    """--only NAME keeps NAME on every device (its "NAME (phone)" copy too), plus everything they depend on. A copy's
    own name ("NAME (phone)") keeps that copy alone, and `exact` (--exact) keeps each name alone, so rerun --failed
    does not run again a copy that passed. JSR price run A (30 Oct): --only ran the desktop copy only."""
    if not only:
        return list(suite.scenarios)
    by_name = {s.name: s for s in suite.scenarios}
    missing = [n for n in only if n not in by_name]
    if missing:
        raise ConfigError(f"no scenario named {missing}; have {list(by_name)}")
    keep, todo = set(), list(only) if exact else [copy for n in only for copy in device_copies(suite.scenarios, n)]
    while todo:
        name = todo.pop()
        if name not in keep:
            keep.add(name)
            todo.extend(by_name[name].depends_on)
    return [s for s in suite.scenarios if s.name in keep]


def groups(scenarios):
    """Connected chains: a scenario runs in the same worker (and tab) as everything it depends on."""
    parent = {s.name: s.name for s in scenarios}

    def find(n):
        while parent[n] != n:
            n = parent[n]
        return n

    for s in scenarios:
        for dep in s.depends_on:
            if dep in parent:
                parent[find(s.name)] = find(dep)
    out = {}
    for s in scenarios:
        out.setdefault(find(s.name), []).append(s)
    return list(out.values())


def slug(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-")[:80] or "scenario"


# --------------------------------------------------------------------------------------------------------------
# One scenario


def _effect_check(session, scenario, run_dir, url):
    import hashlib

    from .pixels import PixelError, compare
    from .session import HookThrew

    spec = scenario.expect["effect"]
    if run_dir is None:
        raise PixelError("effect comparison needs a report folder for its evidence")
    frames = {}
    for state in ("off", "on"):
        try:
            _run_hooks(session, f"effect {spec['name']!r}, {state}", spec[state], url, doing=False)
            frames[state] = session.capture_region_png(spec["region"])
            if not isinstance(frames[state], bytes) or len(frames[state]) > EFFECT_MAX_BYTES:
                raise PixelError("PNG capture is missing or exceeds the 32 MiB evidence limit")
        except HookThrew:  # a hook's throw, not a capture's trouble: the runner grades it (suite.hook_throw)
            raise
        except (RuntimeError, ValueError, OSError, TimeoutError) as e:
            raise PixelError(f"effect {spec['name']!r}, {state} capture: {e}") from None
    metrics, difference = compare(frames["off"], frames["on"], tolerance=spec["tolerance"],
                                  min_changed=spec["min_changed"])
    # Hash the complete scenario name: distinct names with the same filename slug must keep distinct evidence.
    stem = "effect-" + hashlib.sha256(scenario.name.encode()).hexdigest()[:16]
    evidence = {"name": spec["name"], "region": spec["region"], "metrics": metrics}
    for state, contents in (("off", frames["off"]), ("on", frames["on"]), ("difference", difference)):
        relative = f"shots/{stem}-{state}.png"
        path = run_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        evidence[state] = relative
    detail = (f"{metrics['changed']}/{metrics['total']} pixels changed (fraction {metrics['fraction']:.8g}); "
              f"minimum {metrics['min_changed']:.8g}, RGB tolerance {metrics['tolerance']}; "
              f"region {spec['region']['name']!r}")
    check = {**verdict.effect_planned(spec), "ok": metrics["ok"], "detail": detail, "value": metrics}
    return check, evidence


# Stops after which no later hook or check runs: the harness's, and a js hook that threw with hook_throw: fail
HALTS = verdict.HARNESS_STOPS | {"hook_check_failed"}


# The input may take at most this much longer than its no-input control (x 1.25, + 0.25 s) for the two changes to be
# compared: a longer input has more time to change by itself (redteam-1 M1, #275). The 0.25 s is a hook's own cost:
# a drag's start point and its press and release are not paced, and each event waits for the renderer. An input past
# this bound gets a second control after it, for as long as it took; past the bound of that one too, it is harness.
INPUT_TIME_RATIO, INPUT_TIME_SLACK_S = 1.25, 0.25


def _input_wait_s(hooks):
    """How long the no-input control waits: the time the input hooks plan to take (a drag's ms, a key's holds and
    intervals, a pad's frames, a react hook's for_s, a sleep), at least 0.2 s. A hook with no planned time (a click,
    a wait_for) counts 0; the time guard in _input_check then says when the input took longer."""
    planned = 0.0
    for hook in hooks:
        kind, value = next(iter(hook.items()))
        if kind == "drag":
            planned += value.get("ms", 400) / 1000
        elif kind == "sleep":
            planned += min(float(value), 30)
        elif kind == "key":
            p = keys.plan(value)
            planned += len(p["keys"]) * p["repeat"] * (p["hold_ms"] + p["interval_ms"]) / 1000
        elif kind == "pad":
            planned += sum(frame["ms"] for frame in pad_mod.plan(value)) / 1000
        elif kind == "react":
            planned += keys.react_plan(value)["for_s"]
    return max(planned, 0.2)


def _input_check(session, scenario, run_dir, url):
    """expect.input: how much the region changes with the test's input (its `do` hooks) against how much it changes in
    the same time with none, so a scene that animates by itself cannot pass for one that answers the mouse.
    -> (check, evidence)."""
    import hashlib

    from .pixels import PixelError, compare
    from .session import HookBroke, HookFailed

    spec = scenario.expect["input"]
    if run_dir is None:
        raise PixelError("input comparison needs a report folder for its evidence")
    wait = _input_wait_s(spec["do"])
    frames, took, shots, again = {}, {}, [0], False

    def capture(name):
        shots[0] += 1
        frames[name] = session.capture_region_png(spec["region"])

    try:
        capture("idle-before")
        started = time.monotonic()
        time.sleep(wait)
        capture("idle-after")
        took["idle"] = time.monotonic() - started
        capture("input-before")
        started = time.monotonic()
        try:
            _run_hooks(session, f"input {spec['name']!r}, do", spec["do"], url, doing=False)
        except HookBroke:  # a js hook's error names its place already
            raise
        except HookFailed as e:  # the input itself failed, as a before hook would (redteam-1 L1, #275)
            raise HookFailed(f"input {spec['name']!r}, the do hooks: {e}") from None
        capture("input-after")
        took["input"] = time.monotonic() - started
        if took["input"] > INPUT_TIME_RATIO * took["idle"] + INPUT_TIME_SLACK_S:
            # The input took longer than it planned, as on a slow machine (in the 0.4.47 cloud run a 300 ms drag of 15
            # moves took 1.15 s): measure the change with no input again, after the input, for as long as it took.
            # Motion the input leaves behind counts as change with no input. The first control stays: the input is
            # judged against the larger change of the two, so page motion that stops between them (an intro, a camera
            # easing to rest) cannot pass for the input (redteam-1 B1, #283).
            again = True
            capture("again-before")
            started = time.monotonic()
            time.sleep(took["input"])
            capture("again-after")
            took["again"] = time.monotonic() - started
    except HookFailed:  # a RuntimeError, but not a capture's
        raise
    except (RuntimeError, ValueError, OSError, TimeoutError) as e:
        raise PixelError(f"input {spec['name']!r}, capture {shots[0]} of {6 if again else 4}: {e}") from None
    for name, contents in frames.items():
        if not isinstance(contents, bytes) or len(contents) > EFFECT_MAX_BYTES:
            raise PixelError(f"input {spec['name']!r}, {name}: PNG capture is missing or exceeds the 32 MiB limit")
    # min_changed=1 only satisfies compare's bounds: the check's own rule is the margin below
    idle, idle_png = compare(frames["idle-before"], frames["idle-after"], tolerance=spec["tolerance"], min_changed=1)
    moved, moved_png = compare(frames["input-before"], frames["input-after"], tolerance=spec["tolerance"],
                               min_changed=1)
    differences = [("idle-difference", idle_png), ("input-difference", moved_png)]
    controls = {"before the input": idle["fraction"]}
    if again:
        second, second_png = compare(frames["again-before"], frames["again-after"], tolerance=spec["tolerance"],
                                     min_changed=1)
        differences.append(("again-difference", second_png))
        controls["after the input"] = second["fraction"]
    measured = moved["fraction"] - max(controls.values())
    if again:
        # The first control lasted only the planned time, and a page whose own change grows with time (a panel that
        # slides in, a fade, a pan that ends later) changes less in 0.2 s than in a 1.15 s input: the first control
        # counts at its rate over the input's time, at most the whole region (redteam-1 B2, #283).
        controls["before the input"] = scaled = min(1.0, idle["fraction"] * took["input"] / took["idle"])
    # the control with the larger change; on a tie, the one as long as the input (the last)
    control = max(reversed(controls), key=lambda name: controls[name])
    margin = moved["fraction"] - controls[control]
    ok = margin >= spec["min_changed"]
    # only the scaled first control fails it: the page's own change could explain the input's, so it is not judged
    unsure = again and not ok and measured >= spec["min_changed"]
    longer = took["input"] > INPUT_TIME_RATIO * took["again" if again else "idle"] + INPUT_TIME_SLACK_S
    stem = "input-" + hashlib.sha256(scenario.name.encode()).hexdigest()[:16]
    metrics = {"idle": {k: idle[k] for k in ("changed", "total", "fraction")} | {"seconds": round(took["idle"], 3)},
               "input": {k: moved[k] for k in ("changed", "total", "fraction")} | {"seconds": round(took["input"], 3)},
               "margin": margin, "min_changed": spec["min_changed"], "tolerance": spec["tolerance"],
               "control": control, "ok": ok and not longer}
    if again:
        metrics["idle_again"] = {k: second[k] for k in ("changed", "total", "fraction")} | {
            "seconds": round(took["again"], 3)}
        metrics["idle_scaled"] = {"fraction": scaled, "seconds": round(took["input"], 3)}
    evidence = {"name": spec["name"], "region": spec["region"], "metrics": metrics}
    for name, contents in (*frames.items(), *differences):
        relative = f"shots/{stem}-{name}.png"
        path = run_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        evidence[name] = relative
    none = f"{idle['fraction']:.2%} in {took['idle']:.2f} s"
    if again:
        none += (f" before the input ({scaled:.2%} at that rate over the input's {took['input']:.2f} s) and "
                 f"{second['fraction']:.2%} in {took['again']:.2f} s after it, judged against the larger ({control})")
    detail = (f"with the input {moved['fraction']:.2%} of pixels changed in {took['input']:.2f} s; with none {none}; "
              f"the input must add {spec['min_changed']:.2%}, it added {margin:.2%}")
    if longer:  # nothing was judged: even the control after the input was too short for the input's own time
        return {**verdict.input_planned(spec), "ok": False, "broke": True, "value": metrics,
                "detail": f"the input took {took['input']:.2f} s and the no-input control {took['again']:.2f} s, so "
                          f"their changes cannot be compared ({detail}); the control after the input ended before "
                          "its wait, so this machine's waits cannot be trusted"}, evidence
    if unsure:  # the measured controls pass it; at the first one's rate over the input's time the page could do it all
        return {**verdict.input_planned(spec), "ok": False, "broke": True, "value": metrics,
                "detail": f"the page changed by itself before the input ({idle['fraction']:.2%} in "
                          f"{took['idle']:.2f} s), and at that rate it could change {scaled:.2%} in the input's "
                          f"{took['input']:.2f} s, so the input's own change cannot be told apart ({detail}); against "
                          f"the controls as measured it added {measured:.2%}"}, evidence
    return {**verdict.input_planned(spec), "ok": ok, "detail": detail, "value": metrics}, evidence


SHEET_FRAMES = 6  # at most this many frames, evenly picked, on the sheet Clef judges


def frame_gaps(times):
    """The page's animation frame times -> {frames, seconds, fps, longest_ms, p95_ms}."""
    gaps = sorted(b - a for a, b in zip(times, times[1:], strict=False))
    span = (times[-1] - times[0]) / 1000 if len(times) > 1 else 0
    return {"frames": len(times), "seconds": round(span, 3), "fps": round((len(times) - 1) / span, 1) if span else 0,
            "longest_ms": round(gaps[-1], 1) if gaps else None,
            "p95_ms": round(gaps[min(len(gaps) - 1, int(len(gaps) * 0.95))], 1) if gaps else None}


def _all_flat_dark(frames):
    """pixels.flat_dark on each motion frame: -> the first frame's reading when every frame is one flat dark colour,
    else None."""
    from .pixels import flat_dark, grid_lumas

    readings = [flat_dark(grid_lumas(frame)) for frame in frames]
    return readings[0] if all(readings) else None


def _motion_check(session, scenario, run_dir, url, observed):
    """expect.motion: after its `do` hooks, the page's frame gaps over `seconds` with no capture (max_gap_ms), then
    `frames` captures of the region over `seconds`: each pair of frames in a row must change (min_changed) in at least
    min_moving of the pairs. Clef judges each looks statement on a sheet of up to SHEET_FRAMES of them (ghosting, a
    trail, an effect that never plays). -> (checks, evidence, why Clef failed or None)."""
    import hashlib

    from .pixels import PixelError, compare, sheet

    spec = scenario.expect["motion"]
    if run_dir is None:
        raise PixelError("motion capture needs a report folder for its evidence")
    planned = verdict.motion_planned(spec)
    _run_hooks(session, f"motion {spec['name']!r}, do", spec["do"], url, doing=False)
    checks, gaps = [], None
    if spec.get("max_gap_ms"):
        gaps = frame_gaps(session.frame_times(spec["seconds"] * 1000))
        ok = gaps["longest_ms"] is not None and gaps["longest_ms"] <= spec["max_gap_ms"]
        checks.append({**planned[1], "ok": ok, "value": gaps,
                       "detail": f"longest gap {gaps['longest_ms']} ms between two of the main thread's animation "
                                 f"frames, 95% of gaps within {gaps['p95_ms']} ms, {gaps['fps']} frames a second over "
                                 f"{gaps['seconds']} s"})
    frames, at = [], []
    interval = spec["seconds"] / (spec["frames"] - 1)
    started = time.monotonic()
    try:
        for i in range(spec["frames"]):
            time.sleep(max(0.0, started + i * interval - time.monotonic()))
            at.append(round(time.monotonic() - started, 3))
            frames.append(session.capture_region_png(spec["region"]))
    except (RuntimeError, ValueError, OSError, TimeoutError) as e:
        raise PixelError(f"motion {spec['name']!r}, capture {len(frames) + 1} of {spec['frames']}: {e}") from None
    if any(not isinstance(f, bytes) or len(f) > EFFECT_MAX_BYTES for f in frames):
        raise PixelError(f"motion {spec['name']!r}: a PNG capture is missing or exceeds the 32 MiB limit")
    pairs = [compare(a, b, tolerance=spec["tolerance"], min_changed=spec["min_changed"])[0]
             for a, b in zip(frames, frames[1:], strict=False)]
    moving = sum(1 for m in pairs if m["ok"])
    still, run = 0, 0
    for m in pairs:  # the longest run of pairs in a row with no motion: a frozen stretch
        run = 0 if m["ok"] else run + 1
        still = max(still, run)
    share = moving / len(pairs)
    stem = "motion-" + hashlib.sha256(scenario.name.encode()).hexdigest()[:16]
    paths = []
    for i, contents in enumerate(frames):
        relative = f"shots/{stem}-{i:02d}.png"
        (run_dir / relative).parent.mkdir(parents=True, exist_ok=True)
        (run_dir / relative).write_bytes(contents)
        paths.append(relative)
    metrics = {"pairs": [round(m["fraction"], 6) for m in pairs], "moving": moving, "still_run": still,
               "share": share, "at": at, "min_changed": spec["min_changed"], "min_moving": spec["min_moving"],
               "tolerance": spec["tolerance"], **({"gaps": gaps} if gaps else {})}
    evidence = {"name": spec["name"], "region": spec["region"], "metrics": metrics, "frames": paths}
    checks.insert(0, {**planned[0], "ok": share >= spec["min_moving"], "value": metrics,
                      "detail": f"{moving} of {len(pairs)} frame pairs moved (need {spec['min_moving']:.0%}); "
                                f"the longest still stretch is {still} pair(s); the largest change "
                                f"{max(m['fraction'] for m in pairs):.2%} of the region"})
    dark = None if scenario.canvas_dark else _all_flat_dark(frames)
    if dark:  # a capture without its GPU layer reads as a frozen view (redteam-1 L3, #276): nothing was judged
        evidence["flat_dark"] = dark
        broke = (f"every frame of region {spec['region']['name']!r} is one flat dark colour (median grey "
                 f"{dark['median']}): the capture may have lost the page's GPU layer, so motion was not judged; if "
                 "the region is dark on purpose, say why with canvas_dark")
        checks[0] = {**planned[0], "ok": False, "broke": True, "value": metrics, "detail": broke}
        looks = planned[1 + bool(spec.get("max_gap_ms")):]
        return checks + [{**named, "ok": False, "broke": True, "detail": broke} for named in looks], evidence, None
    if not spec["looks"]:
        return checks, evidence, None
    pick = sorted({round(i * (len(frames) - 1) / (min(SHEET_FRAMES, len(frames)) - 1))
                   for i in range(min(SHEET_FRAMES, len(frames)))})
    picture = sheet([frames[i] for i in pick])
    evidence["sheet"] = f"shots/{stem}-sheet.png"
    (run_dir / evidence["sheet"]).write_bytes(picture)
    evidence["sheet_frames"] = pick
    context = {"page": {"url": observed.get("url"), "title": observed.get("title")},
               "image": f"{len(pick)} frames of the region {spec['region']['name']!r}, taken over "
                        f"{at[pick[-1]] - at[pick[0]]:.1f} s, in time order: left to right, then top to bottom"}
    try:
        judged = vision.look(session.jev.model.post_json, vision.data_url(picture), spec["looks"], context)
    except (RuntimeError, ValueError) as e:
        return checks, evidence, f"while judging the motion looks: {e}"
    for check, named in zip(judged, planned[1 + bool(spec.get("max_gap_ms")):], strict=True):
        checks.append({**check, **named})
    return checks, evidence, None


# The page's records that a sweep counts as an error: what it logged with console.error, an uncaught exception, an
# unhandled rejection. A worker's console counts at the error level only.
SWEEP_ERRORS = {"console", "exception", "rejection"}
SWEEP_KEEP = 10  # errors and banners kept for each setting


def _sweep_errors(probe):
    """The errors in one read of the page (session.probe's `errors`, drained at each read), cleaned for the report."""
    out = []
    for err in probe.get("errors") or []:
        kind, detail = err.get("kind"), str(err.get("detail", ""))
        if (kind in SWEEP_ERRORS or (kind == "worker" and err.get("level") == "error")) \
                and "QAJev read-only" not in detail:  # QAJev's own write block, as findings_from_probe skips it
            out.append(f"{kind}: {clean_data(detail)[:300]}")
    return out


def _sweep_banners(session, spec):
    """The banners a person sees now -> (shown, broken): [{selector, text}], and the read's errors (a selector that is
    not CSS, or a read that failed)."""
    try:
        banners = session.banners(spec["banners"]) if spec["banners"] else []
    except (RuntimeError, TimeoutError) as e:
        banners = [{"selector": None, "error": f"the banners could not be read: {e}"}]
    return [b for b in banners if not b.get("error")], [b for b in banners if b.get("error")]


def _sweep_check(session, scenario, url, read):
    """expect.sweep: for each setting, the banners shown before it, its do hooks, the sweep's wait, then the page's new
    errors and the banners a person sees that were not there before the hooks. read() is the runner's own read of the
    page: it drains the errors, so each read holds only what came after the one before. -> (one check per setting,
    evidence)."""
    from .session import HookBroke, HookFailed

    spec = scenario.expect["sweep"]
    read()  # what the page logged before the sweep is no setting's
    checks, evidence = [], {"name": spec["name"], "wait": spec["wait"], "banners": spec["banners"], "settings": []}
    for setting, planned in zip(spec["settings"], verdict.sweep_planned(spec), strict=True):
        # a banner shown before the hooks (before the sweep, or by an earlier setting) is not this one's (redteam-1 M1)
        before, broken = _sweep_banners(session, spec)
        try:
            _run_hooks(session, f"sweep {spec['name']!r}, setting {setting['name']!r}, do", setting["do"], url,
                       doing=False)
        except HookBroke:  # a js hook's error names its place already
            raise
        except HookFailed as e:  # the sweep could not reach the setting: a failed hook, as a before hook's
            raise HookFailed(f"sweep {spec['name']!r}, setting {setting['name']!r}: {e}") from None
        time.sleep(spec["wait"])
        probe = (read() or {}).get("probe") or {}
        errors, blocked = _sweep_errors(probe), probe.get("blocked") or []
        after, broke_after = _sweep_banners(session, spec)
        broken += broke_after
        seen = {(b["selector"], b["text"]) for b in before}
        shown = [b for b in after if (b["selector"], b["text"]) not in seen]
        evidence["settings"].append({"name": setting["name"], "errors": errors[:SWEEP_KEEP], "error_count": len(errors),
                                     "banners": shown[:SWEEP_KEEP], "banners_before": before[:SWEEP_KEEP],
                                     **({"blocked": blocked[:SWEEP_KEEP]} if blocked else {})})
        if broken:  # nothing was judged: the test's selector, or QAJev's read, broke
            checks.append({**planned, "ok": False, "broke": True,
                           "detail": "; ".join(f"banner selector {b['selector']!r}: {b['error']}" if b["selector"]
                                               else b["error"] for b in broken)})
            continue
        if blocked:  # QAJev's read-only guard stopped the setting's own write: the page's answer is not the product's
            writes = ", ".join(f"{w.get('method')} {w.get('url')}" for w in blocked[:3])
            checks.append({**planned, "ok": False, "broke": True,
                           "detail": f"QAJev's read-only guard blocked the setting's write ({writes}), so what the "
                                     "page did next was not judged: run the sweep where the write may go out, or "
                                     "choose settings that save nothing"})
            continue
        said = [*errors[:3], *(f"banner {b['selector']}: {b['text']!r}" for b in shown[:3])]
        more = len(errors) + len(shown) - len(said)
        checks.append({**planned, "ok": not errors and not shown,
                       "detail": (f"{len(errors)} new error(s), {len(shown)} new banner(s): " + "; ".join(said)
                                  + (f"; and {more} more" if more > 0 else "")) if said
                       else "no new error and no new banner"})
    return checks, evidence


def _load_check(session, scenario, opts, run_dir, started):
    """expect.load: the time from the navigation (started: the browser's clock right before it) to the page's first
    playable frame (session.wait_load), with a screenshot of that moment. -> (check, evidence)."""
    spec = scenario.expect["load"]
    got = session.wait_load(spec, started)
    limit = spec["max_ms"]
    evidence = {"max_ms": limit, "cache": "cold", **got}
    check = {**verdict.load_planned(spec), "value": evidence}
    # the browser's clock could not be read before the navigation: a loader page's own time is then left out
    # (redteam-1 L2, #278)
    clock = "" if got.get("clock", "navigation") == "navigation" else \
        "; timed from the last document's start, as the browser's clock could not be read before the navigation"
    if got["ms"] is not None:
        ok = got["ms"] <= limit
        detail = f"playable at {got['ms'] / 1000:.2f} s" + ("" if ok else f", over the {limit / 1000:g} s limit") + \
            clock
        if opts.shots and run_dir is not None:
            try:
                shot = session.screenshot(run_dir / "shots" / f"{slug(scenario.name)}-load.jpg")
                evidence["shot"] = str(shot.relative_to(run_dir)) if shot else None
            except (RuntimeError, TimeoutError, OSError, ValueError) as e:  # the time stands; the picture is missing
                evidence["shot_error"] = f"{type(e).__name__}: {e}"
        return {**check, "ok": ok, "detail": detail}, evidence
    if not got.get("watched"):  # nothing was judged: the author's expression stopped QAJev's watcher
        return {**check, "ok": False, "broke": True,
                "detail": "the ready watcher never ran in the page: check ready and failed for a JavaScript "
                          "syntax error"}, evidence
    if not got.get("frames") and got.get("failed_ms") is None:  # nothing was judged: the page drew no frame
        return {**check, "ok": False, "broke": True,
                "detail": f"the page drew no animation frame in {got['waited_ms'] / 1000:.1f} s, so its load was not "
                          "judged: QAJev's browser window may be hidden or covered"}, evidence
    if got.get("failed_ms") is not None:
        detail = f"the page said its load failed at {got['failed_ms'] / 1000:.2f} s ({spec['failed']!r} returned true)"
    else:
        detail = f"not playable after {got['waited_ms'] / 1000:.1f} s (limit {limit / 1000:g} s)"
    if got.get("error"):
        detail += f"; ready last threw: {got['error']}"
    return {**check, "ok": False, "detail": detail + clock}, evidence


def run_scenario(session, scenario, *, opts, hosts, run_dir):  # pyright: ignore[reportGeneralTypeIssues]
    # SHORTCUT: basedpyright stops at this function's size ("too complex to analyze"). Its parts move into helpers
    # when it next changes for a feature; until then this line keeps the type check meaningful for the rest.
    from .session import GuardMissing, HookFailed, HookThrew, LeftSite, require_transport, transport_error
    from .viewport import ViewportMismatch

    started = time.monotonic()
    if hasattr(session, "begin_test"):  # the last test's page: its frames were named under that test
        session.begin_test()
    session.assists = []  # this scenario's own; a hook failing before Jev's loop must not inherit the last one's
    session.react_log, session.react_frames = [], 0  # frames and releases from react hooks, this scenario's own
    session.last_capture_viewport = None
    session.shot_dir = run_dir / "shots" if opts.shots and run_dir else None
    session.shot_prefix = slug(scenario.name)
    quiet, load1 = wait_for_quiet(opts)
    scheme = scenario.color_scheme or getattr(opts, "color_scheme", None)
    result = {
        "name": scenario.name, "url": scenario.url, "goal": scenario.goal, "mode": scenario.mode,
        "device": scenario.device, "load1": round(load1, 1), "checks": [], "findings": [], "screens": [],
        "blocked_writes": [], "guard_hidden": 0, "history": [], "jev": None, "shot": None,
        **({"color_scheme": scheme} if scheme else {}), **_cache_record(_cache(scenario, opts)),
    }
    if not quiet:
        return _finish(result, "skipped", f"machine busy (load1 {load1:.0f})", started)

    findings = []
    observed = {}
    stop, detail, thrown = None, None, None
    before = None  # the page's checks as Jev found it, before its first move
    ux_observer = None
    load = scenario.expect.get("load")
    measured_load = load_started = None
    ran = set()  # the expectations beyond the page's own that did run
    ux_enabled = resolve_ux(opts, getattr(scenario, "ux_options", None))["ux"] != "off"

    def absorb(obs):
        probe = (obs or {}).get("probe") or {}
        keep_console(result, probe)
        findings.extend(verdict.findings_from_probe(probe, scenario=scenario.name, url=obs.get("url"),
                                                    first_party_hosts=hosts, why=getattr(session, "why_failed", None)))
        result["blocked_writes"].extend(probe.get("blocked") or [])
        if "block_list" in probe:  # block_urls set: every request to a blocked address, even none
            result.setdefault("block_list_requests", []).extend(probe["block_list"])
        for url in probe.get("frames_not_observed") or []:  # frames QAJev could not read: their lists say nothing
            if url not in result.setdefault("frames_not_observed", []):
                result["frames_not_observed"].append(url)
        for frame in probe.get("frames_accepted") or []:  # guard.unguarded_frames: reviewed, so never harness
            if frame not in result.setdefault("frames_accepted", []):
                result["frames_accepted"].append(frame)
        said = result.setdefault("frame_console", []) if probe.get("frame_console") else []
        said.extend((probe.get("frame_console") or [])[:verdict.FRAME_KEEP - len(said)])
        for r in probe.get("frame_requests") or []:  # the latest state of each: pending, then failed or finished
            asked = result.setdefault("frame_requests", [])
            key = (r.get("method"), r.get("url"), r.get("frame"))
            asked[:] = [a for a in asked
                        if (a.get("method"), a.get("url"), a.get("frame")) != key][:verdict.FRAME_KEEP - 1]
            asked.append(r)
        for key, out in (("allowed", "allowed_writes"), ("dialogs", "dialogs")):  # what production let through, asked
            if probe.get(key):
                result.setdefault(out, []).extend(probe[key])
        result["guard_hidden"] = max(result["guard_hidden"], probe.get("hidden") or 0)
        verdict.keep_hidden(result.setdefault("guard_hidden_controls", []), probe.get("hidden_controls"))
        if probe.get("guard_hydration"):  # React's warnings about the guard's own attributes: a note, not a finding
            result["guard_hydration"] = max(result.get("guard_hydration", 0), probe["guard_hydration"])
            kept = result.setdefault("guard_hydration_details", [])
            kept.extend((probe.get("guard_hydration_details") or [])[:20 - len(kept)])
        result["blank_ms"] = max(result.get("blank_ms", 0), probe.get("blank_ms") or 0)
        for vital in ("lcp", "cls"):
            if probe.get(vital):
                result[vital] = probe[vital]
        return obs

    def read():
        nonlocal observed
        observed = absorb(session.probe(scenario.expect))
        require_transport()
        return observed

    def page_ok():
        return has_page_checks(scenario.expect) and verdict.all_ok(verdict.page_checks(scenario.expect, observed))

    def blind():
        """What the guard hides from the absent checks once every other check holds, else None."""
        if not has_page_checks(scenario.expect):
            return None
        hid = verdict.guard_blind(verdict.page_checks(scenario.expect, observed))
        return "; ".join(c["check"] for c in hid) or None

    try:
        if hasattr(session, "set_color_scheme"):  # a stand-in session in tests may not have it
            session.set_color_scheme(scheme)
        session.set_device(scenario.device)
        _prepare_tab(session, scenario, opts)
        session.arm(scenario.mode, scenario.speech)
        if hasattr(session, "configure_ux"):
            ux_observer = session.configure_ux(opts, run_dir, scenario.name, getattr(scenario, "ux_options", None))
        effect = scenario.expect.get("effect") or {}
        if any("pad" in hook for hook in [*scenario.before, *scenario.after, *effect.get("off", []),
                                         *effect.get("on", []), *(scenario.expect.get("input") or {}).get("do", []),
                                         *(scenario.expect.get("motion") or {}).get("do", []),
                                         *sweep_hooks(scenario.expect)]):
            session.use_pad()  # before the page loads: a game may read its pads only once, at load
        if scenario.canvas_capture and scenario.expect.get("looks") and hasattr(session, "count_draws"):
            session.count_draws()  # before the page loads: it wraps the draw calls of contexts made later (drawn_dark)
        if scenario.url:
            session.check_host(scenario.url)
            if load:
                session.watch_load(load)  # before the page loads
                load_started = session.browser_clock()  # the time runs from the navigation, on the browser's clock
            error = session.navigate(scenario.url)
            if error:
                stop, detail = "unreachable", error
        if stop is None:
            if hasattr(session, "verify_viewport"):
                result["viewport"] = session.verify_viewport()
            if ux_observer:
                ux_observer.safe(ux_observer.start)
            if load:
                _emit(opts, "step", scenario=scenario.name, at=time.time(),
                      doing=f"waiting for the page to be playable (limit {load['max_ms'] / 1000:g} s)")
                measured_load, result["load"] = _load_check(session, scenario, opts, run_dir, load_started)
                ran.add("load")
                session.unwatch_load()  # the test's later reloads and pages are not measured
            # What it is doing, for the dashboard and qajev top, while a slow check runs in the page (a script that
            # plays the game for a minute said nothing at all before).
            _emit(opts, "step", scenario=scenario.name, at=time.time(),
                  doing="reading the page and running its checks" if has_page_checks(scenario.expect)
                  else "reading the page")
            read()
            findings += verdict.document_findings(observed.get("status"), scenario.expect, scenario=scenario.name,
                                                  url=observed.get("url"))
            _run_hooks(session, "before", scenario.before, observed.get("url") or scenario.url)
            if scenario.before:  # the checks judge the page after its hooks (verse2, 6 Oct: a wait_for "did not
                read()           # wait" because the checks used the page as read before it ran)
            if scenario.goal:
                if has_page_checks(scenario.expect):
                    before = verdict.page_checks(scenario.expect, observed)
                session.reset_agent(scenario.task)

                def step(doing, **extra):  # what Jev is doing and the money spent so far, for qajev top
                    _emit(opts, "step", scenario=scenario.name, doing=doing, at=time.time(),
                          spent_usd=round(session.ledger.spent(), 5), **extra)

                def decided(d):  # each decision as it is made: what it chose, how sure, the runner-up
                    s = verdict.screens([d])[0]
                    _emit(opts, "decision", scenario=scenario.name, at=time.time(),
                          screen=observed.get("title") or observed.get("url"), chose=s["next_step"],
                          operation=s["operation"], p=s["p"], runner_up=s["runner_up"],
                          runner_up_p=s["runner_up_p"], options=s["options"], ms=s["ms"],
                          **({"stale": s["stale"]} if s.get("stale") else {}))

                held = bool(before) and verdict.all_ok(before)
                with vision.seeing(session.screen_image if scenario.vision else None):
                    stop, detail = drive(session, scenario, read, page_ok, started, step, decided, held=held,
                                         blind=blind)
            else:
                stop = "checked"
    except ViewportMismatch as e:
        if "viewport" not in result:  # preflight failed before any scenario work
            return _finish({**result, "stop": "viewport_mismatch", "viewport": e.evidence},
                           "harness", str(e), started)
        stop, detail = "viewport_mismatch", str(e)
        result["capture_viewport"] = e.evidence
    except HookThrew as e:  # harness, or the page's answer when the test says so (suite.hook_throw)
        stop, detail, thrown = _thrown_stop(scenario), str(e), e
    except HookFailed as e:
        stop, detail = "hook_failed", str(e)
    except LeftSite as e:
        stop, detail = "left_site", str(e)
    except GuardMissing as e:
        stop, detail = "guard_missing", str(e)
    except CostCapReached as e:
        stop, detail = "cost_cap", str(e)
    except KeyboardInterrupt:
        stop, detail = "interrupted", "stopped by the operator"
        result["interrupted"] = True
    except (RuntimeError, ValueError, TimeoutError, OSError) as e:
        stop, detail = "browser_error", f"{type(e).__name__}: {e}"

    checks = []
    if load and hasattr(session, "unwatch_load"):
        session.unwatch_load()
    if stop not in {"unreachable", "interrupted", "viewport_mismatch"} and transport_error() is None:
        _doing(session, "the checks")
        try:
            if stop != "reached" and has_page_checks(scenario.expect):
                if not page_ok():
                    _emit(opts, "step", scenario=scenario.name, at=time.time(),
                          doing=f"waiting up to {scenario.settle:.0f} s for the expectations to hold")
                settle_until = time.monotonic() + scenario.settle
                while not page_ok() and time.monotonic() < settle_until:
                    time.sleep(0.5)
                    read()
            elif not observed:
                read()
            if scenario.goal and scenario.expect.get("visible") and not page_ok():
                assist = scroll_to_visible(session, scenario.expect)
                if assist:
                    session.assists.append(assist)
                    read()
            measured_effect = measured_input = None
            measured_motion = []
            if scenario.expect.get("motion") and stop not in HALTS:
                measured_motion, result["motion"], failed = _motion_check(
                    session, scenario, run_dir, observed.get("url") or scenario.url, observed)
                if failed:  # Clef failed, not the browser: its looks were not judged, so motion did not run
                    stop, detail = "model_error", failed
                else:
                    ran.add("motion")
                read()
            if scenario.expect.get("input") and stop not in HALTS:
                measured_input, result["input"] = _input_check(session, scenario, run_dir,
                                                               observed.get("url") or scenario.url)
                ran.add("input")
                read()  # the page checks and looks judge the page after its input
            if scenario.expect.get("effect") and stop not in HALTS:
                measured_effect, result["effect"] = _effect_check(session, scenario, run_dir,
                                                                 observed.get("url") or scenario.url)
                ran.add("effect")
                read()  # ordinary page checks and looks judge the final, enabled state
            measured_sweep = []
            if scenario.expect.get("sweep") and stop not in HALTS:
                measured_sweep, result["sweep"] = _sweep_check(session, scenario, observed.get("url") or scenario.url,
                                                               read)
                ran.add("sweep")
            checks = verdict.page_checks(scenario.expect, observed)
            if measured_load is not None:
                checks.append(measured_load)
            if measured_input is not None:
                checks.append(measured_input)
            checks += measured_motion
            if measured_effect is not None:
                checks.append(measured_effect)
            checks += measured_sweep
            if scenario.expect.get("looks") and stop not in HALTS:
                looks, failed = judge_looks(session, scenario, observed, run_dir, result, ran)
                checks += looks
                if failed:  # Clef failed, not the browser
                    stop, detail = "model_error", failed
            if stop not in HALTS:
                ran.add("after")  # a hook that fails is named by the stop itself, not as never run
                _run_hooks(session, "after", scenario.after, observed.get("url") or scenario.url)
                says = scenario.expect.get("says") or {}  # the author's words for these checks (the test plan)
                for probe in scenario.expect.get("fetch", []):
                    checks.append(verdict.fetch_check(probe, session.fetch(probe) or {}))
                    if says.get("fetch"):
                        checks[-1]["says"] = says["fetch"]
                ran.add("fetch")
                if scenario.expect.get("command"):
                    spec = scenario.expect["command"]
                    checks.append(verdict.command_check(spec, session.command(spec["run"], observed.get("url"))))
                    if says.get("command"):
                        checks[-1]["says"] = says["command"]
                ran.add("command")
        except HookThrew as e:
            stop, detail, thrown = _thrown_stop(scenario), str(e), e
        except HookFailed as e:
            stop, detail = "hook_failed", str(e)
        except ViewportMismatch as e:
            stop, detail = "viewport_mismatch", str(e)
            result["capture_viewport"] = e.evidence
        except (RuntimeError, ValueError, TimeoutError, OSError) as e:
            if stop in {"done", "reached", "checked", "blocked"}:
                stop, detail = "browser_error", f"while checking: {type(e).__name__}: {e}"
        if opts.shots and run_dir and transport_error() is None and stop != "viewport_mismatch":
            try:
                shot = session.screenshot(run_dir / "shots" / f"{slug(scenario.name)}.jpg")
                result["shot"] = str(shot.relative_to(run_dir)) if shot else None
                try:
                    if shot and not scenario.canvas_capture and (blank := session.blank_canvas(shot)):
                        result["canvas_blank"] = blank
                except (RuntimeError, KeyError, TimeoutError, OSError, ValueError) as e:  # the reason says so
                    result["canvas_blank_error"] = f"{type(e).__name__}: {e}"
            except ViewportMismatch as e:
                stop, detail = "viewport_mismatch", str(e)
                result["capture_viewport"] = e.evidence
        if scenario.canvas_capture and run_dir and transport_error() is None:  # the canvas's own readback (suite.py)
            got = session.page_canvas(scenario.canvas_capture,
                                      run_dir / "shots" / f"{slug(scenario.name)}-canvas.png")
            image = str(got["image"].relative_to(run_dir)) if got["image"] else None
            result["canvas_capture"] = {**got, "image": image}
            if got["blank"]:
                result["canvas_blank"] = {**got["blank"], "readback": True}

    capture = getattr(session, "last_capture_viewport", None)
    if "capture_viewport" not in result and isinstance(capture, dict):
        result["capture_viewport"] = capture

    state = session.agent.state if scenario.goal else None
    if state:
        result["jev"] = {
            "status": state["status"], "actions": len(state["history"]), "decisions": len(state["decisions"]),
            "text_calls": len(state["text_calls"]), "elapsed_ms": state["elapsed_ms"],
        }
        result["screens"] = verdict.screens(state["decisions"])
        result["history"] = [
            {k: h.get(k) for k in ("step", "action", "kind", "text", "url", "page_changed", "probability",
                                   "latency_ms", "text_latency_ms", "executed_ms", "elapsed_ms")}
            for h in state["history"][-15:]
        ]
    if thrown is not None and stop == "hook_check_failed":  # the hook's check, named by its place for expect_fail
        checks.append({"check": f"{thrown.where or 'a js hook'} ran", "ok": False,
                       "detail": f"`{thrown.source}` threw {thrown.said}"})
    result["checks"] = checks
    if session.react_log:  # what react hooks saw: the frames they asked for (relative to the run) and any releases
        result["react"] = [{**e, "path": str(Path(e["path"]).relative_to(run_dir)) if e.get("path") and run_dir
                            else e.get("path")} for e in session.react_log]
    result["end_url"] = observed.get("url")
    result["page_says"] = observed.get("says")
    findings += verdict.blank_finding(result.get("blank_ms", 0), scenario=scenario.name, url=observed.get("url"))
    result["findings"] = verdict.dedupe(findings)
    result["stop"] = stop
    wanted = {"looks": scenario.expect.get("looks"), "after": scenario.after, "fetch": scenario.expect.get("fetch"),
              "command": scenario.expect.get("command"), "effect": scenario.expect.get("effect"),
              "load": load, "input": scenario.expect.get("input"), "motion": scenario.expect.get("motion"),
              "sweep": scenario.expect.get("sweep")}
    not_run = [NOT_RUN[k] for k, v in wanted.items() if v and k not in ran]
    outcome, reason = verdict.classify(stop, checks, has_checks=has_checks(scenario.expect), stop_detail=detail,
                                       not_run=not_run, actions=(result.get("jev") or {}).get("actions"),
                                       url=scenario.url, before=before)
    result.update({k: v for k, v in (("stop_detail", detail), ("not_run", not_run)) if v})
    if result.get("canvas_blank") and scenario.canvas_dark:
        result["canvas_dark"] = scenario.canvas_dark
    outcome, reason = looks_blank_grade(outcome, reason, result.get("looks_blank"), checks)
    outcome, reason = canvas_blank_grade(outcome, reason, result.get("canvas_blank"), dark=scenario.canvas_dark,
                                         error=result.get("canvas_blank_error"))
    outcome, reason = canvas_capture_grade(outcome, reason, result.get("canvas_capture"))
    idle = verdict.never_set_off(scenario.url, observed.get("url"), scenario.expect,
                                 session.agent.state["history"]) if scenario.goal else None
    if outcome in {"fail", "stuck"} and idle:
        outcome, reason = "harness", f"{idle}; {reason}"
    wall = verdict.sign_in_wall(scenario.url, observed, account=opts.account,
                                profile=None if opts.ephemeral or opts.profile == "default" else opts.profile)
    if outcome not in {"pass", "skipped"} and wall:
        result["needs_sign_in"] = wall
        result = _finish(result, "harness", wall["reason"], started)
        if ux_observer and transport_error() is None:
            ux_observer.safe(ux_observer.finish)
            ux_observer.apply(result)
        return result
    assists = getattr(session, "assists", []) if scenario.goal else []
    if assists:
        result["assists"] = assists
        blocked = [a for a in assists if a["after"] == "BLOCKED"]
        reach = [a for a in assists if a["after"] == "visible"]
        if outcome == "pass" and any(a["scrolled"] for a in blocked):
            reason += "; QAJev scrolled for Jev after it said BLOCKED (the content is below the fold)"
        elif outcome == "stuck" and blocked and not any(a["scrolled"] for a in blocked):
            reason += "; QAJev's own scroll did not move the page either"
        for a in reach:
            reason += (f"; QAJev scrolled {a['scrolled']} screen(s) to bring the expected text on screen, as a person "
                       "reading on would" if a["found"] else
                       f"; the expected text is in the page but {a['scrolled']} screen(s) of scrolling did not bring "
                       "it on screen")
    hidden = result.get("guard_hidden_controls") or []
    # Before blaming the page: was it the guard, or an action Jev does not have? Then harness, and no struggle notes.
    blame = ux.blocked_by(scenario.goal, outcome, hidden) if scenario.goal else None
    if blame:
        outcome, reason = "harness", f"{blame}; {reason}"
    elif ux_enabled and scenario.goal and session.agent.state["history"]:  # free notes on finding the goal
        result["ux"] = [*result.get("ux", []), *ux.struggle(session.agent.state["history"], result.get("screens") or [],
                                                             outcome, assists,
                                                             jev_done=session.agent.state.get("status") == "done")]
    if not (blame or "").startswith("blocked by the guard"):  # the blame already names what the guard hid
        reason = verdict.with_guard_note(outcome, reason, hidden)
    outcome, reason = verdict.unguarded(outcome, reason, result.get("frames_not_observed"))
    result = _finish(result, outcome, reason, started)
    if ux_observer and transport_error() is None:
        ux_observer.safe(ux_observer.finish)
        ux_observer.apply(result)
    return result


VISIBLE_SCROLLS = 6  # how far a person reads on for something already on the page; beyond that it is not "visible"


def scroll_to_visible(session, expect):
    """Jev stopped (often declaring DONE from the top of the page) while an expected `visible` text is in the page but
    not on screen. Scroll down to it the way a person reading on would, one screen at a time, at most VISIBLE_SCROLLS.
    -> {"after": "visible", "scrolled": screens, "found": all on screen}, or None when a text is not in the page."""
    ci = bool(expect.get("ignore_case"))
    wanted = expect.get("visible") or []
    seen = session.probe({"visible": wanted, "ignore_case": ci}).get("visible") or []
    missing = [t for t, ok in zip(wanted, seen) if not ok]
    if not missing or not all(session.probe({"text": missing, "ignore_case": ci}).get("text") or [False]):
        return None
    screens = 0
    for screens in range(1, VISIBLE_SCROLLS + 1):
        moved = session.scroll_further()
        if all(session.probe({"visible": missing, "ignore_case": ci}).get("visible") or [False]):
            return {"after": "visible", "scrolled": screens, "found": True}
        if not moved:
            break
    return {"after": "visible", "scrolled": screens, "found": False}


def judge_looks(session, scenario, observed, run_dir, result, ran):
    """expect.looks, judged by Clef on the screen now: -> (its checks, None), and "looks" joins ran. On a screen whose
    main canvas is one flat dark colour (looks_blank) Clef is not asked: -> ([], None), with result["looks_blank"], and
    looks_blank_grade grades it, unless the page's own frame is dark too (drawn_dark): then Clef judges the screen.
    When Clef fails: -> ([], why). A failed capture raises, as a browser error."""
    structured = any(isinstance(entry, dict) for entry in scenario.expect["looks"])
    image = session.screen_image_png() if structured else session.screen_image()
    blank = None if scenario.canvas_dark else looks_blank(session, image, result)
    if blank and scenario.canvas_capture and run_dir:
        frame = drawn_dark(session, scenario, run_dir)
        result["looks_frame"] = frame
        if frame["dark"]:
            blank = None
    if blank:
        result["looks_blank"] = blank
        return [], None
    try:
        checks = vision.look(session.jev.model.post_json, image, scenario.expect["looks"],
                             {"page": {"url": observed.get("url"), "title": observed.get("title")}},
                             folder=getattr(session, "cwd", None), evidence_dir=run_dir)
    except (RuntimeError, ValueError) as e:
        return [], f"while judging looks: {e}"
    ran.add("looks")
    return checks, None


def drawn_dark(session, scenario, run_dir):
    """The screen is flat dark and the scenario has canvas_capture: page. The page's own frame, read inside a
    requestAnimationFrame callback after the page drew it (Session.page_canvas in_frame), tells why. -> {dark, image,
    transparent, draws, problem, blank}. dark: that frame is flat dark, not transparent, and the page made draw calls
    in it (Session.count_draws), so the page drew a dark frame and Clef judges the looks (redteam-1's known-answer
    tests, 3 Nov: a black-frame fault must fail on its look, not grade harness). A lit frame means the screenshot lost
    the canvas's GPU layer; a transparent one, a cleared buffer or nothing drawn; 0 draws (or no count), that the buffer
    holds no frame the page drew in this one (redteam-1 O1): none of these is judged, as before."""
    got = session.page_canvas(scenario.canvas_capture, run_dir / "shots" / f"{slug(scenario.name)}-frame-canvas.png",
                              in_frame=True)
    transparent, draws = bool(got.get("transparent")), got.get("draws")
    drew = isinstance(draws, int) and not isinstance(draws, bool) and draws > 0
    return {"dark": bool(got["blank"]) and not transparent and drew and not got["problem"],
            "image": str(got["image"].relative_to(run_dir)) if got["image"] else None,
            "transparent": transparent, "draws": draws, "problem": got["problem"], "blank": got["blank"]}


def looks_blank(session, image, result):
    """The image Clef would judge for expect.looks (a data URL) shows the page's main canvas as one flat dark colour,
    read now at the canvas points no other element covers: -> Session.blank_canvas's reading, else None. Clef is then
    not asked: a blank capture says nothing about how the page looks, and Clef graded one as fail (rea's Crumble run on
    workload-gpu, 1 Nov). A check that breaks is noted in looks_blank_error, and Clef judges the image as before."""
    reader = getattr(session, "blank_canvas", None)
    if reader is None:
        return None
    try:
        return reader(base64.b64decode(image.split(",", 1)[1], validate=True))
    except (RuntimeError, KeyError, TimeoutError, OSError, ValueError, IndexError, binascii.Error) as e:
        result["looks_blank_error"] = f"{type(e).__name__}: {e}"
        return None


def looks_blank_grade(outcome, reason, blank, checks):
    """expect.looks on a screen whose main canvas is one flat dark colour was not judged (looks_blank): harness, with
    the words first. A check that ran and failed still fails the test, with the words. -> (outcome, reason)"""
    if not blank:
        return outcome, reason
    words = (f"the looks were not judged: the screen shows the page's main canvas as one flat dark colour "
             f"({blank['flat']:.0%} of the {blank['points']} points no other element covers), so Clef would only "
             "judge a blank capture. The browser's capture may lack the canvas's GPU layer, or the page drew nothing")
    if any(not c["ok"] for c in checks):
        return outcome, f"{reason}; {words}"
    return "harness", f"{words}; {reason}"


def canvas_blank_grade(outcome, reason, blank, *, dark=None, error=None):
    """A pass, or a test with no checks, whose end screenshot shows the page's main canvas as one flat dark colour
    says nothing about the page: harness (workload-gpu, 31 Oct: a headless Chrome with --disable-vulkan-surface left
    every 3D view out of the screenshot, and the tests passed on their text checks). A failure stays a failure, with
    the note. dark: the suite's canvas_dark, why the canvas is dark on purpose: the outcome stays, and the reason says
    so. error: the check itself broke, so nothing is known about the canvas: the reason says so. -> (outcome, reason)"""
    if error:
        return outcome, f"{reason}; the canvas check could not read the end screenshot ({error})"
    if not blank:
        return outcome, reason
    if dark:
        return outcome, (f"{reason}; the main canvas is one flat dark colour, which the suite says is on purpose "
                         f"(canvas_dark: {dark})")
    if blank.get("readback"):
        words = (f"the canvas's own readback (canvas_capture: page) is one flat dark colour ({blank['flat']:.0%} of "
                 f"{blank['points']} points): the page drew nothing")
    else:
        words = (f"the screenshot shows the page's main canvas as one flat dark colour ({blank['flat']:.0%} of the "
                 f"{blank['points']} points no other element covers): the browser's capture may lack the canvas's GPU "
                 "layer (a headless Chrome with --disable-vulkan-surface does that), or the page drew nothing")
    return _harness_unless_failed(outcome, reason, words)


def canvas_capture_grade(outcome, reason, capture):
    """canvas_capture: page. No readback image says nothing about the canvas, and neither does a run on a software
    WebGPU adapter when the suite asks for gpu: hardware: harness, as canvas_blank_grade. -> (outcome, reason)"""
    if not capture:
        return outcome, reason
    if capture.get("problem"):
        outcome, reason = _harness_unless_failed(outcome, reason, f"canvas_capture: page gave no readback image: "
                                                                  f"{capture['problem']}")
    if capture.get("software"):
        outcome, reason = _harness_unless_failed(outcome, reason, f"canvas_capture asks for a hardware GPU, but "
                                                                  f"{capture['software']}")
    return outcome, reason


def _harness_unless_failed(outcome, reason, words):
    """A pass, or a test with no checks, is harness with words first; a failure keeps its outcome, with the words."""
    if outcome in {"pass", "unverified"}:
        return "harness", f"{words}; {reason}"
    if outcome == "fail":
        return outcome, f"{reason}; {words}"
    return outcome, reason


def _finish(result, outcome, reason, started):
    result["outcome"] = outcome
    result["reason"] = reason
    result["seconds"] = round(time.monotonic() - started, 2)
    return result


def _did(h):
    """One of Jev's executed actions, in words: "click 'See pricing'", "type 'hello' into 'Email'"."""
    action = " ".join(str(h.get("action") or "").split())[:80]
    if h.get("text"):
        return f"type {str(h['text'])[:40]!r} into {action!r}"
    return f"{h.get('kind') or 'act'} {action!r}"


_CONTROL = ("role", "label", "kind", "value", "current_value", "checked", "selected", "expanded")


def _seen(page):
    """The page as a person reads it, for _loop: its address, scroll, words and controls (role, name, value, state),
    with clock times and times taken ticked. Jev's own fingerprint also holds each control's node id and box, so a step
    that builds the same sliders again, or a line that says how long the view took, reads as a new page after each
    click (verse #1000, 31 Oct: 8 clicks on "Adjust"). A page that has no reading keeps its fingerprint."""
    if not isinstance(page, dict) or not isinstance(page.get("actions"), list):
        return (page or {}).get("fingerprint")
    from .session import _ticked
    controls = [[_ticked(a.get(k)) for k in _CONTROL] for a in page["actions"] if isinstance(a, dict)]
    return json.dumps([page.get("url"), (page.get("scroll") or {}).get("y"), _ticked(page.get("text")), controls],
                      default=str)


def _loop(history, prints, first):
    """How often Jev clicked one target in a row while the page did not move on, once that is LOOP_CLICKS or more;
    else 0. prints[i] is the page's fingerprint after history[i], first the one before history[0]. "Load more" clicked
    3 times shows 3 new pages, so it is no loop; a button that does nothing, or a box that toggles, is."""
    if len(prints) < len(history) or not history:
        return 0
    last = history[-1]
    if last.get("kind") != "click":
        return 0
    n = 0
    while n < len(history) and history[-1 - n].get("kind") == "click" and history[-1 - n].get("action") == last.get(
            "action"):
        n += 1
    if n < LOOP_CLICKS:
        return 0
    start = len(history) - n
    states = {prints[start - 1] if start else first, *prints[start:len(history)]}
    return n if None not in states and len(states) <= 2 else 0


def _last_stale(session):
    """The last stale decision's reason, for a "stale" stop: "; the last: <Jev's reason> <what changed>"."""
    words = verdict.stale_words(getattr(session, "last_stale", None))
    return f"; the last: {words}" if words else ""


def drive(session, scenario, read, page_ok, started, step=None, decided=None, held=False, blind=None):
    """Jev's loop with QAJev's judgment around it. Returns (stop, detail). step(doing, **extra) hears each move;
    decided(decision) hears each of the model's decisions as it is made, stale ones too (qajev top's decisions view).
    held: the page's checks already held before Jev's first move, so they count as reached only after one.
    blind() names the absent checks the guard hides once every other check holds: no move of Jev's can judge them."""
    _doing(session, "Jev's goal")
    agent = session.agent
    state = agent.state
    step = step or (lambda doing, **extra: None)
    told = heard = 0
    deadline = started + scenario.budget["seconds"]
    reasks = escapes = model_failures = stale_base = 0
    assists = session.assists = []
    first, prints = _seen(state.get("page")), []
    while True:
        read()
        # A goal whose checks held before it began is not reached by holding still: the Orchestrator's sign-in goal
        # ended "reached" in 5 s with 0 decisions, its url check matching the sign-in page's ?next= (14 Oct).
        if page_ok() and (state["history"] or not held):
            return "reached", None
        if blind is not None and (hid := blind()):
            return "guard_blind", hid
        if state["status"] == "done":
            step("said DONE", n=len(state["history"]) + 1)
            return "done", None
        if clicks := _loop(state["history"], prints, first):
            # Before Jev's own BLOCKED after 3 actions that changed nothing: QAJev's two scroll-and-ask-again turns
            # would only bring more clicks on the same target (#802 run 9: 9 clicks).
            target = state["history"][-1].get("action")
            return "loop", (f"{target!r} {clicks} times in a row, and the page did not move on; the last actions: "
                            + "; ".join(_did(h) for h in state["history"][-5:]))
        if state["status"] == "blocked":
            step("said BLOCKED" + ("; QAJev scrolls a screen and asks again" if reasks < 2 else ""))
            # BLOCKED often fires on a page that has not finished loading, or on content below the fold that Jev
            # will not scroll to by itself. Look again, twice: first scroll one screen further, as a person would.
            if reasks < 2:
                reasks += 1
                moved = session.scroll_further()
                assists.append({"after": "BLOCKED", "scrolled": moved})
                time.sleep(1 if moved else 2)
                session.observe()
                state["status"] = "ready"
                continue
            # BLOCKED after the page kept changing under Jev (a live counter, a progress bar): its moves went stale
            # before they could run. That says the page churns, not that a visitor has no way forward.
            moves = sum(1 for d in state["decisions"] if d.get("operation") not in {"BLOCKED", "DONE"})
            wasted = moves - len(state["history"])
            if wasted >= 4 and wasted > len(state["history"]):
                return "stale", (f"{wasted} of Jev's moves went stale before they ran (the page kept changing)"
                                 + _last_stale(session))
            return "blocked", None
        if len(state["history"]) >= scenario.budget["actions"]:
            return "budget_actions", f"{len(state['history'])} actions"
        if time.monotonic() > deadline:
            return "budget_seconds", f"{scenario.budget['seconds']:.0f} s"
        pending = len(state["decisions"]) - len(state["history"]) - reasks - stale_base
        if pending > STALE_LIMIT:
            # A ticking timer or an open palette keeps invalidating decisions. Escape it, at most twice.
            if escapes < 2:
                escapes += 1
                session.press("Escape")
                session.observe()
                stale_base += pending
                continue
            return "stale", f"{pending} decisions without an action" + _last_stale(session)
        try:
            session.tick()
            while len(prints) < len(state["history"]):  # the page each action left: _loop compares them
                prints.append(_seen(state.get("page")))
            model_failures = 0
            for h in state["history"][told:]:
                step(_did(h), p=h.get("probability"), n=h.get("step"))
            told = len(state["history"])
            if decided:
                for d in state["decisions"][heard:]:
                    decided(d)
                heard = len(state["decisions"])
        except CostCapReached:
            raise
        except (RuntimeError, ValueError) as e:
            message = str(e)
            if "budget" in message:
                return "budget_actions", message
            if MODEL_ERROR.search(message):
                if model_failures < 2:
                    model_failures += 1
                    time.sleep(3 * model_failures)
                    continue
                return "model_error", message
            if type(e).__name__ in {"GuardMissing", "LeftSite", "HookFailed"}:
                raise
            return "browser_error", f"{type(e).__name__}: {message}"


# --------------------------------------------------------------------------------------------------------------
# Groups and workers


def run_clients(scenario, *, opts, suite_meta, run_dir, ledger):
    """A scenario with clients (clients.py), on a quiet machine like any other; Ctrl-C stops it as any other."""
    from . import clients

    started = time.monotonic()
    quiet, load1 = wait_for_quiet(opts)
    base = {"name": scenario.name, "url": scenario.url, "goal": None, "mode": scenario.mode, "checks": [],
            "findings": [], "screens": [], "load1": round(load1, 1)}
    if not quiet:
        return _finish(base, "skipped", f"machine busy (load1 {load1:.0f})", started)

    def step(doing):
        _emit(opts, "step", scenario=scenario.name, doing=doing, at=time.time())

    try:
        from .ux_options import resolve

        ux_config = resolve(opts, getattr(scenario, "ux_options", None))
        result = clients.run(scenario, ledger=ledger, hosts=set(suite_meta["hosts"]), run_dir=run_dir,
                             suite_meta={**suite_meta, "ux_options": ux_config},
                             step=step)
    except KeyboardInterrupt:
        return _finish({**base, "stop": "interrupted", "interrupted": True}, "harness",
                       "interrupted: stopped by the operator", started)
    result["load1"] = round(load1, 1)
    return result


def _transport_result(scenario, error, started, result=None):
    return _finish({"name": scenario.name, "url": scenario.url, "goal": scenario.goal, "mode": scenario.mode,
                    "checks": [], "findings": [], "screens": [], **(result or {}),
                    "stop": "browser_error", "transport_lost": True, "stop_detail": str(error)},
                   "harness", f"browser/daemon transport lost: {error}; no automatic retry", started)


def _doing(session, words):
    """What the test is doing now, in words, for a page that crashes (_crashed_result). Session.begin_test and
    Session.navigate say the rest: run_scenario is at the type checker's limit of paths."""
    session.doing = words


def _thrown_stop(scenario):
    """The stop for a js hook that threw a plain Error: harness (the test could not set up), or a failed check when
    the test takes the throw as the page's answer (hook_throw: fail; the Orchestrator, 4 Nov). A script bug
    (TypeError...) is a HookBroke, never a HookThrew: it stays hook_failed."""
    return "hook_check_failed" if getattr(scenario, "hook_throw", "harness") == "fail" else "hook_threw"


def _run_hooks(session, kind, hooks, url, doing=True):
    """Hooks in order, each named by its place ("before hook 2 of 3", "input 'aim', do hook 1 of 1"): for a page that
    crashes (doing: the test's before and after hooks), and in the error of a js hook that throws or breaks."""
    from .session import HookBroke

    for n, hook in enumerate(hooks, 1):
        where = f"{kind} hook {n} of {len(hooks)}"
        if doing:
            _doing(session, where)
        try:
            session.run_hook(hook, url)
        except HookBroke as e:  # a HookThrew too
            raise e.at(where) from None


def _cache(scenario, opts):
    """cold when the test or the run asks for a cold cache, else None."""
    load = (getattr(scenario, "expect", None) or {}).get("load")  # a load time means a first visit's
    return getattr(scenario, "cache", None) or getattr(opts, "cache", None) or ("cold" if load else None)


def _cache_record(cache):
    return {"cache": cache} if cache else {}


def _prepare_tab(session, scenario, opts):
    """The test's cache and console_keep patterns, before its page loads and its guard is armed."""
    if hasattr(session, "set_cache"):  # a stand-in session in tests may not have it
        session.set_cache(_cache(scenario, opts))
    session.console_keep = list(dict.fromkeys([*(getattr(opts, "console_keep", None) or ()),
                                               *getattr(scenario, "console_keep", [])]))


def _page_crash(session, scenario):
    """{url, step} when the test's own tab crashed (session.page_crash), else None. A multiplayer test has no tab."""
    if scenario.clients or session is None or not hasattr(session, "page_crash"):
        return None
    return session.page_crash()


def _crashed_result(scenario, crash, doing, started, result=None):
    """A test whose page crashed (session.PageCrashed, Chrome's "Aw, Snap"): fail, whatever its checks said by then. A
    page that crashes is the product's fault, not QAJev's (Verse, 1 Nov: on a cold cache). doing: what the test was
    doing when the crash was heard (crash["step"], else session.doing); Jev's last action when it was on its goal."""
    result = result or {}
    url = crash.get("url") or result.get("end_url") or scenario.url
    history = result.get("history") or []
    doing = crash.get("step") or doing
    step = doing or "the test's start"
    if doing == "Jev's goal" and history:
        step = f"Jev's step {history[-1].get('step') or len(history)}: {_did(history[-1])}"
    return _finish({"name": scenario.name, "url": scenario.url, "goal": scenario.goal, "mode": scenario.mode,
                    "checks": [], "findings": [], "screens": [], **result,
                    "stop": "page_crashed", "page_crashed": {"url": url, "last_step": step},
                    "stop_detail": f"the page crashed at {url}"},
                   "fail", f"the page crashed at {url}; last step: {step}", started)


def _stalled_result(scenario, stalled, started, result=None):
    """A test whose browser stopped answering (session.BrowserStalled): harness, whatever its checks said by then."""
    return _finish({"name": scenario.name, "url": scenario.url, "goal": scenario.goal, "mode": scenario.mode,
                    "checks": [], "findings": [], "screens": [], **(result or {}),
                    "stop": "browser_stalled", "stop_detail": str(stalled)},
                   "harness", f"{stalled}; the next test tries the browser again", started)


NVIDIA_GPUS = Path("/proc/driver/nvidia/gpus")  # one folder per NVIDIA GPU the Linux driver sees


def lift_gpu(browser, results):
    """The renderer each Chrome drew with (run_group puts it on the chain's first result) -> browser["gpu"]: {device}
    or {none}, plus `software` when it is SwiftShader, llvmpipe... and a `note` when this machine has an NVIDIA GPU
    all the same: WebGL and WebGPU pages then ran on the CPU (rea's looks on workload-gpu, 2 Nov). A fresh Chrome per
    test that drew with another renderer is listed under `others`."""
    from .session import _SOFTWARE_GPU

    seen = []
    for result in results:
        gpu = result.pop("browser_gpu", None)
        if isinstance(gpu, dict) and gpu not in seen:
            seen.append(gpu)
    if not seen:
        return
    gpu = dict(seen[0])
    if soft := _SOFTWARE_GPU.search(str(gpu.get("device") or "")):
        gpu["software"] = soft.group(0)
        try:
            nvidia = any(NVIDIA_GPUS.iterdir())
        except OSError:
            nvidia = False
        if nvidia:
            gpu["note"] = (f"Chrome drew with a software renderer ({soft.group(0)}) on a machine with an NVIDIA GPU: "
                           "WebGL and WebGPU pages ran on the CPU. Give Chrome the GPU (docs/troubleshooting.md).")
    if seen[1:]:
        gpu["others"] = seen[1:]
    browser["gpu"] = gpu


def software_gpu(scenario, drew, adapter):
    """A test whose canvas_capture asks for gpu: hardware, on a Chrome whose WebGPU adapter (adapter:
    Session.webgpu_adapter, read once per Chrome) is no hardware GPU by the end check's own rule
    (session.software_adapter, with drew: Session.gpu_info): why it must not start, else None. Jev then spends no step
    on a test whose end check grades it harness (rea's looks on workload-gpu, 2 Nov). A named hardware adapter starts
    the test whatever WebGL draws with: WebGPU can run on Vulkan while WebGL runs on SwiftShader (redteam-1 B1 on
    #266). No adapter, or one that could not be read, starts the test too: the end check decides."""
    from .session import software_adapter

    if (scenario.canvas_capture or {}).get("gpu") != "hardware" or not isinstance(adapter, dict) or adapter.get("none"):
        return None
    if why := software_adapter(adapter, drew):
        return (f"canvas_capture asks for a hardware GPU, but {why}: the test did not start, so it says nothing about "
                "the page")
    return None


def run_group(scenarios, *, opts, cdp_url, suite_meta, run_dir, ledger, prior=None):
    from . import session as session_mod

    results = []
    outcomes = dict(prior or {})
    name = session_mod.configure_env(cdp_url)
    session = gpu = drew = adapter = None
    lost_after = "an earlier scenario" if session_mod.transport_error() is not None else None
    try:
        for scenario in scenarios:
            if lost_after is not None:
                result = _finish({"name": scenario.name, "url": scenario.url, "goal": scenario.goal,
                                  "mode": scenario.mode, "checks": [], "findings": [], "screens": [],
                                  "stop": "transport_lost", "cost_usd": 0.0}, "skipped",
                                 f"not run: transport lost after {lost_after!r}; no automatic retry", time.monotonic())
                results.append(result)
                outcomes[scenario.name] = result["outcome"]
                _emit(opts, "scenario", result=result)
                continue
            blocked_by = [d for d in scenario.depends_on if outcomes.get(d) != "pass"]
            started = time.monotonic()
            spent_before = ledger.spent()
            session_mod.clear_stall()  # a timeout in the test before does not cost this one
            if blocked_by:
                result = _finish({"name": scenario.name, "url": scenario.url, "goal": scenario.goal,
                                  "mode": scenario.mode, "checks": [], "findings": [], "screens": []},
                                 "skipped", f"depends on {blocked_by} which did not pass", time.monotonic())
            else:
                try:
                    if scenario.clients:  # a multiplayer scenario: its own tabs, not this session
                        _emit(opts, "start", scenario=scenario.name)
                        result = run_clients(scenario, opts=opts, suite_meta=suite_meta, run_dir=run_dir, ledger=ledger)
                    else:
                        if session is None:
                            session = session_mod.Session(
                                ledger, headless=suite_meta["headless"], hosts=suite_meta["hosts"],
                                guard_opts=suite_meta["guard"], allow_commands=opts.allow_commands,
                                cwd=suite_meta["cwd"], motion=suite_meta.get("motion", "reduce"),
                                cpu_throttle=suite_meta.get("cpu_throttle", 1),
                            )
                            gpu = drew = session.gpu_info()  # the report names the renderer this Chrome draws with
                            if getattr(opts, "sign_in_here", None):
                                opts.signed_here = sign_in_here(session, opts.sign_in_here, opts)
                        if (startup_error := session_mod.transport_error()) is not None:
                            raise startup_error
                        signed_here = getattr(opts, "signed_here", None)
                        if signed_here and not signed_here["ok"]:
                            result = _finish({"name": scenario.name, "url": scenario.url, "goal": scenario.goal,
                                              "mode": scenario.mode, "checks": [], "findings": [], "screens": [],
                                              "stop": "sign_in"}, *sign_in_grade(signed_here), time.monotonic())
                            result["cost_usd"] = 0.0
                            results.append(result)
                            outcomes[scenario.name] = result["outcome"]
                            _emit(opts, "scenario", result=result)
                            continue
                        if (scenario.canvas_capture or {}).get("gpu") == "hardware" and adapter is None:
                            adapter = session.webgpu_adapter()  # once per Chrome, and only when a test asks
                        if (software := software_gpu(scenario, drew, adapter)) is not None:
                            result = _finish({"name": scenario.name, "url": scenario.url, "goal": scenario.goal,
                                              "mode": scenario.mode, "checks": [], "findings": [], "screens": [],
                                              "stop": "software_gpu"}, "harness", software, time.monotonic())
                        else:
                            _emit(opts, "start", scenario=scenario.name)
                            with live.frames(run_dir, session.page_socket(), managed=suite_meta.get("managed", False)):
                                result = run_scenario(session, scenario, opts=opts, hosts=set(suite_meta["hosts"]),
                                                      run_dir=run_dir)
                except session_mod.TransportLost as error:
                    result = _transport_result(scenario, error, started)
                    lost_after = scenario.name
                except KeyboardInterrupt:  # e.g. while waiting for the machine to calm down
                    result = _finish({"name": scenario.name, "url": scenario.url, "goal": scenario.goal,
                                      "mode": scenario.mode, "checks": [], "findings": [], "screens": [],
                                      "stop": "interrupted", "interrupted": True},
                                     "harness", "interrupted: stopped by the operator", time.monotonic())
                if (error := session_mod.transport_error()) is not None:
                    result = _transport_result(scenario, error, started, result)
                    lost_after = scenario.name
                elif (crash := _page_crash(session, scenario)) is not None:
                    result = _crashed_result(scenario, crash, getattr(session, "doing", None), started, result)
                elif (stalled := session_mod.browser_stalled()) is not None:
                    result = _stalled_result(scenario, stalled, started, result)
            if not blocked_by:
                result["cost_usd"] = round(ledger.spent() - spent_before, 5)
            if gpu is not None:  # once per Chrome: lift_gpu moves it to the report's browser record
                result["browser_gpu"], gpu = gpu, None
            outcomes[scenario.name] = result["outcome"]
            results.append(result)
            _emit(opts, "scenario", result=result)
            if result.get("interrupted"):
                break
    finally:
        if session is not None:
            session.close()
        session_mod.stop_daemon()
    return {"results": results, "ledger": ledger.summary(), "bu_name": name}


_shared_cost = None


def _init_worker(shared, provider_bootstrap=None):
    global _shared_cost
    _shared_cost = shared
    if provider_bootstrap is not None:
        provider_bootstrap()


def _worker(payload):
    scenarios, opts_dict, cdp_url, suite_meta, run_dir, cap, per_call = payload
    opts = Options(**{**opts_dict, "emit": None})
    ledger = Ledger(cap, per_call, shared=_shared_cost)
    return run_group(scenarios, opts=opts, cdp_url=cdp_url, suite_meta=suite_meta, run_dir=run_dir, ledger=ledger)


# --------------------------------------------------------------------------------------------------------------
# A fresh Chrome per test

# Each test, or each depends_on chain, in its own new Chrome (suite browser: fresh, the default): a form left open, a
# dialog or a hung page in one test cannot hold up the next. #802 (30 Oct): one shared Chrome cost 5-6.5 min a step;
# one process per test with a 150 s cap passed 21 of 21, each in 9-17 s. A chain keeps one Chrome: its tests share a
# tab on purpose.
CAP_MARGIN = 60.0  # s beyond a test's budget.seconds (90 by default, so a 150 s cap): loading, hooks and checks
START_WAIT = 60.0  # QAJEV_FRESH_START_WAIT's default: s for each step before a test (Chrome up, sign-in, the next test)


def start_wait():
    """Seconds a fresh Chrome's chain may take for each step before a test starts: after Chrome is up, after the
    sign-in, and between two tests. Chrome's own start has QAJEV_CHROME_START_WAIT on top. Raises ConfigError."""
    text = os.environ.get("QAJEV_FRESH_START_WAIT", "")
    try:
        value = float(text) if text else START_WAIT
    except ValueError:
        value = -1.0
    if not value > 0:
        raise ConfigError(f"QAJEV_FRESH_START_WAIT must be a number of seconds above 0, not {text!r}")
    return value


def _chrome_start_wait():
    try:
        return float(os.environ.get("QAJEV_CHROME_START_WAIT", "60"))
    except ValueError:
        return 60.0


def test_cap(scenario):
    """How long one test may run in a fresh Chrome before it is stopped and graded harness."""
    return float(scenario.budget["seconds"]) + CAP_MARGIN


def browser_plan(suite, opts):
    """("fresh", None) when each chain gets its own Chrome, else ("shared", why one Chrome serves them all)."""
    if getattr(suite, "browser", "fresh") == "shared":
        return "shared", "the suite says browser: shared"
    if opts.cdp_url:
        return "shared", "the run uses a Chrome QAJev did not start (--cdp-url)"
    open_ = [r for r in chrome.status(opts.profile) if r.get("alive") and not r.get("ephemeral")]
    if open_ and not _fresh_ephemeral(opts):
        return "shared", f"profile {opts.profile!r}'s Chrome was already open, and QAJev does not close it"
    if opts.jobs > 1 and not _fresh_ephemeral(opts):  # a throwaway copy would lose the profile's sign-in
        return "shared", f"--jobs {opts.jobs} with the kept profile {opts.profile!r}: one Chrome at a time can use it"
    return "fresh", None


def _fresh_ephemeral(opts):
    """A throwaway profile for each fresh Chrome, as for a shared one: --ephemeral, or a machine running several."""
    return bool(opts.ephemeral or browser_pool.parallel())


def _fresh_chain(payload, messages):
    """One chain in its own new Chrome, in a child process the parent can stop at a test's cap. It tells the parent
    through `messages`: ("chrome", state key) once Chrome runs, each ("event", event) the chain emits (with
    ("ledger", summary) after each test), ("signin", what happened), then ("done", results and ledger)."""
    from . import session as session_mod

    suite, chain, opts_dict, suite_meta, run_dir, cap, per_call, shared, bootstrap, ephemeral = payload
    _init_worker(shared, bootstrap)
    ledger = Ledger(cap, per_call, shared=shared)
    told = set()

    def say(kind, body):
        """Only this process read the account's secrets (vault, password, codes, cookie): it redacts them from all it
        sends, and tells the parent what to redact as well, for the report it writes."""
        learnt = [v for v in remembered_secrets() if v not in told]
        if learnt:
            told.update(learnt)
            messages.put(("secrets", learnt))
        messages.put((kind, redact_tree(body, secret_values())))

    def emit(event):
        if event.get("event") == "start":  # by now its daemon runs, if a Session did not say so already
            session_mod.daemon_up()
        say("event", event)
        if event.get("event") == "scenario":
            say("ledger", ledger.summary())

    session_mod.listen_daemon(lambda pid: say("daemon", pid))  # each daemon a Session starts, sign-in's too
    handed = False  # run_group stops the daemon it used: a chain that does not get there stops its own
    fields = {f.name for f in dataclasses.fields(Options)}
    opts = Options(**{k: v for k, v in opts_dict.items() if k in fields}, emit=emit)
    for k, v in opts_dict.items():  # what run() set on top, such as login.tab: same's sign_in_here
        if k not in fields:
            setattr(opts, k, v)
    try:
        owned = chrome.start(opts.profile, headless=opts.headless, ephemeral=ephemeral, port=0 if ephemeral else None)
    except chrome.ChromeError as e:  # this chain's tests, not the run: the next chain gets its own try
        results = [_not_run(s, "harness", "browser_error", f"a fresh Chrome did not start: {redact(str(e))[:240]}")
                   for s in chain]
        for r in results:
            emit({"event": "scenario", "result": r})
        say("done", {"results": results, "ledger": ledger.summary(), "load_waited": opts.load_waited})
        return
    key = owned.get("state_key") or opts.profile
    say("chrome", key)
    try:
        if suite.account and not getattr(opts, "sign_in_here", None):  # login.tab: same signs in in run_group
            signed = sign_in_first(suite, opts=opts, cdp_url=owned["cdp_url"], headless=suite_meta["headless"],
                                   motion=suite_meta["motion"])
            say("signin", signed)
            opts.account = suite.account["name"] if signed["ok"] else None
            if not signed["ok"]:
                results = [_finish({"name": s.name, "url": s.url, "goal": s.goal, "mode": s.mode, "checks": [],
                                    "findings": [], "screens": [], "stop": "sign_in", "cost_usd": 0.0},
                                   *sign_in_grade(signed), time.monotonic()) for s in chain]
                for r in results:
                    emit({"event": "scenario", "result": r})
                say("done", {"results": results, "ledger": ledger.summary(), "load_waited": opts.load_waited})
                return
        handed = True
        out = run_group(chain, opts=opts, cdp_url=owned["cdp_url"], suite_meta=suite_meta, run_dir=run_dir,
                        ledger=ledger)
        signed_here = getattr(opts, "signed_here", None)
        if signed_here:
            say("signin", signed_here)
        say("done", {"results": out["results"], "ledger": out["ledger"], "load_waited": opts.load_waited})
    finally:
        if not handed:  # its sign-in's daemon (a failed sign-in, say): as try_sign_in does
            session_mod.stop_daemon()
        chrome.stop(key)


def _start_chain(ctx, payload):
    """A chain's child process and the queue it reports on (a test swaps this for a thread)."""
    messages = ctx.Queue()
    process = ctx.Process(target=_fresh_chain, args=(payload, messages), daemon=True)
    process.start()
    return process, messages


def _not_run(scenario, outcome, stop, reason):
    return _finish({"name": scenario.name, "url": scenario.url, "goal": scenario.goal, "mode": scenario.mode,
                    "checks": [], "findings": [], "screens": [], "stop": stop, "cost_usd": 0.0},
                   outcome, reason, time.monotonic())


def _stop_group(pid, grace=5.0):
    """Stop a process group this run's chain started in a session of its own (its browser daemon), by its pid: never
    by a name. A pid that leads no group now is someone else's: left alone."""
    try:
        if os.getpgid(pid) != pid:
            return
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def run_fresh(suite, chains, *, opts, suite_meta, run_dir, cap, emit):
    """Each chain in its own Chrome (_fresh_chain), at most opts.jobs at once. A test still running at its cap
    (test_cap) is stopped with its Chrome and graded harness, "cap N s hit"; the rest of its chain is not run. A wait
    for a quiet machine stops the clock. -> (results by name, ledger summaries, the first sign-in)."""
    import multiprocessing
    import queue as queue_mod

    ctx = multiprocessing.get_context("spawn")
    shared = ctx.Value("d", 0.0)
    opts_dict = {k: v for k, v in opts.__dict__.items() if k != "emit"}
    ephemeral = _fresh_ephemeral(opts)
    between = start_wait()
    results, ledgers, signed = {}, [], None
    waiting, running = list(chains), []

    def stop(job):
        """The chain's process, then what it started: its Chrome (by its state record, the whole process group) and
        its browser daemon (by the pid it said)."""
        job["process"].terminate()
        job["process"].join(5)
        if job["process"].is_alive():
            job["process"].kill()
            job["process"].join(5)
        if job["chrome"]:
            chrome.stop(job["chrome"], group=True)
        if job["daemon"]:
            _stop_group(job["daemon"])

    def report(scenario, result):
        results[scenario.name] = result
        emit({"event": "scenario", "result": result})

    def drain(job, signed, wait=0.0):
        """What a chain said since the last look. -> the first sign-in so far."""
        while True:
            try:
                kind, body = job["messages"].get(timeout=wait) if wait else job["messages"].get_nowait()
            except queue_mod.Empty:
                return signed
            now = time.monotonic()
            if kind == "secrets":  # what the chain read for its sign-in: this report is redacted of it too
                for value in body:
                    remember_secret(value)
            elif kind == "daemon":
                job["daemon"] = body
            elif kind == "chrome":
                job["chrome"] = body
                job["deadline"] = now + between
            elif kind == "ledger":
                job["ledger"] = body
            elif kind == "signin":
                signed = signed or body
            elif kind == "done":
                job["done"] = True
                job["ledger"] = body["ledger"]
                job["waited"] = body.get("load_waited", job["waited_from"]) - job["waited_from"]
                for r in body["results"]:
                    results.setdefault(r["name"], r)
            elif kind == "event":
                event = body.get("event")
                if event == "start":
                    job["current"] = next(s for s in job["chain"] if s.name == body["scenario"])
                    job["deadline"] = now + test_cap(job["current"])
                elif event == "scenario":
                    results[body["result"]["name"]] = body["result"]
                    job["current"] = None
                    job["deadline"] = now + between
                elif event == "signin" and not job["current"]:  # a sign-in started or ended: the next step's time
                    job["deadline"] = now + between
                elif event == "waiting" and job["paused"] is None:  # the clock stops for a quiet-machine wait...
                    job["paused"] = now
                elif event == "waited" and job["paused"] is not None:  # ...and goes on from where it stopped
                    job["deadline"] += now - job["paused"]
                    job["paused"] = None
                emit(body)

    def late(job):
        now = time.monotonic()
        if job["paused"] is not None:  # a wait is bounded by the run's load budget: past it, the chain is stuck
            return now > job["paused"] + max(0.0, opts.load_wait - opts.load_waited) + between
        return now > job["deadline"]

    try:
        while waiting or running:
            while waiting and len(running) < max(1, opts.jobs):
                chain = waiting.pop(0)
                payload = (suite, chain, {**opts_dict, "load_waited": opts.load_waited}, suite_meta, run_dir, cap,
                           opts.typesafe_usd_per_call, shared, providers.WORKER_BOOTSTRAP, ephemeral)
                process, messages = _start_chain(ctx, payload)
                running.append({"chain": chain, "process": process, "messages": messages, "chrome": None,
                                "daemon": None, "current": None, "paused": None, "ledger": None, "done": False,
                                "deadline": time.monotonic() + _chrome_start_wait() + between,
                                "waited_from": opts.load_waited, "waited": 0.0})
            for job in list(running):
                signed = drain(job, signed)
                over = not job["done"] and late(job)
                if not job["done"] and not over and job["process"].is_alive():
                    continue
                if not job["done"] and not over:  # it ended: what it said last can still be on its way
                    signed = drain(job, signed, wait=1.0)
                if over:
                    busy = job["paused"] is not None  # cut in a quiet-machine wait, not by the test's own time
                    stop(job)
                    current = job["current"]
                    for s in job["chain"]:
                        if s.name in results:
                            continue
                        if busy and (current is None or s.name == current.name):
                            report(s, _not_run(s, "harness", "machine_busy", "the machine stayed busy past the "
                                               f"run's load budget (--load-wait {opts.load_wait:.0f} s)"))
                        elif current is None:
                            report(s, _not_run(s, "harness", "browser_error", "a fresh Chrome's chain did not get "
                                               f"to its next test within {between:.0f} s (QAJEV_FRESH_START_WAIT)"))
                        elif s.name == current.name:
                            report(s, _not_run(s, "harness", "cap", f"cap {test_cap(s):.0f} s hit"))
                        else:
                            why = "the machine stayed busy" if busy else "hit its cap"
                            report(s, _not_run(s, "skipped", "machine_busy" if busy else "cap",
                                               f"not run: {current.name!r} before it in its chain: {why}"))
                else:
                    if job["done"]:
                        job["process"].join(5)
                    code = job["process"].exitcode
                    if not job["done"]:  # a child that died without saying so: what it started is stopped as at a cap
                        stop(job)
                    for s in job["chain"]:  # its own tests, not the whole run's
                        if s.name not in results:
                            report(s, _not_run(s, "harness", "browser_error",
                                               f"the test's browser process ended (exit {code})"))
                opts.load_waited += job["waited"]  # load_wait is one budget for the whole run, as in one Chrome
                ledgers.append(job["ledger"] or Ledger(cap).summary())
                running.remove(job)
            time.sleep(0.2)
    except KeyboardInterrupt:
        for job in running:
            stop(job)
        raise
    return results, ledgers, signed


# --------------------------------------------------------------------------------------------------------------
# Suite


def real_device_run(suite, scenarios, platform, opts, cap, run_dir, emit):
    """The website's scenarios as one session in a QAJev-owned device browser. -> (results, ledger summary)."""
    from types import SimpleNamespace

    from . import mobile, native

    steps = mobile.web_steps(SimpleNamespace(scenarios=scenarios), platform)
    first = next((st["open"] for st in steps if st.get("open")), None)
    ledger = Ledger(cap, opts.typesafe_usd_per_call)
    if not first:
        return [], ledger.summary()
    device = os.environ.get("QAJEV_IOS_DEVICE" if platform == "ios" else "QAJEV_ANDROID_AVD")
    try:
        with mobile.MobileApp(f"{platform}:{first}", device=device, readonly=True) as game:
            results = native.run_session(game, steps, ledger=ledger, run_dir=run_dir, shots=opts.shots, emit=emit)
    except native.NativeError as e:
        results = [{"name": st["name"], "outcome": "harness", "stop": "browser_error", "checks": [], "findings": [],
                    "screens": [], "reason": f"{mobile.BROWSER_NAMES[platform]} unavailable: {e}"} for st in steps]
    capture = {f"{s.name} ({mobile.BROWSER_NAMES[platform]})" for s in scenarios if s.canvas_capture}
    for result in results:  # the readback runs over CDP in QAJev's Chrome: a device browser gives none
        if result.get("name") in capture:
            result["canvas_capture"] = {"problem": "not run in a device browser"}
            result["outcome"], result["reason"] = canvas_capture_grade(result.get("outcome"), result.get("reason", ""),
                                                                       result["canvas_capture"])
    return results, ledger.summary()


def _sign_in_crash(session, done):
    """The sign-in page crashed (session.page_crash): the reason says so, and sign_in_grade fails the tests."""
    crash = session.page_crash() if session is not None and hasattr(session, "page_crash") else None
    if crash is not None:
        url = crash.get("url") or "the sign-in page"
        done.update(ok=False, page_crashed={"url": url, "last_step": "signing in"},
                    reason=f"the page crashed at {url} while signing in")


def sign_in_grade(signed):
    """(outcome, reason) of a test that did not run because its sign-in failed: harness, as QAJev could not sign
    in, or fail when the sign-in page crashed (a crash is the product's fault, never harness)."""
    if signed.get("page_crashed"):
        return "fail", signed["reason"]
    return "harness", f"sign-in failed: {signed['reason']}"


def sign_in_here(session, account, opts):
    """login.tab: same (foleyapp, 13 Oct): the account signed in in the scenarios' own tab, for an app that keeps one
    writer per origin (Foley's OPFS database) and must not see a second tab. -> what happened, as sign_in_first."""
    from . import signin, vault

    _emit(opts, "signin", account=account["name"])
    try:
        done = signin.sign_in(session, account)
    except (signin.SignInFailed, vault.VaultError, RuntimeError, TimeoutError, OSError) as e:
        done = {"account": account["name"], "ok": False, "reason": redact(str(e))[:300]}
        seen = signin.page_seen(session)
        if seen:
            done["reason"] = f"{done['reason']} (the page showed: {redact(seen)[:240]})"
        _sign_in_crash(session, done)
    done["tab"] = "same"
    opts.account = account["name"] if done["ok"] else None
    _emit(opts, "signin", **done)
    return done


def sign_in_first(suite, *, opts, cdp_url, headless, motion):
    """The suite's account signed in once, in its own unguarded tab, before any scenario. -> what happened."""
    from . import session as session_mod
    from . import signin, vault

    _emit(opts, "signin", account=suite.account["name"])
    session = None
    try:
        session_mod.configure_env(cdp_url)
        session = session_mod.Session(Ledger(0.0), headless=headless, hosts=suite.hosts, guard_opts=suite.guard,
                                      motion=motion)
        done = signin.sign_in(session, suite.account)
    except (signin.SignInFailed, vault.VaultError, RuntimeError, TimeoutError, OSError) as e:
        done = {"account": suite.account["name"], "ok": False, "reason": redact(str(e))[:300]}
        # What the page showed, so a failed sign-in can be read without a second run (Foley's dry check, 13 Oct).
        seen = signin.page_seen(session) if session is not None else ""
        if seen:
            done["reason"] = f"{done['reason']} (the page showed: {redact(seen)[:240]})"
        _sign_in_crash(session, done)
    finally:
        if session is not None:
            session.close()
    _emit(opts, "signin", **done)
    return done


def run(suite, opts):
    from . import report

    scenarios = select(suite, opts.only, exact=opts.only_exact)
    try:
        models = providers.describe(providers.resolve())
        if any(s.goal for s in scenarios) and models["jev"] == "none":
            raise ConfigError("goals need an OpenRouter key (OPENROUTER_API_KEY) or TYPESAFE_API_KEY for Jev, or "
                              "QAJEV_JEV_PROVIDER=cloudflare with CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN for "
                              "Clef; see `qajev doctor`")
    except providers.ProviderError as e:
        raise ConfigError(str(e)) from None
    sees = [s for s in scenarios if s.vision or s.expect.get("looks") or (s.expect.get("motion") or {}).get("looks")]
    if sees:
        refused = vision.require_clef("vision" if any(s.vision for s in sees) else "looks")
        if refused:
            raise ConfigError(f"{refused} ({sees[0].name!r} asks for it)")
    if any(s.uses_commands for s in scenarios) and not opts.allow_commands:
        raise ConfigError("this suite runs shell commands; pass --allow-commands if you trust it")

    reaped = chrome.reap()
    if reaped:
        _emit(opts, "reaped", items=reaped)
    started_at = time.time()
    opts.out_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=f"{time.strftime('%Y%m%d-%H%M%S')}-{slug(suite.name)}-", dir=opts.out_dir))
    owned = None
    plan_mode, shared_because = browser_plan(suite, opts)
    if plan_mode == "fresh":  # each chain starts its own Chrome (run_fresh)
        start_wait()  # a bad QAJEV_FRESH_START_WAIT stops the run here, before any Chrome
        cdp_url = None
        browser = {"managed": True, "fresh": True, "profile": opts.profile, "ephemeral": _fresh_ephemeral(opts),
                   "headless": bool(opts.headless)}
    elif opts.cdp_url:
        cdp_url = opts.cdp_url.rstrip("/")
        if not chrome.version(cdp_url):
            raise ConfigError(f"no Chrome DevTools endpoint at {chrome.shown_cdp_url(cdp_url)}")
        browser = {"cdp_url": chrome.shown_cdp_url(cdp_url), "managed": False}
    else:
        owned = chrome.start(opts.profile, headless=opts.headless, ephemeral=opts.ephemeral or browser_pool.parallel(),
                             port=0 if browser_pool.parallel() else None)
        cdp_url = owned["cdp_url"]
        browser = chrome.run_record(owned)
    if plan_mode == "shared" and suite.browser == "fresh":
        browser["shared_because"] = shared_because  # the report says why the default did not apply
    _emit(opts, "run", suite=suite.name, run_dir=str(run_dir), browser=browser, scenarios=len(scenarios),
          decider=models.get("decider"))
    _emit(opts, "plan", items=plan.from_suite(scenarios))  # what each test will prove, shown before it runs

    cap = opts.cost_cap_usd if opts.cost_cap_usd is not None else suite.cost_cap_usd
    motion = opts.motion or getattr(suite, "motion", "reduce")
    cpu_throttle = opts.cpu_throttle or getattr(suite, "cpu_throttle", 1)
    suite_meta = {"headless": browser.get("headless", False), "hosts": suite.hosts, "guard": suite.guard,
                  "cwd": str(suite.path.parent) if suite.path else None, "motion": motion,
                  "cpu_throttle": cpu_throttle,
                  "managed": bool(browser.get("managed"))}  # QAJev's own Chrome: a LIVE viewer may bring it to front
    chains = groups(scenarios)
    results_by_name, ledgers, interrupted = {}, [], False
    partial = report.Partial(run_dir, suite, browser, started_at, cap)
    user_emit = opts.emit

    def emit(event):
        if event.get("event") == "scenario":
            partial.add(event["result"])
        if callable(user_emit):
            user_emit(event)

    opts.emit = emit
    signed = None
    try:
        same_tab = bool(suite.account) and suite.account["login"].get("tab") == "same"
        if same_tab:
            opts.sign_in_here = suite.account  # run_group signs in in the scenarios' tab (one group: one tab)
        if suite.account and not same_tab and plan_mode == "shared":
            signed = sign_in_first(suite, opts=opts, cdp_url=cdp_url, headless=suite_meta["headless"], motion=motion)
            opts.account = suite.account["name"] if signed["ok"] else None
            if opts.jobs > 1 and len(chains) > 1:  # the workers start their own daemons; this one is done
                from . import session as session_mod

                session_mod.stop_daemon()
        if plan_mode == "fresh":  # each fresh Chrome signs the account in again: a new Chrome has no session cookie
            fresh, fresh_ledgers, signed = run_fresh(suite, chains, opts=opts, suite_meta=suite_meta, run_dir=run_dir,
                                                     cap=cap, emit=emit)
            results_by_name.update(fresh)
            ledgers += fresh_ledgers
        elif signed and not signed["ok"]:  # Jev must not meet a sign-in page it cannot pass: stop with the reason
            for s in scenarios:
                r = _finish({"name": s.name, "url": s.url, "goal": s.goal, "mode": s.mode, "checks": [],
                             "findings": [], "screens": [], "stop": "sign_in"},
                            *sign_in_grade(signed), time.monotonic())
                results_by_name[s.name] = r
                emit({"event": "scenario", "result": r})
        elif opts.jobs > 1 and len(chains) > 1 and not same_tab:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor, as_completed

            ctx = multiprocessing.get_context("spawn")
            shared = ctx.Value("d", 0.0)
            opts_dict = {k: v for k, v in opts.__dict__.items() if k != "emit"}
            payloads = [(chain, opts_dict, cdp_url, suite_meta, run_dir, cap, opts.typesafe_usd_per_call)
                        for chain in chains]
            with ProcessPoolExecutor(max_workers=min(opts.jobs, len(chains)), mp_context=ctx,
                                     initializer=_init_worker, initargs=(shared, providers.WORKER_BOOTSTRAP)) as pool:
                futures = {pool.submit(_worker, p): p[0] for p in payloads}
                for future in as_completed(futures):
                    try:
                        out = future.result()
                    except Exception as e:  # a crashed worker costs its own chain, not the whole run
                        out = {"ledger": Ledger(cap).summary(), "results": [
                            _finish({"name": s.name, "url": s.url, "goal": s.goal, "mode": s.mode, "checks": [],
                                     "findings": [], "screens": [], "stop": "browser_error"},
                                    "harness", f"worker failed: {type(e).__name__}: {e}", time.monotonic())
                            for s in futures[future]]}
                    ledgers.append(out["ledger"])
                    for r in out["results"]:
                        results_by_name[r["name"]] = r
                        emit({"event": "scenario", "result": r})
        else:
            ledger = Ledger(cap, opts.typesafe_usd_per_call)
            out = run_group(scenarios, opts=opts, cdp_url=cdp_url, suite_meta=suite_meta, run_dir=run_dir,
                            ledger=ledger)
            ledgers.append(out["ledger"])
            results_by_name.update({r["name"]: r for r in out["results"]})
    except KeyboardInterrupt:
        interrupted = True
    finally:
        opts.emit = user_emit
        if owned and owned.get("ephemeral"):
            chrome.stop(owned["state_key"])

    for r in partial.results:  # an interrupt must not throw away scenarios that already finished
        results_by_name.setdefault(r["name"], r)
    results = [results_by_name[s.name] for s in scenarios if s.name in results_by_name]
    lift_gpu(browser, results)
    interrupted = interrupted or any(r.get("interrupted") for r in results)
    if not interrupted:  # opt-in: the same scenarios in a real device browser (iOS Safari, Android Chrome)
        for platform in getattr(suite, "real_devices", None) or []:
            device_results, device_ledger = real_device_run(suite, scenarios, platform, opts, cap, run_dir, emit)
            results += device_results
            ledgers.append(device_ledger)
    built = report.build(suite, results, ledgers, browser=browser, started_at=started_at, strict=opts.strict,
                         interrupted=interrupted, run_dir=run_dir)
    built["models"] = models
    built["motion"] = motion
    built["cpu_throttle"] = cpu_throttle
    if measured := browser_pool.timing():
        built["timing"] = measured
    if live.watched_seconds(run_dir):  # someone watched it LIVE in the dashboard: for how long
        built["watched_live_s"] = live.watched_seconds(run_dir)
    signed = signed or getattr(opts, "signed_here", None)
    if signed:
        built["sign_in"] = signed
    built = redact_tree(built, secret_values())
    report.write(run_dir, built)
    _emit(opts, "done", gate=built["gate"], run_dir=str(run_dir))
    return built
