"""No browser or models: real spawned workers exercise the production runner boundary."""

import contextlib
import ctypes
import functools
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from qajev import config, runner, session, suite, verdict, watchdog


def _blocked(mode, marker):
    watchdog.notify("scenario", name="blocked")
    watchdog.active("native check" if mode == "native" else "blocked check")
    Path(marker).write_text(str(os.getpid()))
    if mode == "native":
        # PyDLL deliberately holds the GIL: Python timer threads and signal handlers cannot run.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        ctypes.PyDLL(None).sleep(30)
    elif mode == "descendant":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        Path(marker + ".child").write_text(str(child.pid))
        time.sleep(30)
    elif mode == "noise":
        while True:
            watchdog.active("polling the same false condition")
            watchdog.notify("event", value={"event": "step", "at": time.time(), "doing": "animation"})
            time.sleep(0.01)
    elif mode == "interrupt":
        os.kill(os.getppid(), signal.SIGINT)
        time.sleep(30)
    elif mode == "success":
        return {"ok": True}
    elif mode == "pending":
        watchdog.notify("resource_pending", method="Target.createTarget", request_id="first")
        watchdog.notify("resource_pending", method="Target.createTarget", request_id="second")
        watchdog.notify("resource_resolved", method="Target.createTarget", request_id="first")
        time.sleep(30)
    else:
        time.sleep(30)


def _fixture_group_child(*args):
    """Only replace browser/model dependencies inside the spawned worker, never the runner path."""
    real = runner._group_child
    session.stop_daemon = lambda: None
    runner.live.frames = lambda *_a, **_kw: contextlib.nullcontext()
    notify = watchdog.notify

    def fixture_notify(kind, **data):
        if kind == "resource":
            data.pop("daemon", None)  # The fixture has no real daemon to own or stop.
        notify(kind, **data)

    watchdog.notify = fixture_notify
    from qajev import signin

    def sign_in(_page, account):
        with (args[4] / "signins.txt").open("a") as out:
            out.write(account["name"] + "\n")
        return {"account": account["name"], "ok": True}

    signin.sign_in = sign_in
    if args[1]["profile"] == "fixture-load":
        loads = iter([2, 0])
        runner.os.getloadavg = lambda: (next(loads, 2), 2, 2)
        sleep = time.sleep
        runner.time.sleep = lambda seconds: sleep(.02 if seconds == 5 else seconds)

    class Page:
        def __init__(self, ledger, **_kw):
            self.ledger = ledger
            self.url = "http://127.0.0.1/"
            with (args[4] / "generations.jsonl").open("a") as out:
                out.write(json.dumps({"pid": os.getpid(), "daemon": os.environ["BU_NAME"],
                                      "tab": uuid.uuid4().hex, "cdp_url": os.environ["BU_CDP_URL"],
                                      "isolated": _kw.get("isolated", False)}) + "\n")

        def page_socket(self):
            return None

        def set_device(self, _device):
            pass

        def arm(self, *_args):
            pass

        def check_host(self, _url):
            pass

        def navigate(self, url):
            self.url = url

        def probe(self, _expect):
            if self.url.endswith("/blocked-probe"):
                time.sleep(30)
            return {"url": self.url, "status": 200, "text": [not self.url.endswith("/jev")], "probe": {}}

        def run_hook(self, hook, _url):
            marker = args[4] / "actions.txt"
            with marker.open("a") as out:
                out.write(next(iter(hook)) + "\n")
            if hook.get("js") == "interrupt()":
                raise KeyboardInterrupt
            if "js" in hook and hook["js"] == "blocked()":
                ctypes.PyDLL(None).sleep(30)
            if "sleep" in hook:
                time.sleep(hook["sleep"])

        def reset_agent(self, _task):
            self.agent = SimpleNamespace(state={"status": "ready", "history": [], "decisions": [], "text_calls": []})

        def tick(self):
            time.sleep(30)

        def close(self):
            (args[4] / "closed.txt").write_text("closed")

    session.Session = Page
    return real(*args)


