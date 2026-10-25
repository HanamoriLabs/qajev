"""Runs a suite: one guarded tab per worker, scenarios in order, a verdict per scenario.

Parallelism is one process per worker: browser_harness reads BU_NAME at import, so a process can only
own one daemon. Every worker opens its own tab in the same QAJev Chrome and stops its own daemon.
"""

import math
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import browser_pool, chrome, live, plan, providers, ux, verdict, vision, watchdog
from .config import redact, redact_tree, secret_values
from .diagnostics import keep_console
from .ledger import CostCapReached, Ledger
from .suite import has_checks
from .suite import page_checks as has_page_checks
from .ux_options import resolve as resolve_ux

MODEL_ERROR = re.compile(r"model|typesafe|text helper|text_model|provider", re.I)
STALE_LIMIT = 8  # decisions without an executed action before we call the page churning
# The expectations a run that stopped early may never reach, as its reason names them.
NOT_RUN = {"looks": "expect.looks", "after": "the after hooks", "fetch": "expect.fetch", "command": "expect.command",
           "effect": "expect.effect"}
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
    shots: bool = True
    ux: str | None = None  # None inherits suite; default advisory. No model calls.
    ux_checks: list | None = None
    ux_keyboard_scope: str | None = None
    ux_keyboard_ratio: float | None = None
    ux_keyboard_main: str | None = None
    ux_keyboard_press_return: bool | None = None
    ux_settle_ms: int | None = None
    ux_shift_px: float | None = None
    ux_tap_min_px: float | None = None
    ux_max_findings: int | None = None
    motion: str | None = None  # overrides the suite's motion (reduce | full)
    color_scheme: str | None = None  # light | dark for every test that names none (a test's own wins)
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
    watchdog.notify("queued")
    try:
        while load >= opts.load_ok:
            if time.monotonic() - started > remaining:
                return False, load
            time.sleep(5)
            load = os.getloadavg()[0]
    finally:
        opts.load_waited += time.monotonic() - started
        watchdog.notify("admitted")
        watchdog.notify("runtime", value=_runtime_state(opts))
    return True, load


def select(suite, only):
    """--only NAME keeps NAME plus everything it depends on."""
    if not only:
        return list(suite.scenarios)
    by_name = {s.name: s for s in suite.scenarios}
    missing = [n for n in only if n not in by_name]
    if missing:
        raise ConfigError(f"no scenario named {missing}; have {list(by_name)}")
    keep, todo = set(), list(only)
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

    spec = scenario.expect["effect"]
    if run_dir is None:
        raise PixelError("effect comparison needs a report folder for its evidence")
    frames = {}
    for state in ("off", "on"):
        try:
            for n, hook in enumerate(spec[state], 1):
                watchdog.call(f"effect {state} hook {n} ({next(iter(hook))})", session.run_hook, hook, url)
            frames[state] = watchdog.call(f"effect {state} capture", session.capture_region_png, spec["region"])
            if not isinstance(frames[state], bytes) or len(frames[state]) > EFFECT_MAX_BYTES:
                raise PixelError("PNG capture is missing or exceeds the 32 MiB evidence limit")
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


