"""Runs a suite: one guarded tab per worker, scenarios in order, a verdict per scenario.

Parallelism is one process per worker: browser_harness reads BU_NAME at import, so a process can only
own one daemon. Every worker opens its own tab in the same QAJev Chrome and stops its own daemon.
"""

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import chrome, live, providers, verdict, vision
from .config import redact, redact_tree, secret_values
from .ledger import CostCapReached, Ledger
from .suite import has_checks
from .suite import page_checks as has_page_checks

MODEL_ERROR = re.compile(r"model|typesafe|text helper|text_model|provider", re.I)
STALE_LIMIT = 8  # decisions without an executed action before we call the page churning


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
    motion: str | None = None  # overrides the suite's motion (reduce | full)
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


def run_scenario(session, scenario, *, opts, hosts, run_dir):
    from .session import GuardMissing, HookFailed, LeftSite

    started = time.monotonic()
    session.assists = []  # this scenario's own; a hook failing before Jev's loop must not inherit the last one's
    quiet, load1 = wait_for_quiet(opts)
    result = {
        "name": scenario.name, "url": scenario.url, "goal": scenario.goal, "mode": scenario.mode,
        "device": scenario.device, "load1": round(load1, 1), "checks": [], "findings": [], "screens": [],
        "blocked_writes": [], "guard_hidden": 0, "history": [], "jev": None, "shot": None,
    }
    if not quiet:
        return _finish(result, "skipped", f"machine busy (load1 {load1:.0f})", started)

    findings = []
    observed = {}
    stop, detail = None, None

    def absorb(obs):
        probe = (obs or {}).get("probe") or {}
        findings.extend(verdict.findings_from_probe(probe, scenario=scenario.name, url=obs.get("url"),
                                                    first_party_hosts=hosts, why=getattr(session, "why_failed", None)))
        result["blocked_writes"].extend(probe.get("blocked") or [])
        result["guard_hidden"] = max(result["guard_hidden"], probe.get("hidden") or 0)
        if "hidden_controls" in probe:  # what the guard holds back on the page now
            result["guard_hidden_controls"] = probe["hidden_controls"]
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
        return observed

    def page_ok():
        return has_page_checks(scenario.expect) and verdict.all_ok(verdict.page_checks(scenario.expect, observed))

    try:
        session.set_device(scenario.device)
        session.arm(scenario.mode, scenario.speech)
        if scenario.url:
            session.check_host(scenario.url)
            error = session.navigate(scenario.url)
            if error:
                stop, detail = "unreachable", error
        if stop is None:
            # What it is doing, for the dashboard and qajev top, while a slow check runs in the page (a script that
            # plays the game for a minute said nothing at all before).
            _emit(opts, "step", scenario=scenario.name, at=time.time(),
                  doing="reading the page and running its checks" if has_page_checks(scenario.expect)
                  else "reading the page")
            read()
            findings += verdict.document_findings(observed.get("status"), scenario.expect, scenario=scenario.name,
                                                  url=observed.get("url"))
            for hook in scenario.before:
                session.run_hook(hook, observed.get("url") or scenario.url)
            if scenario.goal:
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

                with vision.seeing(session.screen_image if scenario.vision else None):
                    stop, detail = drive(session, scenario, read, page_ok, started, step, decided)
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
    if stop not in {"unreachable", "interrupted"}:
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
            checks = verdict.page_checks(scenario.expect, observed)
            if scenario.expect.get("looks") and stop not in verdict.HARNESS_STOPS:
                image = session.screen_image()
                try:
                    checks += vision.look(session.jev.model.post_json, image, scenario.expect["looks"],
                                          {"page": {"url": observed.get("url"), "title": observed.get("title")}})
                except (RuntimeError, ValueError) as e:  # Clef failed, not the browser
                    stop, detail = "model_error", f"while judging looks: {e}"
            if stop not in verdict.HARNESS_STOPS:
                for hook in scenario.after:
                    session.run_hook(hook, observed.get("url") or scenario.url)
                for probe in scenario.expect.get("fetch", []):
                    checks.append(verdict.fetch_check(probe, session.fetch(probe) or {}))
                if scenario.expect.get("command"):
                    spec = scenario.expect["command"]
                    checks.append(verdict.command_check(spec, session.command(spec["run"], observed.get("url"))))
        except HookFailed as e:
            stop, detail = "hook_failed", str(e)
        except (RuntimeError, ValueError, TimeoutError, OSError) as e:
            if stop in {"done", "reached", "checked", "blocked"}:
                stop, detail = "browser_error", f"while checking: {type(e).__name__}: {e}"
        if opts.shots and run_dir:
            shot = session.screenshot(run_dir / "shots" / f"{slug(scenario.name)}.jpg")
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
    result["end_url"] = observed.get("url")
    result["page_says"] = observed.get("says")
    findings += verdict.blank_finding(result.get("blank_ms", 0), scenario=scenario.name, url=observed.get("url"))
    result["findings"] = verdict.dedupe(findings)
    result["stop"] = stop
    outcome, reason = verdict.classify(stop, checks, has_checks=has_checks(scenario.expect), stop_detail=detail)
    idle = verdict.never_set_off(scenario.url, observed.get("url"), scenario.expect,
                                 session.agent.state["history"]) if scenario.goal else None
    if outcome in {"fail", "stuck"} and idle:
        outcome, reason = "harness", f"{idle}; {reason}"
    wall = verdict.sign_in_wall(scenario.url, observed, account=opts.account,
                                profile=None if opts.ephemeral or opts.profile == "default" else opts.profile)
    if outcome not in {"pass", "skipped"} and wall:
        result["needs_sign_in"] = wall
        return _finish(result, "harness", wall["reason"], started)
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
    reason = verdict.with_guard_note(outcome, reason, result.get("guard_hidden_controls") or [])
    return _finish(result, outcome, reason, started)


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


