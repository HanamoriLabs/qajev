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
        watchdog.notify("resource_pending", method="Target.createTarget")
        time.sleep(30)
    else:
        time.sleep(30)


def _fixture_group_child(*args):
    """Only replace browser/model dependencies inside the spawned worker, never the runner path."""
    real = runner._group_child
    session.configure_env = lambda _url: ""
    session.stop_daemon = lambda: None
    runner.live.frames = lambda *_a, **_kw: contextlib.nullcontext()

    class Page:
        def __init__(self, ledger, **_kw):
            self.ledger = ledger
            self.url = "http://127.0.0.1/"

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
            if "js" in hook:
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
def test_actual_runner_entry_retains_prior_results_and_never_replays(monkeypatch, tmp_path, url, hook, goal, identity):
    monkeypatch.setattr(runner, "_group_child", _fixture_group_child)
    monkeypatch.setattr(watchdog, "supervise", functools.partial(watchdog.supervise, timeout=2))
    monkeypatch.setattr(runner.providers, "resolve", lambda: {})
    monkeypatch.setattr(runner.providers, "describe", lambda _r: {"jev": "fixture", "text": "fixture"})
    monkeypatch.setattr(runner.chrome, "reap", lambda: [])
    monkeypatch.setattr(runner.chrome, "version", lambda _u: {"fixture": True})
    parsed = suite.parse({"base_url": "http://127.0.0.1", "scenarios": [
        {"name": "earlier", "url": "/earlier", "expect": {"text": "ready"}, "settle": 0},
        {"name": "blocked", "url": url, "before": hook, "goal": goal,
         "expect": {"text": "ready"}, "settle": 0},
        {"name": "later", "url": "/later", "before": [{"js": "mustNotRun()"}], "expect": {"text": "ready"}},
    ]})
    opts = runner.Options(cdp_url="http://127.0.0.1:9", out_dir=tmp_path, shots=False, ux="off", load_high=None)
    report = runner.run(parsed, opts)
    results = report["scenarios"]
    assert [r["outcome"] for r in results] == ["pass", "harness", "skipped"]
    assert results[1]["stop"] == "no_progress" and results[1]["session_uncertain"]
    assert identity in results[1]["watchdog"]["identity"]
    assert "no automatic retry" in results[1]["reason"]
    assert results[2]["stop"] == "session_uncertain"
    saved = json.loads((Path(report["run_dir"]) / "report.json").read_text())
    assert [r["outcome"] for r in saved["scenarios"]] == ["pass", "harness", "skipped"]
    assert report["gate"] != "PASS"
    action_file = Path(report["run_dir"]) / "actions.txt"
    assert (action_file.read_text().splitlines() if action_file.exists() else []) == (["js"] if hook else [])
    if hook:
        assert results[1]["checks"] and results[1]["checks"][0]["ok"] is True


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

    class Socket:
        def send(self, request):
            requests.append(json.loads(request))

        def recv(self, timeout):
            request = requests[-1]
            return json.dumps({"id": request["id"], "result": {"browserContextIds": ["mine", "other-chain"]}})

    monkeypatch.setattr(runner.chrome, "targets", lambda _u: [{"id": "my-tab"}, {"id": "other-tab"}])
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
        return {"account": opts.sign_in_here, "parent": "parent-private-seed-value"}

    runner.run_group = group
    return runner._group_child(*args)


def test_spawn_preserves_required_runtime_options_and_redacts_both_sides(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "_group_child", _echo_options)
    config.remember_secret("parent-private-seed-value")
    events = []
    opts = runner.Options(emit=events.append)
    opts.sign_in_here = {"name": "seeded-account", "password": "seed:fixture.json#password"}
    out = runner.supervised_group([], opts=opts, cdp_url="unused", suite_meta={}, run_dir=tmp_path,
                                  ledger=runner.Ledger(1))
    assert out["account"] == opts.sign_in_here
    assert out["parent"] == "[redacted]"
    assert events == [{"event": "fixture", "child": "[redacted]"}]