def run_scenario(session, scenario, *, opts, hosts, run_dir):
    from .session import GuardMissing, HookFailed, LeftSite, require_transport, transport_error

    quiet, load1 = wait_for_quiet(opts)
    started = time.monotonic()
    if hasattr(session, "begin_test"):  # the last test's page: its frames were named under that test
        session.begin_test()
    session.assists = []  # this scenario's own; a hook failing before Jev's loop must not inherit the last one's
    session.react_log, session.react_frames = [], 0  # frames and releases from react hooks, this scenario's own
    session.shot_dir = run_dir / "shots" if opts.shots and run_dir else None
    session.shot_prefix = slug(scenario.name)
    scheme = scenario.color_scheme or getattr(opts, "color_scheme", None)
    result = {
        "name": scenario.name, "url": scenario.url, "goal": scenario.goal, "mode": scenario.mode,
        "device": scenario.device, "load1": round(load1, 1), "checks": [], "findings": [], "screens": [],
        "blocked_writes": [], "guard_hidden": 0, "history": [], "jev": None, "shot": None,
        **({"color_scheme": scheme} if scheme else {}),
    }
    if not quiet:
        return _finish(result, "skipped", f"machine busy (load1 {load1:.0f})", started)

    findings = []
    observed = {}
    stop, detail = None, None
    before = None  # the page's checks as Jev found it, before its first move
    ux_observer = None
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
        watchdog.active("page checks (polling does not count as progress)")
        observed = absorb(session.probe(scenario.expect))
        require_transport()
        watchdog.notify("checkpoint", partial={**result, "checks": verdict.page_checks(scenario.expect, observed)})
        return observed

    def page_ok():
        return has_page_checks(scenario.expect) and verdict.all_ok(verdict.page_checks(scenario.expect, observed))

    try:
        if hasattr(session, "set_color_scheme"):  # a stand-in session in tests may not have it
            watchdog.call("color scheme", session.set_color_scheme, scheme)
        watchdog.call("device setup", session.set_device, scenario.device)
        watchdog.call("guard setup", session.arm, scenario.mode, scenario.speech)
        if hasattr(session, "configure_ux"):
            ux_observer = session.configure_ux(opts, run_dir, scenario.name, getattr(scenario, "ux_options", None))
        effect = scenario.expect.get("effect") or {}
        if any("pad" in hook for hook in [*scenario.before, *scenario.after, *effect.get("off", []),
                                         *effect.get("on", [])]):
            session.use_pad()  # before the page loads: a game may read its pads only once, at load
        if scenario.url:
            session.check_host(scenario.url)
            error = watchdog.call("navigation", session.navigate, scenario.url)
            if error:
                stop, detail = "unreachable", error
        if stop is None:
            if ux_observer:
                ux_observer.safe(ux_observer.start)
            # What it is doing, for the dashboard and qajev top, while a slow check runs in the page (a script that
            # plays the game for a minute said nothing at all before).
            _emit(opts, "step", scenario=scenario.name, at=time.time(),
                  doing="reading the page and running its checks" if has_page_checks(scenario.expect)
                  else "reading the page")
            read()
            watchdog.complete("initial page checks")
            findings += verdict.document_findings(observed.get("status"), scenario.expect, scenario=scenario.name,
                                                  url=observed.get("url"))
            for n, hook in enumerate(scenario.before, 1):
                watchdog.call(f"before hook {n} ({next(iter(hook))})", session.run_hook,
                              hook, observed.get("url") or scenario.url)
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
                    stop, detail = drive(session, scenario, read, page_ok, started, step, decided, held=held)
            else:
                stop = "checked"
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
    ran = set()  # the expectations beyond the page's own that did run
    if stop not in {"unreachable", "interrupted"} and transport_error() is None:
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
            measured_effect = None
            if scenario.expect.get("effect") and stop not in verdict.HARNESS_STOPS:
                measured_effect, result["effect"] = _effect_check(session, scenario, run_dir,
                                                                 observed.get("url") or scenario.url)
                ran.add("effect")
                read()  # ordinary page checks and looks judge the final, enabled state
            checks = verdict.page_checks(scenario.expect, observed)
            watchdog.complete("final page checks", partial={**result, "checks": checks})
            if measured_effect is not None:
                checks.append(measured_effect)
            if scenario.expect.get("looks") and stop not in verdict.HARNESS_STOPS:
                watchdog.active("expect.looks")
                structured = any(isinstance(entry, dict) for entry in scenario.expect["looks"])
                image = session.screen_image_png() if structured else session.screen_image()
                try:
                    checks += vision.look(session.jev.model.post_json, image, scenario.expect["looks"],
                                          {"page": {"url": observed.get("url"), "title": observed.get("title")}},
                                          folder=getattr(session, "cwd", None), evidence_dir=run_dir)
                    ran.add("looks")
                    watchdog.complete("expect.looks", partial={**result, "checks": checks})
                except (RuntimeError, ValueError) as e:  # Clef failed, not the browser
                    stop, detail = "model_error", f"while judging looks: {e}"
            if stop not in verdict.HARNESS_STOPS:
                ran.add("after")  # a hook that fails is named by the stop itself, not as never run
                for n, hook in enumerate(scenario.after, 1):
                    watchdog.call(f"after hook {n} ({next(iter(hook))})", session.run_hook,
                                  hook, observed.get("url") or scenario.url)
                says = scenario.expect.get("says") or {}  # the author's words for these checks (the test plan)
                for n, probe in enumerate(scenario.expect.get("fetch", []), 1):
                    response = watchdog.call(f"fetch check {n}", session.fetch, probe)
                    checks.append(verdict.fetch_check(probe, response or {}))
                    watchdog.notify("checkpoint", partial={**result, "checks": checks})
                    if says.get("fetch"):
                        checks[-1]["says"] = says["fetch"]
                ran.add("fetch")
                if scenario.expect.get("command"):
                    spec = scenario.expect["command"]
                    response = watchdog.call("command check", session.command, spec["run"], observed.get("url"))
                    checks.append(verdict.command_check(spec, response))
                    watchdog.notify("checkpoint", partial={**result, "checks": checks})
                    if says.get("command"):
                        checks[-1]["says"] = says["command"]
                ran.add("command")
        except HookFailed as e:
            stop, detail = "hook_failed", str(e)
        except (RuntimeError, ValueError, TimeoutError, OSError) as e:
            if stop in {"done", "reached", "checked", "blocked"}:
                stop, detail = "browser_error", f"while checking: {type(e).__name__}: {e}"
        if opts.shots and run_dir and transport_error() is None:
            shot = watchdog.call("scenario screenshot", session.screenshot,
                                 run_dir / "shots" / f"{slug(scenario.name)}.jpg")
            result["shot"] = str(shot.relative_to(run_dir)) if shot else None

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
              "command": scenario.expect.get("command"), "effect": scenario.expect.get("effect")}
    not_run = [NOT_RUN[k] for k, v in wanted.items() if v and k not in ran]
    outcome, reason = verdict.classify(stop, checks, has_checks=has_checks(scenario.expect), stop_detail=detail,
                                       not_run=not_run, actions=(result.get("jev") or {}).get("actions"),
                                       url=scenario.url, before=before)
    result.update({k: v for k, v in (("stop_detail", detail), ("not_run", not_run)) if v})
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
        watchdog.active("UX final checks")
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


