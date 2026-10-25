"""A lost browser/daemon transport never silently replays a scenario or reuses its session."""

import contextlib
import sys
from types import SimpleNamespace

import pytest

from qajev import runner, session, suite


@pytest.fixture
def group(monkeypatch, tmp_path):
    state = SimpleNamespace(error=None)
    seen = {"raw": [], "hooks": [], "starts": [], "closed": 0, "daemons": 0, "made": 0, "current": ""}
    error = RuntimeError("no close frame received or sent")
    fail = {"at": "hook", "error": error, "swallow": False}

    def raw(method, **params):
        seen["raw"].append((seen["current"], method))
        if seen["current"] == "lost" and method == fail["at"]:
            raise fail["error"]
        return {}

    factory = getattr(session, "_patient_cdp", lambda raw_cdp, timeout, transport: raw_cdp)
    cdp = factory(raw, 30, state)
    monkeypatch.setattr(session, "_jev", SimpleNamespace(transport=state))
    monkeypatch.setattr(session, "configure_env", lambda _url: "fixture")
    monkeypatch.setattr(session, "stop_daemon", lambda: seen.__setitem__("daemons", seen["daemons"] + 1))
    monkeypatch.setattr(runner.live, "frames", lambda *_a, **_kw: contextlib.nullcontext())
    monkeypatch.setattr(runner, "wait_for_quiet", lambda _opts: (True, 0))

    class Page:
        def __init__(self, *_a, **_kw):
            seen["made"] += 1
            self.url = "http://127.0.0.1/"

        def page_socket(self):
            cdp("socket")
            return None

        def close(self):
            seen["closed"] += 1

        def set_device(self, *_a):
            cdp("device")

        def arm(self, *_a):
            pass

        def check_host(self, *_a):
            pass

        def navigate(self, url):
            self.url = url
            cdp("navigate")

        def probe(self, *_a):
            try:
                cdp("probe")
            except RuntimeError:
                if not fail["swallow"]:
                    raise
            return {"url": self.url, "status": 200, "text": [True], "probe": {}}

        def run_hook(self, hook, _url):
            seen["hooks"].append((seen["current"], hook["js"]))
            cdp("hook" if hook["js"] == "lose()" else "after")

        def screenshot(self, *_a):
            cdp("screenshot")
            return None

    monkeypatch.setattr(session, "Session", Page)
    scenarios = suite.parse({"base_url": "http://127.0.0.1", "scenarios": [
        {"name": "earlier", "url": "/earlier", "expect": {"text": "ready"}, "settle": 0},
        {"name": "lost", "url": "/lost", "before": [{"js": "lose()"}],
         "after": [{"js": "mustNotReplay()"}], "expect": {"text": "ready"}, "settle": 0},
        {"name": "later", "url": "/later", "before": [{"js": "mustNotRun()"}],
         "expect": {"text": "ready"}},
        {"name": "dependent", "url": "/dependent", "depends_on": ["lost"], "expect": {"text": "ready"}},
    ]}).scenarios

    def emit(event):
        if event["event"] == "start":
            seen["current"] = event["scenario"]
            seen["starts"].append(event["scenario"])

    opts = runner.Options(shots=False, ux="off", load_high=None, emit=emit)
    meta = {"headless": True, "hosts": ["127.0.0.1"], "guard": {}, "cwd": str(tmp_path)}

    def run():
        return runner.run_group(scenarios, opts=opts, cdp_url="http://127.0.0.1:9350", suite_meta=meta,
                                run_dir=tmp_path, ledger=runner.Ledger(0.0))

    return run, fail, seen, state, scenarios, opts, cdp


@pytest.mark.parametrize("at", ["device", "navigate", "probe", "hook", "after", "socket", "screenshot"])
def test_mid_suite_transport_loss_keeps_one_harness_and_never_reuses_or_replays(group, at):
    run, fail, seen, _state, _scenarios, opts, _cdp = group
    fail["at"] = at
    opts.shots = at == "screenshot"
    out = run()
    results = out["results"]
    assert [r["outcome"] for r in results] == ["pass", "harness", "skipped", "skipped"]
    assert results[1]["stop"] == "browser_error" and results[1].get("transport_lost") is True
    assert "no close frame" in results[1]["reason"]
    assert [(r["stop"], r["reason"], r["cost_usd"], r["checks"]) for r in results[2:]] == [
        ("transport_lost", "not run: transport lost after 'lost'; no automatic retry", 0.0, []),
        ("transport_lost", "not run: transport lost after 'lost'; no automatic retry", 0.0, []),
    ]
    assert seen["starts"] == ["earlier", "lost"]
    assert all(name not in {"later", "dependent"} for name, _method in seen["raw"])
    assert len([1 for name, method in seen["raw"] if name == "lost" and method == at]) == 1
    assert seen["made"] == seen["closed"] == seen["daemons"] == 1
    assert ("lost", "mustNotReplay()") not in seen["hooks"] or at in {"after", "screenshot"}