def drive(session, scenario, read, page_ok, started, step=None, decided=None):
    """Jev's loop with QAJev's judgment around it. Returns (stop, detail). step(doing, **extra) hears each move;
    decided(decision) hears each of the model's decisions as it is made, stale ones too (qajev top's decisions view)."""
    agent = session.agent
    state = agent.state
    step = step or (lambda doing, **extra: None)
    told = heard = 0
    deadline = started + scenario.budget["seconds"]
    reasks = escapes = model_failures = stale_base = 0
    assists = session.assists = []
    while True:
        read()
        if page_ok():
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
            session.tick()
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
        result = clients.run(scenario, ledger=ledger, hosts=set(suite_meta["hosts"]), run_dir=run_dir,
                             suite_meta=suite_meta, step=step)
    except KeyboardInterrupt:
        return _finish({**base, "stop": "interrupted", "interrupted": True}, "harness",
                       "interrupted: stopped by the operator", started)
    result["load1"] = round(load1, 1)
    return result


def run_group(scenarios, *, opts, cdp_url, suite_meta, run_dir, ledger, prior=None):
    from . import session as session_mod

    results = []
    outcomes = dict(prior or {})
    name = session_mod.configure_env(cdp_url)
    session = None
    try:
        for scenario in scenarios:
            blocked_by = [d for d in scenario.depends_on if outcomes.get(d) != "pass"]
            if blocked_by:
                result = _finish({"name": scenario.name, "url": scenario.url, "goal": scenario.goal,
                                  "mode": scenario.mode, "checks": [], "findings": [], "screens": []},
                                 "skipped", f"depends on {blocked_by} which did not pass", time.monotonic())
            elif scenario.clients:  # a multiplayer scenario: its own clients' tabs, not this tab, and no Jev
                _emit(opts, "start", scenario=scenario.name)
                spent_before = ledger.spent()
                result = run_clients(scenario, opts=opts, suite_meta=suite_meta, run_dir=run_dir, ledger=ledger)
            else:
                if session is None:
                    session = session_mod.Session(
                        ledger, headless=suite_meta["headless"], hosts=suite_meta["hosts"],
                        guard_opts=suite_meta["guard"], allow_commands=opts.allow_commands, cwd=suite_meta["cwd"],
                        motion=suite_meta.get("motion", "reduce"),
                    )
                _emit(opts, "start", scenario=scenario.name)
                spent_before = ledger.spent()
                try:
                    with live.frames(run_dir, session.page_socket()):  # its screen, while someone watches it
                        result = run_scenario(session, scenario, opts=opts, hosts=set(suite_meta["hosts"]),
                                              run_dir=run_dir)
                except KeyboardInterrupt:  # e.g. while waiting for the machine to calm down
                    result = _finish({"name": scenario.name, "url": scenario.url, "goal": scenario.goal,
                                      "mode": scenario.mode, "checks": [], "findings": [], "screens": [],
                                      "stop": "interrupted", "interrupted": True},
                                     "harness", "interrupted: stopped by the operator", time.monotonic())
            if not blocked_by:
                result["cost_usd"] = round(ledger.spent() - spent_before, 5)
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


def _init_worker(shared):
    global _shared_cost
    _shared_cost = shared


def _worker(payload):
    scenarios, opts_dict, cdp_url, suite_meta, run_dir, cap, per_call = payload
    opts = Options(**{**opts_dict, "emit": None})
    ledger = Ledger(cap, per_call, shared=_shared_cost)
    return run_group(scenarios, opts=opts, cdp_url=cdp_url, suite_meta=suite_meta, run_dir=run_dir, ledger=ledger)


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
    run_dir = opts.out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug(suite.name)}"
    run_dir.mkdir(parents=True, exist_ok=True)
    owned = None
    if opts.cdp_url:
        cdp_url = opts.cdp_url.rstrip("/")
        if not chrome.version(cdp_url):
            raise ConfigError(f"no Chrome DevTools endpoint at {cdp_url}")
        browser = {"cdp_url": cdp_url, "managed": False}
    else:
        owned = chrome.start(opts.profile, headless=opts.headless, ephemeral=opts.ephemeral)
        cdp_url = owned["cdp_url"]
        browser = {"cdp_url": cdp_url, "managed": True, "profile": owned["profile"], "headless": owned["headless"],
                   "port": owned["port"]}
    _emit(opts, "run", suite=suite.name, run_dir=str(run_dir), browser=browser, scenarios=len(scenarios),
          decider=models.get("decider"))

    cap = opts.cost_cap_usd if opts.cost_cap_usd is not None else suite.cost_cap_usd
    motion = opts.motion or getattr(suite, "motion", "reduce")
    suite_meta = {"headless": browser.get("headless", False), "hosts": suite.hosts, "guard": suite.guard,
                  "cwd": str(suite.path.parent) if suite.path else None, "motion": motion}
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
        if suite.account:
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
        elif opts.jobs > 1 and len(chains) > 1:
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
    if signed:
        built["sign_in"] = signed
    built = redact_tree(built, secret_values())
    report.write(run_dir, built)
    _emit(opts, "done", gate=built["gate"], run_dir=str(run_dir))
    return built