def _last_stale(session):
    """The last stale decision's reason, for a "stale" stop: "; the last: <Jev's reason> <what changed>"."""
    words = verdict.stale_words(getattr(session, "last_stale", None))
    return f"; the last: {words}" if words else ""


def _progress_action(history):
    # Pinned Jev appends only after Browser.act returns, then records the post-action fingerprint.
    # Missing/unknown kinds, waits, failed/stale observations and no-op histories are not proof of progress.
    executed = history.get("executed_ms")
    return (history.get("kind") in {"click", "fill", "select", "scroll"}
            and type(executed) in {int, float} and math.isfinite(executed) and executed >= 0
            and history.get("page_changed") is True)


def drive(session, scenario, read, page_ok, started, step=None, decided=None, held=False):
    """Jev's loop with QAJev's judgment around it. Returns (stop, detail). step(doing, **extra) hears each move;
    decided(decision) hears each of the model's decisions as it is made, stale ones too (qajev top's decisions view).
    held: the page's checks already held before Jev's first move, so they count as reached only after one."""
    agent = session.agent
    state = agent.state
    step = step or (lambda doing, **extra: None)
    told = heard = 0
    deadline = started + scenario.budget["seconds"]
    reasks = escapes = model_failures = stale_base = 0
    assists = session.assists = []
    while True:
        read()
        # A goal whose checks held before it began is not reached by holding still: the Orchestrator's sign-in goal
        # ended "reached" in 5 s with 0 decisions, its url check matching the sign-in page's ?next= (14 Oct).
        if page_ok() and (state["history"] or not held):
            return "reached", None
        if state["status"] == "done":
            step("said DONE", n=len(state["history"]) + 1)
            return "done", None
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
            watchdog.active("Jev decision/action")
            session.tick()
            model_failures = 0
            for h in state["history"][told:]:
                step(_did(h), p=h.get("probability"), n=h.get("step"))
                if _progress_action(h):
                    watchdog.complete(f"Jev action {h.get('step', told + 1)}")
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

    quiet, load1 = wait_for_quiet(opts)
    started = time.monotonic()
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
                    "stop": "browser_error", "transport_lost": True, "session_uncertain": True,
                    "stop_detail": str(error)},
                   "harness", f"browser/daemon transport lost: {error}; no automatic retry", started)