def test_swallowed_optional_probe_error_cannot_turn_transport_loss_into_pass(group):
    run, fail, seen, _state, _scenarios, _opts, _cdp = group
    fail.update(at="probe", swallow=True)
    results = run()["results"]
    assert [r["outcome"] for r in results] == ["pass", "harness", "skipped", "skipped"]
    assert results[1].get("transport_lost") is True
    assert seen["starts"] == ["earlier", "lost"]
    assert seen["hooks"] == []


def test_checks_pass_then_transport_loss_before_scenario_end_cannot_keep_pass(group, monkeypatch):
    run, _fail, seen, state, _scenarios, _opts, _cdp = group
    original = runner.run_scenario
    passed_checks = [{"kind": "text", "ok": True, "actual": "ready"}]

    def pass_then_lose(page, scenario, **kwargs):
        if scenario.name != "lost":
            return original(page, scenario, **kwargs)
        state.error = session.TransportLost("connection closed after the checks passed")
        return {"name": scenario.name, "url": scenario.url, "goal": scenario.goal, "mode": scenario.mode,
                "outcome": "pass", "reason": "all checks passed", "stop": "checked", "checks": passed_checks,
                "findings": [], "screens": []}

    monkeypatch.setattr(runner, "run_scenario", pass_then_lose)
    results = run()["results"]
    assert [r["outcome"] for r in results] == ["pass", "harness", "skipped", "skipped"]
    assert results[1]["checks"] == passed_checks
    assert results[1]["transport_lost"] is True
    assert results[1]["reason"] == (
        "browser/daemon transport lost: connection closed after the checks passed; no automatic retry")
    assert seen["starts"] == ["earlier", "lost"] and seen["hooks"] == []
    assert seen["made"] == seen["closed"] == seen["daemons"] == 1


@pytest.mark.parametrize("error", [RuntimeError("ordinary CDP command rejected"), TimeoutError("slow page")])
def test_ordinary_command_failure_or_timeout_does_not_poison_later_scenarios(group, error):
    run, fail, seen, _state, _scenarios, _opts, _cdp = group
    fail["error"] = error
    results = run()["results"]
    assert [r["outcome"] for r in results] == ["pass", "harness", "pass", "skipped"]
    assert results[1].get("transport_lost") is not True
    assert seen["starts"] == ["earlier", "lost", "later"]


def test_later_multiplayer_scenario_does_not_start_after_lost_transport(group, monkeypatch):
    run, _fail, seen, _state, scenarios, _opts, _cdp = group
    scenarios[2].clients = [{"name": "player"}]
    calls = []
    monkeypatch.setattr(runner, "run_clients", lambda *_a, **_kw: calls.append(True) or {
        "name": "later", "outcome": "pass", "checks": [], "findings": [], "screens": []})
    results = run()["results"]
    assert calls == []
    assert results[2]["reason"].startswith("not run: transport lost")
    assert seen["made"] == 1


def test_poisoned_transport_never_sends_a_second_raw_command(group):
    _run, fail, seen, _state, _scenarios, _opts, cdp = group
    seen["current"] = "lost"
    fail["at"] = "Input.dispatchMouseEvent"
    for _ in range(2):
        with pytest.raises(RuntimeError, match="no close frame"):
            cdp("Input.dispatchMouseEvent", type="mousePressed")
    assert seen["raw"] == [("lost", "Input.dispatchMouseEvent")]


@pytest.mark.parametrize("error", [ConnectionResetError("reset"), ConnectionRefusedError("refused"),
                                  BrokenPipeError("pipe"), EOFError("daemon closed the connection"),
                                  RuntimeError("sent 1001 (going away); no close frame received"),
                                  RuntimeError("daemon is shutting down"),
                                  FileNotFoundError("daemon endpoint is gone")])
def test_each_transport_failure_opens_the_fuse_before_a_second_command(group, error):
    _run, fail, seen, state, _scenarios, _opts, cdp = group
    seen["current"] = "lost"
    fail.update(at="probe", error=error)
    with pytest.raises((RuntimeError, OSError, EOFError)):
        cdp("probe")
    assert state.error is not None
    with pytest.raises(RuntimeError):
        cdp("unrelated")
    assert seen["raw"] == [("lost", "probe")]