def test_monotonic_deadline_does_not_count_polls_noise_or_identity_changes(monkeypatch):
    state = watchdog.Progress(timeout=300, last=10, started=10)
    state.receive({"kind": "scenario", "name": "wait"}, now=10)
    for now in (30, 60, 189, 309):
        state.receive({"kind": "active", "identity": "wait_for condition"}, now=now)
        state.receive({"kind": "event", "value": {"at": now, "console": "tick"}}, now=now)
    assert state.last == 10
    assert not state.expired(309)
    assert state.expired(310)
    monkeypatch.setattr(time, "time", lambda: -10**12)
    monkeypatch.setattr(time, "monotonic", lambda: 310)
    assert state.expired()


def test_completion_uses_real_event_time_instead_of_ipc_delivery_time():
    state = watchdog.Progress(last=0, started=0)
    state.receive({"kind": "complete", "identity": "step done", "monotonic": 10}, now=290)
    assert state.last == 10
    assert state.expired(310)


def test_declared_180_second_wait_valid_and_queue_time_separate():
    state = watchdog.Progress(last=0, started=0)
    state.receive({"kind": "queued"}, now=10)
    assert not state.expired(1000)
    state.receive({"kind": "admitted"}, now=1010)
    assert state.last == 1000 and state.queue_seconds == 1000
    state.receive({"kind": "active", "identity": "before hook 1 (wait_for)"}, now=1010)
    assert not state.expired(1190)
    state.receive({"kind": "complete", "identity": "before hook 1 (wait_for)"}, now=1190)
    assert not state.expired(1489)
    assert state.expired(1490)
    parsed = suite.parse({"base_url": "http://127.0.0.1", "scenarios": [
        {"name": "readiness", "before": [{"wait_for": {"js": "ready", "timeout": 180}}],
         "expect": {"text": "ready"}}]})
    assert parsed.scenarios[0].before[0]["wait_for"]["timeout"] == 180
    assert suite.WAIT_MAX_S == 300