def run_group(scenarios, *, opts, cdp_url, suite_meta, run_dir, ledger, prior=None):
    from . import session as session_mod

    results = []
    outcomes = dict(prior or {})
    name = session_mod.configure_env(cdp_url)
    watchdog.notify("resource", daemon=name)
    session = None
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
            if blocked_by:
                result = _finish({"name": scenario.name, "url": scenario.url, "goal": scenario.goal,
                                  "mode": scenario.mode, "checks": [], "findings": [], "screens": []},
                                 "skipped", f"depends on {blocked_by} which did not pass", time.monotonic())
            else:
                watchdog.notify("scenario", name=scenario.name)
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
                            if getattr(opts, "sign_in_here", None) and not getattr(opts, "signed_here", None):
                                opts.sign_in_attempted = True
                                opts.signed_here = sign_in_here(session, opts.sign_in_here, opts)
                        if (startup_error := session_mod.transport_error()) is not None:
                            raise startup_error
                        signed_here = getattr(opts, "signed_here", None)
                        if signed_here and not signed_here["ok"]:
                            result = _finish({"name": scenario.name, "url": scenario.url, "goal": scenario.goal,
                                              "mode": scenario.mode, "checks": [], "findings": [], "screens": [],
                                              "stop": "sign_in"}, "harness", f"sign-in failed: {signed_here['reason']}",
                                             time.monotonic())
                            result["cost_usd"] = 0.0
                            results.append(result)
                            outcomes[scenario.name] = result["outcome"]
                            _emit(opts, "scenario", result=result)
                            continue
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
            if not blocked_by:
                result["cost_usd"] = round(ledger.spent() - spent_before, 5)
            outcomes[scenario.name] = result["outcome"]
            results.append(result)
            _emit(opts, "scenario", result=result)
            if result.get("interrupted"):
                break
    finally:
        watchdog.active("session cleanup")
        if session is not None:
            session.close()
        session_mod.stop_daemon()
    return {"results": results, "ledger": ledger.summary(), "bu_name": name}


def _group_child(scenarios, opts_dict, cdp_url, suite_meta, run_dir, ledger_state, shared, prior, redaction_values):
    from .config import remember_secret

    # Spawn does not inherit process-local vault redaction state. Never stream those values back as data.
    for value in redaction_values:
        remember_secret(value)
    ledger = Ledger(ledger_state["cap_usd"], ledger_state["typesafe_usd_per_call"], shared=shared)
    for key, value in ledger_state.items():
        setattr(ledger, key, value)

    def emit(event):
        watchdog.notify("ledger", value=ledger.summary())
        watchdog.notify("runtime", value=_runtime_state(opts))
        watchdog.notify("event", value=event)

    opts = Options(**{k: v for k, v in opts_dict.items() if k in Options.__dataclass_fields__})
    # Sign-in state is a runtime option, intentionally not an extra public dataclass field.
    for key, value in opts_dict.items():
        setattr(opts, key, value)
    opts.emit = emit
    result = run_group(scenarios, opts=opts, cdp_url=cdp_url, suite_meta=suite_meta,
                       run_dir=run_dir, ledger=ledger, prior=prior)
    return {**result, "runtime": _runtime_state(opts)}