def test_daemon_line_limit_diagnostic_does_not_poison_a_healthy_transport(group):
    _run, fail, seen, state, _scenarios, _opts, cdp = group
    seen["current"] = "lost"
    fail.update(at="Runtime.evaluate", error=BrokenPipeError("too large"))
    with pytest.raises(RuntimeError, match="over the browser daemon"):
        cdp("Runtime.evaluate", expression="x" * 100_000)
    assert state.error is None
    cdp("healthy")
    assert seen["raw"][-1] == ("lost", "healthy")


def test_an_explicit_websocket_loss_with_a_large_request_still_opens_the_fuse(group):
    _run, fail, seen, state, _scenarios, _opts, cdp = group
    seen["current"] = "lost"
    fail.update(at="Runtime.evaluate", error=RuntimeError("no close frame received or sent"))
    with pytest.raises(RuntimeError, match="no close frame"):
        cdp("Runtime.evaluate", expression="x" * 100_000)
    assert state.error is not None


def test_a_second_group_on_the_same_lost_transport_never_constructs_a_session(group):
    run, _fail, seen, _state, _scenarios, _opts, _cdp = group
    run()
    starts = list(seen["starts"])
    results = run()["results"]
    assert [r["outcome"] for r in results] == ["skipped"] * 4
    assert all(r["reason"].startswith("not run: transport lost") for r in results)
    assert seen["starts"] == starts and seen["made"] == 1


@pytest.mark.parametrize("lost_reply", [False, True])
def test_load_wires_the_same_fuse_into_jev_and_the_runner(monkeypatch, lost_reply):
    calls = []
    helpers = SimpleNamespace(_send=lambda *_a, **_kw: {})

    def raw(method, **_kwargs):
        calls.append(True)
        if lost_reply:
            return helpers._send({"method": method}).get("result", {})
        raise RuntimeError("no close frame received or sent")

    class Browser:
        def __init__(self, _url):
            pass

    browser = SimpleNamespace(cdp=raw, Browser=Browser)
    agent = SimpleNamespace()
    model = SimpleNamespace(field_text=lambda _: None, post_json=lambda *_a, **_kw: None)
    monkeypatch.setitem(sys.modules, "browser_harness", SimpleNamespace(admin=SimpleNamespace(), helpers=helpers))
    monkeypatch.setitem(sys.modules, "jev_ultrafast", SimpleNamespace(browser=browser, agent=agent, model=model))
    monkeypatch.setattr(session, "_jev", None)
    monkeypatch.setenv("BU_NAME", "fixture")
    monkeypatch.setattr(session.providers, "apply", lambda: {})
    monkeypatch.setattr(session.providers, "route", lambda raw, _resolved: raw)
    loaded = session.load(runner.Ledger(0.0))
    assert loaded.cdp is browser.cdp
    why = "acknowledgement missing" if lost_reply else "no close frame"
    with pytest.raises(RuntimeError, match=why):
        loaded.cdp("Input.dispatchMouseEvent")
    assert isinstance(loaded.transport.error, session.TransportLost)
    assert session.transport_error() is loaded.transport.error
    with pytest.raises(RuntimeError, match=why):
        browser.cdp("Input.dispatchMouseEvent")
    assert calls == [True]


def test_transport_loss_during_session_start_marks_only_the_first_scenario_harness(group, monkeypatch):
    run, _fail, seen, state, _scenarios, _opts, _cdp = group

    def start(*_a, **_kw):
        state.error = session.TransportLost("connection closed during setup")
        raise state.error

    monkeypatch.setattr(session, "Session", start)
    results = run()["results"]
    assert [r["outcome"] for r in results] == ["harness", "skipped", "skipped", "skipped"]
    assert results[0].get("transport_lost") is True
    assert seen["daemons"] == 1


def test_a_typed_transport_loss_stops_the_group_even_without_a_shared_fuse(group, monkeypatch):
    run, _fail, _seen, _state, _scenarios, _opts, _cdp = group

    def lost(*_a, **_kw):
        raise session.TransportLost("connection closed")

    monkeypatch.setattr(runner, "run_scenario", lost)
    results = run()["results"]
    assert [r["outcome"] for r in results] == ["harness", "skipped", "skipped", "skipped"]


def test_a_response_timeout_keeps_the_existing_single_retry_without_opening_the_fuse(monkeypatch):
    state = SimpleNamespace(error=None)
    calls = []

    def raw(method, **kwargs):
        calls.append((method, kwargs))
        if len(calls) == 1:
            raise TimeoutError("slow page")
        return {"ready": True}

    call = session._patient_cdp(raw, 45, state)
    assert call("Runtime.evaluate", _response_timeout=1, expression="true") == {"ready": True}
    assert len(calls) == 2
    assert all(params["_response_timeout"] == 45 for _method, params in calls)
    assert state.error is None