@pytest.mark.parametrize("mode", ["sleep", "native", "noise"])
def test_interrupts_blocked_io_native_and_poll_noise_with_real_process(mode, tmp_path):
    began = time.monotonic()
    _, failure = watchdog.supervise(_blocked, (mode, str(tmp_path / "pid")), timeout=2)
    assert failure["stop"] == "no_progress"
    assert failure["scenario"] == "blocked"
    assert failure["idle_seconds"] >= 2
    assert time.monotonic() - began < 8
    assert failure["cleanup_complete"] is True
    pid = int((tmp_path / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_operator_interruption_is_distinct_and_owns_no_caller_timer(tmp_path):
    handler = signal.getsignal(signal.SIGALRM)
    timer = signal.getitimer(signal.ITIMER_REAL)
    began = time.monotonic()
    _, failure = watchdog.supervise(_blocked, ("interrupt", str(tmp_path / "pid")), timeout=5)
    assert failure["stop"] == "interrupted"
    assert signal.getsignal(signal.SIGALRM) == handler
    remaining = signal.getitimer(signal.ITIMER_REAL)
    assert remaining[1] == timer[1]
    assert (abs((timer[0] - remaining[0]) - (time.monotonic() - began)) < 0.1
            if timer[0] else remaining[0] == 0)


def test_thread_caller_has_enforcement_and_caller_signal_state_is_unchanged(tmp_path):
    answer = []
    handler = signal.getsignal(signal.SIGTERM)
    thread = threading.Thread(target=lambda: answer.append(watchdog.supervise(
        _blocked, ("native", str(tmp_path / "pid")), timeout=2)))
    thread.start()
    thread.join(9)
    assert not thread.is_alive()
    assert answer[0][1]["stop"] == "no_progress"
    assert signal.getsignal(signal.SIGTERM) == handler


def test_process_group_cleanup_kills_descendants(tmp_path):
    marker = str(tmp_path / "pid")
    _, failure = watchdog.supervise(_blocked, ("descendant", marker), timeout=2)
    assert failure["stop"] == "no_progress"
    child = int(Path(marker + ".child").read_text())
    # A reparented process can briefly remain a zombie; it must no longer execute.
    proc = Path(f"/proc/{child}/stat")
    try:
        try:
            state = proc.read_text().split()[2]
        except (FileNotFoundError, ProcessLookupError):
            state = None
        assert state in {None, "Z"}
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child, signal.SIGKILL)


@pytest.mark.parametrize("url,hook,goal,identity", [
    ("/blocked", [{"js": "blocked()"}], None, "before hook 1 (js)"),
    ("/blocked-probe", [], None, "page checks"),
    ("/jev", [], "find the button", "Jev decision/action"),
])
def test_actual_runner_entry_retains_prior_results_and_never_replays(actual_runner, url, hook, goal, identity):
    parsed = suite.parse({"base_url": "http://127.0.0.1", "scenarios": [
        {"name": "earlier", "url": "/earlier", "expect": {"text": "ready"}, "settle": 0},
        {"name": "blocked", "url": url, "before": hook, "goal": goal,
         "expect": {"text": "ready"}, "settle": 0},
        {"name": "dependent", "url": "/dependent", "depends_on": ["blocked"], "expect": {"text": "ready"}},
        {"name": "lost-page", "depends_on": ["earlier"], "expect": {"text": "ready"}},
        {"name": "later", "url": "/later", "before": [{"sleep": .01}], "expect": {"text": "ready"}},
        {"name": "after-dependent", "url": "/dependent", "depends_on": ["blocked"], "expect": {"text": "ready"}},
    ]})
    report, _opts = actual_runner(parsed)
    results = report["scenarios"]
    expected = ["pass", "harness", "skipped", "skipped", "pass", "skipped"]
    assert [r["outcome"] for r in results] == expected
    assert results[1]["stop"] == "no_progress" and results[1]["session_uncertain"]
    assert identity in results[1]["watchdog"]["identity"]
    assert "no automatic retry" in results[1]["reason"]
    assert results[2]["stop"] == "dependency"
    assert results[3]["stop"] == "session_uncertain"
    saved = json.loads((Path(report["run_dir"]) / "report.json").read_text())
    assert [r["outcome"] for r in saved["scenarios"]] == expected
    assert report["gate"] != "PASS"
    action_file = Path(report["run_dir"]) / "actions.txt"
    assert action_file.read_text().splitlines() == (["js", "sleep"] if hook else ["sleep"])
    generations = [json.loads(row) for row in (Path(report["run_dir"]) / "generations.jsonl").read_text().splitlines()]
    assert len(generations) == 2
    assert all(len({row[key] for row in generations}) == 2 for key in ("pid", "daemon", "tab"))
    assert len({row["cdp_url"] for row in generations}) == 1
    assert not any(row["isolated"] for row in generations)
    if hook:
        assert results[1]["checks"] and results[1]["checks"][0]["ok"] is True


@pytest.fixture
def actual_runner(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "_group_child", _fixture_group_child)
    monkeypatch.setattr(watchdog, "supervise", functools.partial(watchdog.supervise, timeout=2))
    monkeypatch.setattr(runner.providers, "resolve", lambda: {})
    monkeypatch.setattr(runner.providers, "describe", lambda _r: {"jev": "fixture", "text": "fixture"})
    monkeypatch.setattr(runner.chrome, "reap", lambda: [])
    monkeypatch.setattr(runner.chrome, "version", lambda _u: {"fixture": True})
    def run(parsed, **kwargs):
        opts = runner.Options(**{"cdp_url": "http://127.0.0.1:9", "out_dir": tmp_path,
                                 "shots": False, "ux": "off", "load_high": None, **kwargs})
        return runner.run(parsed, opts), opts

    return run


def test_unsupported_platform_refuses_instead_of_disabling_enforcement(monkeypatch):
    monkeypatch.setattr(watchdog.os, "name", "nt")
    with pytest.raises(RuntimeError, match="run refused"):
        watchdog.supervise(None)


@pytest.mark.parametrize("checks", [[], [{"check": "ready", "ok": True}], [{"check": "ready", "ok": False}]])
def test_no_progress_is_always_harness(checks):
    assert verdict.classify("no_progress", checks, has_checks=bool(checks))[0] == "harness"


def test_parallel_worker_uses_same_production_boundary(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(runner, "supervised_group", lambda *a, **kw: seen.append((a, kw)) or {"results": []})
    runner._worker(([], {}, "http://127.0.0.1:9", {}, tmp_path, 1.0, .0005))
    assert len(seen) == 1


@pytest.mark.parametrize("history,progress", [
    ({"kind": kind, "executed_ms": 12, "page_changed": True}, True)
    for kind in ("click", "fill", "select", "scroll")
] + [
    ({"executed_ms": 12, "page_changed": True}, False),
    ({"kind": "unknown", "executed_ms": 12, "page_changed": True}, False),
    ({"kind": "wait", "executed_ms": 12, "page_changed": True}, False),
    ({"kind": "click", "page_changed": True}, False),
    ({"kind": "click", "executed_ms": True, "page_changed": True}, False),
    ({"kind": "click", "executed_ms": float("nan"), "page_changed": True}, False),
    ({"kind": "click", "executed_ms": 12, "page_changed": False}, False),
    ({"kind": "click", "executed_ms": 12, "page_changed": None}, False),
])
def test_only_confirmed_meaningful_jev_history_is_progress(history, progress):
    assert runner._progress_action(history) is progress


@pytest.mark.parametrize("mode", ["decisions", "noop", "failed", "executed"])
def test_jev_loop_progress_ignores_decisions_noops_and_failed_actions(monkeypatch, mode):
    progress = []
    state = {"status": "ready", "history": [], "decisions": []}

    def tick():
        state["decisions"].append({"operation": "CLICK"})
        if mode == "failed":
            raise RuntimeError("action rejected")
        if mode != "decisions":
            state["history"].append({"kind": "click", "executed_ms": 10, "step": 1,
                                     "page_changed": mode == "executed"})
        state["status"] = "done"

    monkeypatch.setattr(watchdog, "complete", lambda identity, **_data: progress.append(identity))
    scenario = SimpleNamespace(budget={"actions": 10, "seconds": 30})
    browser = SimpleNamespace(agent=SimpleNamespace(state=state), tick=tick)
    runner.drive(browser, scenario, lambda: None, lambda: False, time.monotonic())
    assert progress == (["Jev action 1"] if mode == "executed" else [])


def test_cdp_resource_ownership_streams_exact_target_context_ids(monkeypatch):
    messages = []
    monkeypatch.setattr(watchdog, "notify", lambda kind, **data: messages.append({"kind": kind, **data}))
    cdp = session._patient_cdp(lambda method, **_kw: {"targetId": "mine"} if method.endswith("Target") else
                               {"browserContextId": "my-context"}, 30, SimpleNamespace(error=None))
    cdp("Target.createTarget", url="about:blank")
    cdp("Target.createBrowserContext")
    assert [m for m in messages if m["kind"] == "resource"] == [
        {"kind": "resource", "targets": ["mine"], "contexts": []},
        {"kind": "resource", "targets": [], "contexts": ["my-context"]}]


def test_unacknowledged_resource_creation_reports_cleanup_uncertainty(tmp_path):
    _, failure = watchdog.supervise(_blocked, ("pending", str(tmp_path / "pid")), timeout=2)
    assert failure["resource_ownership_uncertain"] is True
    assert failure["cleanup_complete"] is False


def _blocked_cleanup(pipe, *_args):
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    ctypes.PyDLL(None).sleep(30)


def test_browser_cleanup_is_bounded_even_when_native_cleanup_blocks(monkeypatch):
    monkeypatch.setattr(watchdog, "_cleanup_child", _blocked_cleanup)
    monkeypatch.setattr(watchdog, "CLEANUP_LIMIT", 1)
    began = time.monotonic()
    done = watchdog.cleanup(multiprocessing.get_context("spawn"), "unused", set(), set(), "owned-daemon")
    assert done is False
    assert time.monotonic() - began < 5


def test_independent_cleanup_closes_only_recorded_targets_contexts(monkeypatch):
    requests, closed = [], []
    contexts = {"mine", "other-chain"}

    class Socket:
        def send(self, request):
            requests.append(json.loads(request))
            if "params" in requests[-1]:
                contexts.discard(requests[-1]["params"]["browserContextId"])

        def recv(self, timeout):
            request = requests[-1]
            return json.dumps({"id": request["id"], "result": {"browserContextIds": list(contexts)}})

    monkeypatch.setattr(runner.chrome, "targets", lambda _u: [
        {"id": t} for t in ("my-tab", "other-tab") if t not in closed])
    monkeypatch.setattr(runner.chrome, "close_target", lambda _u, target: closed.append(target) or True)
    monkeypatch.setattr(runner.chrome, "version", lambda _u: {"webSocketDebuggerUrl": "fixture"})
    monkeypatch.setitem(sys.modules, "websockets.sync.client", SimpleNamespace(
        connect=lambda *_a, **_kw: contextlib.nullcontext(Socket())))
    assert watchdog._cleanup("fixture", {"my-tab", "already-closed"}, {"mine", "already-disposed"}, None)
    assert closed == ["my-tab"]
    disposed = [r["params"]["browserContextId"] for r in requests if "params" in r]
    assert disposed == ["mine"]


def _echo_options(*args):
    def group(_scenarios, *, opts, **_kwargs):
        config.remember_secret("child-private-seed-value")
        watchdog.notify("event", value={"event": "fixture", "child": "child-private-seed-value"})
        return {"results": [{"name": _scenarios[0].name, "outcome": "pass", "account": opts.sign_in_here,
                             "parent": "parent-private-seed-value"}], "ledger": _kwargs["ledger"].summary()}

    runner.run_group = group
    return runner._group_child(*args)


def test_spawn_preserves_required_runtime_options_and_redacts_both_sides(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "_group_child", _echo_options)
    config.remember_secret("parent-private-seed-value")
    events = []
    opts = runner.Options(emit=events.append)
    opts.sign_in_here = {"name": "seeded-account", "password": "seed:fixture.json#password"}
    scenarios = [SimpleNamespace(name="fixture")]
    out = runner.supervised_group(scenarios, opts=opts, cdp_url="unused", suite_meta={}, run_dir=tmp_path,
                                  ledger=runner.Ledger(1))
    assert out["results"][0]["account"] == opts.sign_in_here
    assert out["results"][0]["parent"] == "[redacted]"
    assert events == [{"event": "fixture", "child": "[redacted]"}]


def _concurrent_models(witness, pause_receiver=False):
    witness_lock = threading.Lock()

    def entered(stage):
        with witness_lock, Path(witness).open("a") as out:
            out.write(json.dumps({"stage": stage, "at": time.monotonic()}) + "\n")

    entered("worker-entered")
    watchdog.notify("scenario", name="blocked")
    if pause_receiver:
        watchdog.notify("event", value={"event": "pause-receiver"})
    ledger = runner.Ledger(1, .1)
    started = threading.Event()

    def model(_url, _key, body):
        entered("blocked-call-entered" if body["block"] else "fast-call-entered")
        if body["block"]:
            started.set()
            time.sleep(30)
        return {"usage": {"cost": .1}}

    call = ledger.wrap(model)
    thread = threading.Thread(target=lambda: call("https://typesafe.ai", "fixture", {"block": True}))
    thread.start()
    assert started.wait(1)
    call("https://typesafe.ai", "fixture", {"block": False})
    entered("fast-call-completed")
    watchdog.complete("concurrency fixture ready")
    entered("fixture-ready")
    watchdog.active("one model call still in flight")
    time.sleep(30)


def test_concurrent_model_completion_does_not_hide_another_outstanding_call(tmp_path):
    witness = tmp_path / "concurrent-model-witness.jsonl"
    _, failure = watchdog.supervise(_concurrent_models, (str(witness),), timeout=2)
    rows = [json.loads(row) for row in witness.read_text().splitlines()] if witness.exists() else []
    detail = {"failure": failure, "witness": rows}
    assert failure["stop"] == "no_progress", detail
    assert failure["scenario"] == "blocked", detail
    assert failure["identity"] == "one model call still in flight", detail
    assert [row["stage"] for row in rows] == [
        "worker-entered", "blocked-call-entered", "fast-call-entered", "fast-call-completed", "fixture-ready"], detail
    assert failure["cost_uncertain"] is True, detail
    assert failure["ledger"]["usd"] == .1
    assert failure["ledger"]["calls"]["typesafe"] == 1


def _recovery_suite():
    return suite.parse({"base_url": "http://127.0.0.1", "scenarios": [
        {"name": "blocked", "url": "/blocked", "before": [{"js": "blocked()"}], "expect": {"text": "ready"}},
        {"name": "later", "url": "/later", "expect": {"text": "ready"}},
    ]})


def _failure(stop="no_progress", cleaned=True, cost=None, uncertain=False):
    return {"stop": stop, "scenario": "blocked", "detail": "stopped", "identity": "before hook 1 (js)",
            "active_seconds": 2, "cleanup_complete": cleaned, "cost_uncertain": uncertain,
            "results": [], "partial": {}, "ledger": cost, "runtime": {}}


@pytest.mark.parametrize("stop,cleaned", [("no_progress", False), ("interrupted", True),
                                         ("worker_error", True), ("cost_cap", True)])
def test_recovery_is_refused_after_unverified_cleanup_operator_or_other_stops(monkeypatch, tmp_path, stop, cleaned):
    attempts = []

    def supervise(*_args, **_kwargs):
        attempts.append(True)
        if len(attempts) > 1:
            return {"results": [{"name": "later", "outcome": "pass"}], "ledger": runner.Ledger(1).summary()}, None
        return None, _failure(stop, cleaned)

    monkeypatch.setattr(watchdog, "supervise", supervise)
    out = runner.supervised_group(_recovery_suite().scenarios, opts=runner.Options(), cdp_url="unused",
                                  suite_meta={}, run_dir=tmp_path, ledger=runner.Ledger(1))
    assert len(attempts) == 1
    assert [r["outcome"] for r in out["results"]] == ["harness", "skipped"]
    assert out["results"][1]["stop"] == ("cleanup_incomplete" if not cleaned else "session_uncertain")


@pytest.mark.parametrize("uncertain", [False, True])
def test_recovery_keeps_cumulative_cap_cost_and_refuses_unknown_paid_cost(monkeypatch, tmp_path, uncertain):
    attempts = []
    ledger = runner.Ledger(1, .1, shared=multiprocessing.get_context("spawn").Value("d", .25))
    ledger.usd["typesafe"] = .25
    ledger.calls["typesafe"] = 1

    def supervise(_target, *, args, **_kwargs):
        attempts.append(args)
        if len(attempts) == 1:
            return None, _failure(cost=ledger.summary(), uncertain=uncertain)
        state = args[5]
        assert state["cap_usd"] == 1 and state["usd"]["typesafe"] == .25
        assert state["calls"]["typesafe"] == 1
        resumed = runner.Ledger(state["cap_usd"])
        resumed.__dict__.update(state)
        if uncertain:
            assert state["uncertain"] is True and args[6].value == 1
            with pytest.raises(runner.CostCapReached, match="unobserved cost"):
                resumed.before("https://typesafe.ai", {})
        else:
            resumed.before("https://typesafe.ai", {})
            resumed.usd["typesafe"] += .1
        return {"results": [{"name": "later", "outcome": "pass"}], "ledger": resumed.summary()}, None

    monkeypatch.setattr(watchdog, "supervise", supervise)
    out = runner.supervised_group(_recovery_suite().scenarios, opts=runner.Options(), cdp_url="unused",
                                  suite_meta={}, run_dir=tmp_path, ledger=ledger)
    assert len(attempts) == 2
    assert out["ledger"]["usd"] == (.25 if uncertain else .35)
    assert out["results"][0]["cost_incomplete"] is uncertain


def test_same_tab_signin_runtime_reaches_parent_report_without_replaying_login(actual_runner):
    parsed = _recovery_suite()
    parsed.account = {"name": "Fixture account", "login": {"tab": "same"}}
    report, opts = actual_runner(parsed)
    assert "sign_in" in report and report["sign_in"] == {
        "account": "Fixture account", "ok": True, "tab": "same", "watchdog_continuation": {
            "browser_context": "retained", "tab_state": "discarded", "login_replayed": False}}
    assert opts.account == "Fixture account" and opts.signed_here["ok"] is True
    assert (Path(report["run_dir"]) / "signins.txt").read_text().splitlines() == ["Fixture account"]
    assert [r["outcome"] for r in report["scenarios"]] == ["harness", "pass"]


def test_whole_run_load_wait_budget_is_not_restarted_after_watchdog(actual_runner):
    events = []
    report, opts = actual_runner(_recovery_suite(), profile="fixture-load", load_high=1, load_ok=.5,
                                 load_wait=.01, emit=events.append)
    assert len([e for e in events if e["event"] == "waiting"]) == 1
    assert opts.load_waited >= .01
    assert [r["outcome"] for r in report["scenarios"]] == ["harness", "skipped"]
    assert "machine busy" in report["scenarios"][1]["reason"]


def test_cleanup_requires_observed_target_removal_not_just_close_acknowledgement(monkeypatch):
    monkeypatch.setattr(runner.chrome, "targets", lambda _url: [{"id": "still-owned"}])
    monkeypatch.setattr(runner.chrome, "close_target", lambda *_args: True)
    assert watchdog._cleanup("fixture", {"still-owned"}, set(), None) is False


def _creation_retry_then_block():
    watchdog.notify("scenario", name="blocked")
    notify = watchdog.notify

    def fixture_notify(kind, **data):
        if kind == "resource":
            data.pop("targets", None)  # The stand-in response owns no real browser resource.
        notify(kind, **data)

    watchdog.notify = fixture_notify
    attempts = []

    def raw(_method, **_params):
        attempts.append(True)
        if len(attempts) == 1:
            raise TimeoutError("first creation acknowledgement missing")
        return {"targetId": "acknowledged-second-target"}

    call = session._patient_cdp(raw, 30, SimpleNamespace(error=None))
    call("Target.createTarget")
    watchdog.active("blocked check after resource creation")
    time.sleep(30)


def test_preserved_upstream_creation_retry_cannot_confirm_ownership_of_its_first_attempt():
    _, failure = watchdog.supervise(_creation_retry_then_block, timeout=2)
    assert failure["resource_ownership_uncertain"] is True
    assert failure["cleanup_complete"] is False


def test_serial_spawned_run_closes_the_parent_owned_signin_daemon(actual_runner, monkeypatch):
    parsed = suite.parse({"base_url": "http://127.0.0.1", "scenarios": [
        {"name": "ready", "url": "/ready", "expect": {"text": "ready"}}]})
    parsed.account = {"name": "Fixture account", "login": {"tab": "new"}}
    closed = []
    monkeypatch.setattr(runner, "sign_in_first", lambda *_a, **_kw: {"account": "Fixture account", "ok": True})
    monkeypatch.setattr(session, "stop_daemon", lambda: closed.append(os.getpid()))
    report, _opts = actual_runner(parsed)
    assert closed == [os.getpid()]
    assert report["sign_in"]["ok"] is True and report["scenarios"][0]["outcome"] == "pass"


def test_child_operator_interruption_preserves_prefix_and_marks_suffix_without_recovery(actual_runner):
    parsed = suite.parse({"base_url": "http://127.0.0.1", "scenarios": [
        {"name": "earlier", "url": "/earlier", "expect": {"text": "ready"}},
        {"name": "interrupted", "url": "/interrupted", "before": [{"js": "interrupt()"}],
         "expect": {"text": "ready"}},
        {"name": "later", "url": "/later", "before": [{"js": "mustNotRun()"}], "expect": {"text": "ready"}},
        {"name": "dependent", "url": "/dependent", "depends_on": ["interrupted"], "expect": {"text": "ready"}},
    ]})
    try:
        report, _opts = actual_runner(parsed)
    except KeyError as error:
        raise AssertionError(
            "child interruption must classify the unrun suffix instead of losing its results") from error
    assert report["interrupted"] is True
    assert [r["outcome"] for r in report["scenarios"]] == ["pass", "harness", "skipped", "skipped"]
    assert report["scenarios"][1]["stop"] == "interrupted" and report["scenarios"][1]["interrupted"] is True
    assert all(r["stop"] == "interrupted" and "not run: interrupted" in r["reason"]
               for r in report["scenarios"][2:])
    directory = Path(report["run_dir"])
    assert len((directory / "generations.jsonl").read_text().splitlines()) == 1
    assert (directory / "actions.txt").read_text().splitlines() == ["js"]


def _queued_state(kind, witness):
    watchdog.notify("scenario", name="blocked")
    watchdog.notify("event", value={"event": "pause-receiver"})
    if kind == "resource":
        watchdog.notify("resource_pending", method="Target.createTarget", request_id="unacknowledged")
    else:
        watchdog.notify("runtime", value={"load_waited": 17})
    Path(witness).write_text("ready")
    time.sleep(30)


@pytest.mark.parametrize("kind", ["cost", "resource", "runtime"])
def test_unread_critical_notifications_remain_uncertain_at_stop(kind, tmp_path):
    witness = tmp_path / "queued-witness.jsonl"

    def pause(event):
        if event["event"] != "pause-receiver":
            return
        deadline = time.monotonic() + 3
        while True:
            ready = witness.exists()
            if ready and kind == "cost":
                ready = "fast-call-completed" in witness.read_text()
            if ready:
                break
            assert time.monotonic() < deadline, "fixture setup must finish before testing queued notifications"
            time.sleep(.01)
        time.sleep(2.1)  # Messages are queued while the supervisor misses its next receive opportunity.

    target = _concurrent_models if kind == "cost" else _queued_state
    args = (str(witness), True) if kind == "cost" else (kind, str(witness))
    _, failure = watchdog.supervise(target, args, timeout=2, emit=pause)
    assert witness.exists() and failure["stop"] == "no_progress", failure
    if kind == "cost":
        assert "blocked-call-entered" in witness.read_text() and "fast-call-completed" in witness.read_text()
        assert failure["cost_uncertain"] is True, failure
    elif kind == "resource":
        assert failure["resource_ownership_uncertain"] is True, failure
        assert failure["cleanup_complete"] is False, failure
    else:
        assert failure.get("runtime_state_incomplete") is True, failure


def test_unread_ready_still_stops_only_the_owned_process_group(monkeypatch, tmp_path):
    marker = str(tmp_path / "owned")
    context = multiprocessing.get_context("spawn")

    class Unread:
        def __init__(self, connection):
            self.connection = connection

        def poll(self, _timeout):
            time.sleep(.01)
            return False

        def close(self):
            self.connection.close()

    class Context:
        Process = context.Process
        RawArray = context.RawArray

        def Pipe(self, **kwargs):  # noqa: N802
            receive, send = context.Pipe(**kwargs)
            return Unread(receive), send

    monkeypatch.setattr(watchdog, "multiprocessing", SimpleNamespace(get_context=lambda _kind: Context()))
    monkeypatch.setattr(watchdog.Progress, "expired", lambda *_a, **_kw: Path(marker + ".child").exists())
    sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    child = None
    try:
        _, failure = watchdog.supervise(_blocked, ("descendant", marker), timeout=2)
        child = int(Path(marker + ".child").read_text())
        try:
            state = Path(f"/proc/{child}/stat").read_text().split()[2]
        except (FileNotFoundError, ProcessLookupError):
            state = None
        assert failure["stop"] == "no_progress", failure
        assert state in {None, "Z"}, "owned descendant survived unread ready"
        assert sibling.poll() is None, "an unrelated process group must remain alive"
    finally:
        if child is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            sibling.kill()
        sibling.wait(timeout=5)


def test_unknown_runtime_bookkeeping_refuses_a_replacement_worker(monkeypatch, tmp_path):
    attempts = []

    def supervise(*_args, **_kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            failure = _failure()
            failure["runtime_state_incomplete"] = True
            return None, failure
        return {"results": [{"name": "later", "outcome": "pass"}], "ledger": runner.Ledger(1).summary()}, None

    monkeypatch.setattr(watchdog, "supervise", supervise)
    out = runner.supervised_group(_recovery_suite().scenarios, opts=runner.Options(), cdp_url="unused",
                                  suite_meta={}, run_dir=tmp_path, ledger=runner.Ledger(1))
    assert len(attempts) == 1
    assert out["results"][1]["stop"] == "runtime_uncertain"