def _runtime_state(opts):
    return {key: getattr(opts, key) for key in ("load_waited", "account", "signed_here", "sign_in_attempted")
            if hasattr(opts, key)}


def _restore_runtime(opts, state):
    for key, value in state.items():
        if key in {"load_waited", "account", "signed_here", "sign_in_attempted"}:
            setattr(opts, key, value)


def supervised_group(scenarios, *, opts, cdp_url, suite_meta, run_dir, ledger, prior=None):
    """Production adapter: isolate blocking calls while preserving the group's one-session semantics."""
    pending = list(scenarios)
    results, outcomes = {}, dict(prior or {})
    last_name = None

    def keep(result):
        results[result["name"]] = result
        outcomes[result["name"]] = result["outcome"]

    def not_run(scenario, stop, reason):
        result = _finish({"name": scenario.name, "url": scenario.url, "goal": scenario.goal,
                          "mode": scenario.mode, "checks": [], "findings": [], "screens": [],
                          "stop": stop, "session_uncertain": True, "cost_usd": 0.0}, "skipped",
                         reason, time.monotonic())
        keep(result)
        _emit(opts, "scenario", result=result)

    while pending:
        opts_dict = {k: v for k, v in vars(opts).items() if k != "emit"}
        ledger_state = {k: v for k, v in vars(ledger).items() if k not in {"lock", "shared"}}
        out, failure = watchdog.supervise(_group_child,
            args=(pending, opts_dict, cdp_url, suite_meta, run_dir, ledger_state, ledger.shared,
                  outcomes, secret_values()), emit=opts.emit, cdp_url=cdp_url)
        if failure is None:
            _restore_runtime(opts, out.get("runtime", {}))
            for result in out["results"]:
                keep(result)
            _adopt_ledger(ledger, out["ledger"])
            last_name = out.get("bu_name")
            break
        _restore_runtime(opts, failure.pop("runtime"))
        for result in failure.pop("results"):
            keep(result)
        _adopt_ledger(ledger, failure.pop("ledger") or ledger.summary())
        if failure["cost_uncertain"]:
            ledger.uncertain = True
            if ledger.shared is not None:
                # Unknown in-flight cost reserves the remaining suite cap; another worker must not spend it.
                with ledger.shared.get_lock():
                    ledger.shared.value = max(ledger.shared.value, ledger.cap_usd)
        current = failure.get("scenario") or next((s.name for s in pending if s.name not in results),
                                                  pending[-1].name)
        partial = failure.pop("partial")
        scenario = next(s for s in pending if s.name == current)
        detail = f"{failure['detail']} while {failure['identity']}; session state uncertain; no automatic retry"
        if not failure["cleanup_complete"]:
            detail += "; owned browser cleanup incomplete"
        result = _finish({"name": scenario.name, "url": scenario.url, "goal": scenario.goal,
                          "mode": scenario.mode, "checks": [], "findings": [], "screens": [],
                          **(results.get(current) or partial), "stop": failure["stop"], "watchdog": failure,
                          "session_uncertain": True, "cost_incomplete": failure["cost_uncertain"],
                          **({"interrupted": True} if failure["stop"] == "interrupted" else {})},
                         "harness", detail, time.monotonic() - failure["active_seconds"])
        keep(result)
        _emit(opts, "scenario", result=result)
        remaining = pending[pending.index(scenario) + 1:]
        resume = (failure["stop"] == "no_progress" and failure["cleanup_complete"]
                  and not (getattr(opts, "sign_in_attempted", False) and not getattr(opts, "signed_here", None)))
        eligible = []
        for candidate in remaining:
            blocked = [d for d in candidate.depends_on if outcomes.get(d) != "pass"]
            if not resume:
                stop = "cleanup_incomplete" if not failure["cleanup_complete"] else "session_uncertain"
                not_run(candidate, stop, f"not run: {stop} after {current!r}; no automatic retry")
            elif blocked:
                not_run(candidate, "dependency", f"not run: depends on {blocked} which did not pass")
            elif not candidate.url:
                not_run(candidate, "session_uncertain",
                        "not run: previous page state was discarded; declare an own URL")
            else:
                eligible = remaining[remaining.index(candidate):]
                break  # The fresh URL restores a new chain; its normal dependencies/continuations run in order.
        # Each iteration spawns a fresh worker. No tab, daemon, action or page state crosses this boundary.
        # Keep the default browser context on the same Chrome. Persistent sign-in survives;
        # tab-local state does not, and the report must not imply a fresh login was performed.
        if eligible and getattr(opts, "signed_here", None):
            opts.signed_here = {**opts.signed_here, "watchdog_continuation": {
                "browser_context": "retained", "tab_state": "discarded", "login_replayed": False}}
        pending = eligible
    return {"results": [results[s.name] for s in scenarios], "ledger": ledger.summary(), "bu_name": last_name}