@pytest.mark.parametrize("reply", [b"", b'{"result":', b"[]\n"])
def test_empty_cut_or_invalid_daemon_reply_never_acknowledges_an_action_or_reconnects(monkeypatch, reply):
    from browser_harness import helpers

    state = SimpleNamespace(error=None)
    seen = {"connects": 0, "sends": 0, "closes": 0}

    class Socket:
        chunks = [reply, b""]

        def settimeout(self, _timeout):
            pass

        def sendall(self, _data):
            seen["sends"] += 1

        def recv(self, _size):
            return self.chunks.pop(0)

        def close(self):
            seen["closes"] += 1

    def connect(*_a, **_kw):
        seen["connects"] += 1
        return Socket(), None

    monkeypatch.setattr(helpers.ipc, "connect", connect)
    send = session._checked_send(helpers._send, state)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="acknowledgement missing"):
            send({"method": "Input.dispatchMouseEvent", "params": {"type": "mousePressed"}})
    assert state.error is not None
    assert seen == {"connects": 1, "sends": 1, "closes": 1}


def test_empty_success_result_is_a_valid_daemon_acknowledgement():
    state = SimpleNamespace(error=None)
    send = session._checked_send(lambda *_a, **_kw: {"result": {}}, state)
    assert send({"method": "Input.dispatchMouseEvent"}) == {"result": {}}
    assert state.error is None


@pytest.mark.parametrize("meta_request,reply", [
    ({"meta": "pending_dialog"}, {"dialog": None}),
    ({"meta": "current_tab"}, {"targetId": "fixture-tab", "url": "http://localhost/", "title": "Fixture"}),
    ({"meta": "set_session", "session_id": "fixture-session", "target_id": "fixture-tab"},
     {"session_id": "fixture-session"}),
    ({"meta": "session"}, {"session_id": "fixture-session"}),
])
def test_meta_replies_without_cdp_result_keep_the_transport_healthy(meta_request, reply):
    state = SimpleNamespace(error=None)
    calls = []
    cdp_request = {"method": "Runtime.evaluate", "params": {"expression": "true"}}

    def raw(sent, **_kwargs):
        calls.append(sent)
        return reply if sent == meta_request else {"result": {"value": True}}

    send = session._checked_send(raw, state)
    try:
        received = send(meta_request)
    except session.TransportLost as error:
        raise AssertionError("a valid meta reply is not a missing CDP acknowledgement") from error
    assert received == reply and state.error is None
    assert send(cdp_request) == {"result": {"value": True}}
    assert state.error is None and calls == [meta_request, cdp_request]


@pytest.mark.parametrize("message", ["Target closed", "Session with given id not found"])
def test_closed_popup_command_error_does_not_poison_a_healthy_browser_transport(message):
    state = SimpleNamespace(error=None)
    calls = []

    def raw(method, session_id=None, **_kwargs):
        calls.append((method, session_id))
        if session_id == "closed-popup":
            raise RuntimeError(message)
        return {"ready": True}

    call = session._patient_cdp(raw, 30, state)
    try:
        call("Runtime.evaluate", session_id="closed-popup", expression="true")
    except session.TransportLost as error:
        raise AssertionError("a target-scoped rejection does not prove the browser transport is gone") from error
    except RuntimeError as error:
        assert str(error) == message
    else:
        raise AssertionError("the rejected popup command must still fail")
    assert state.error is None
    assert call("Runtime.evaluate", session_id="healthy-page", expression="true") == {"ready": True}
    assert calls == [("Runtime.evaluate", "closed-popup"), ("Runtime.evaluate", "healthy-page")]


def test_a_lost_transport_prevents_even_non_cdp_hooks_from_starting(monkeypatch):
    state = SimpleNamespace(error=session.TransportLost("connection closed"))
    monkeypatch.setattr(session, "_jev", SimpleNamespace(transport=state))
    page = session.Session.__new__(session.Session)
    calls = []
    monkeypatch.setattr(page, "_run_hook", lambda *_a: calls.append(True), raising=False)
    with pytest.raises(RuntimeError, match="connection closed"):
        page.run_hook({"command": "fixture command"}, "http://127.0.0.1/")
    assert calls == []


def test_a_lost_transport_prevents_shell_commands_from_starting(monkeypatch, tmp_path):
    state = SimpleNamespace(error=session.TransportLost("connection closed"))
    monkeypatch.setattr(session, "_jev", SimpleNamespace(transport=state))
    page = session.Session.__new__(session.Session)
    page.allow_commands, page.cwd = True, tmp_path
    calls = []
    monkeypatch.setattr(session.subprocess, "run", lambda *_a, **_kw: calls.append(True) or
                        SimpleNamespace(returncode=0, stdout="", stderr=""))
    with pytest.raises(RuntimeError, match="connection closed"):
        page.command("fixture command", "http://127.0.0.1/")
    assert calls == []