def _adopt_ledger(ledger, summary):
    """Keep one cumulative ledger when a timed-out worker is replaced."""
    ledger.usd = {"typesafe": summary["usd_typesafe_estimated"], "text": summary["usd_text"]}
    ledger.calls = dict(summary["calls"])
    ledger.tokens = dict(summary["tokens"])
    ledger.errors = summary["errors"]
    ledger.text_cost_reported = summary["text_cost_reported"]


_shared_cost = None


def _init_worker(shared):
    global _shared_cost
    _shared_cost = shared


def _worker(payload):
    scenarios, opts_dict, cdp_url, suite_meta, run_dir, cap, per_call = payload
    opts = Options(**{**opts_dict, "emit": None})
    ledger = Ledger(cap, per_call, shared=_shared_cost)
    return supervised_group(scenarios, opts=opts, cdp_url=cdp_url, suite_meta=suite_meta,
                            run_dir=run_dir, ledger=ledger)


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
    return results, ledger.summary()


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
    finally:
        if session is not None:
            session.close()
    _emit(opts, "signin", **done)
    return done


def run(suite, opts):
    from . import report

    scenarios = select(suite, opts.only)
    try:
        models = providers.describe(providers.resolve())
        if any(s.goal for s in scenarios) and models["jev"] == "none":
            raise ConfigError("goals need TYPESAFE_API_KEY or an OpenRouter key (OPENROUTER_API_KEY) for Jev, or "
                              "QAJEV_JEV_PROVIDER=cloudflare with CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN for "
                              "Clef; see `qajev doctor`")
    except providers.ProviderError as e:
        raise ConfigError(str(e)) from None
    sees = [s for s in scenarios if s.vision or s.expect.get("looks")]
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
    if opts.cdp_url:
        cdp_url = opts.cdp_url.rstrip("/")
        if not chrome.version(cdp_url):
            raise ConfigError(f"no Chrome DevTools endpoint at {cdp_url}")
        browser = {"cdp_url": cdp_url, "managed": False}
    else:
        owned = chrome.start(opts.profile, headless=opts.headless, ephemeral=opts.ephemeral or browser_pool.parallel(),
                             port=0 if browser_pool.parallel() else None)
        cdp_url = owned["cdp_url"]
        browser = chrome.run_record(owned)
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
        elif suite.account:
            signed = sign_in_first(suite, opts=opts, cdp_url=cdp_url, headless=suite_meta["headless"], motion=motion)
            opts.account = suite.account["name"] if signed["ok"] else None
            if opts.jobs > 1 and len(chains) > 1:  # the workers start their own daemons; this one is done
                from . import session as session_mod

                session_mod.stop_daemon()
        if signed and not signed["ok"]:  # Jev must not meet a sign-in page it cannot pass: stop with the reason
            for s in scenarios:
                r = _finish({"name": s.name, "url": s.url, "goal": s.goal, "mode": s.mode, "checks": [],
                             "findings": [], "screens": [], "stop": "sign_in"},
                            "harness", f"sign-in failed: {signed['reason']}", time.monotonic())
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
                                     initializer=_init_worker, initargs=(shared,)) as pool:
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
            out = supervised_group(scenarios, opts=opts, cdp_url=cdp_url, suite_meta=suite_meta, run_dir=run_dir,
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
