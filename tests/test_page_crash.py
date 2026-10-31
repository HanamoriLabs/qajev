"""A page that crashes ("Aw, Snap") fails its test: a renderer crash is the product's fault, not QAJev's."""

import contextlib
import os
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from test_page_console import site as site
from test_ux_dom_live import ENDPOINT

from qajev import netlog, report, runner, session, smoke, suite, verdict


def test_the_network_log_hears_the_page_crash_and_not_a_frame_crash():
    log = netlog.Failures("ws://127.0.0.1:1/devtools/page/x")
    log.take({"method": "Inspector.targetCrashed", "params": {}, "sessionId": "frame-on-another-site"})
    assert log.crashed is None
    log.take({"method": "Inspector.targetCrashed", "params": {}})
    assert log.crashed is not None


def _tab(monkeypatch, crashed=1.0):
    transport = SimpleNamespace(error=None, stalled=None)
    monkeypatch.setattr(session, "_jev", SimpleNamespace(transport=transport))
    sent = []
    tab = session.Session.__new__(session.Session)
    tab.net = SimpleNamespace(crashed=crashed)
    tab.browser = SimpleNamespace(target="T1", call=lambda method, **params: sent.append((method, params)) or {})
    tab.jev = SimpleNamespace(cdp=lambda method, **params: {"targetInfo": {"url": "http://127.0.0.1:8125/play"}})
    tab.headless, tab.carried, tab.doing = True, {}, None
    return tab, transport, sent


def test_a_crashed_page_fails_every_later_call_at_once_and_names_its_url(monkeypatch):
    tab, transport, sent = _tab(monkeypatch)
    with pytest.raises(session.PageCrashed, match="the page crashed at http://127.0.0.1:8125/play"):
        tab.call("Runtime.evaluate", expression="1")
    assert sent == []  # nothing waits on a dead page
    assert isinstance(transport.stalled, session.PageCrashed)
    assert tab.page_crash() == {"url": "http://127.0.0.1:8125/play", "step": None}


def test_a_page_that_did_not_crash_is_called_as_before(monkeypatch):
    tab, transport, sent = _tab(monkeypatch, crashed=None)
    tab.call("Runtime.evaluate", expression="1")
    assert sent == [("Runtime.evaluate", {"expression": "1"})] and transport.stalled is None


def test_the_next_test_leaves_the_crashed_page_and_keeps_a_real_stall(monkeypatch):
    tab, transport, sent = _tab(monkeypatch)
    tab.page_crash()
    tab.recover_crash()
    assert tab.page_crash() is None and tab.net.crashed is None and transport.stalled is None
    assert sent == [("Page.navigate", {"url": "about:blank"})]
    stall = session.BrowserStalled("the browser stopped answering")
    transport.stalled, tab.net.crashed = stall, 2.0
    tab.crash = {"url": "x"}
    tab.recover_crash()
    assert transport.stalled is stall  # only the crash is forgiven, not a browser that stopped answering


@pytest.fixture
def group(monkeypatch, tmp_path):
    transport = SimpleNamespace(error=None, stalled=None)
    monkeypatch.setattr(session, "_jev", SimpleNamespace(transport=transport))
    monkeypatch.setattr(session, "configure_env", lambda _url: "fixture")
    monkeypatch.setattr(session, "stop_daemon", lambda: None)
    monkeypatch.setattr(runner.live, "frames", lambda *_a, **_kw: contextlib.nullcontext())
    monkeypatch.setattr(runner, "wait_for_quiet", lambda _opts: (True, 0))
    seen = {"began": 0}

    class Page:
        def __init__(self, *_a, **_kw):
            self.url, self.crash = "", None

        def page_socket(self):
            return None

        def close(self):
            pass

        def begin_test(self):
            seen["began"] += 1
            self.crash = None
            transport.stalled = None

        def set_device(self, *_a):
            pass

        def arm(self, *_a):
            pass

        def check_host(self, *_a):
            pass

        def navigate(self, url):  # as Session.navigate: the page is open, and the test reads it next
            self.url, self.doing = url, "reading the page"

        def _crash_if_asked(self):
            if "/crash" in self.url:
                self.crash = {"url": self.url, "step": getattr(self, "doing", None)}
                transport.stalled = session.PageCrashed(f"the page crashed at {self.url}")
            if self.crash:
                raise transport.stalled

        def probe(self, *_a):
            if not self.url.endswith("/crash-in-hook"):
                self._crash_if_asked()
            return {"url": self.url, "status": 200, "text": [True], "probe": {}}

        def run_hook(self, _hook, _url):
            self._crash_if_asked()

        def screenshot(self, *_a):
            return None

        def page_crash(self):
            return self.crash

    monkeypatch.setattr(session, "Session", Page)
    parsed = suite.parse({"base_url": "http://127.0.0.1", "scenarios": [
        {"name": "crashes", "url": "/crash", "about": "the page loads", "expect": {"text": "ready"}, "settle": 0},
        {"name": "crashes in a hook", "url": "/crash-in-hook", "about": "the hook runs",
         "before": [{"js": "start()"}], "expect": {"text": "ready"}, "settle": 0},
        {"name": "later", "url": "/later", "about": "a later page still loads", "expect": {"text": "ready"},
         "settle": 0},
    ]})
    opts = runner.Options(shots=False, ux="off", load_high=None)
    meta = {"headless": True, "hosts": ["127.0.0.1"], "guard": {}, "cwd": str(tmp_path)}

    def run():
        return runner.run_group(parsed.scenarios, opts=opts, cdp_url="http://127.0.0.1:9350", suite_meta=meta,
                                run_dir=tmp_path, ledger=runner.Ledger(0.0))["results"]

    return run, parsed, seen


def test_a_page_crash_grades_fail_with_its_url_and_last_step_and_the_gate_fails(group, tmp_path):
    run, parsed, seen = group
    results = run()
    assert [r["outcome"] for r in results] == ["fail", "fail", "pass"]
    assert results[0]["stop"] == "page_crashed"
    assert results[0]["reason"] == "the page crashed at http://127.0.0.1/crash; last step: reading the page"
    assert results[0]["page_crashed"] == {"url": "http://127.0.0.1/crash", "last_step": "reading the page"}
    assert results[1]["reason"] == ("the page crashed at http://127.0.0.1/crash-in-hook; last step: "
                                    "before hook 1 of 1")
    assert seen["began"] == 3  # each later test began on its own page
    built = report.build(parsed, results, [], browser={}, started_at=time.time(), strict=False, interrupted=False,
                         run_dir=tmp_path)
    assert built["gate"] == "FAIL"


def test_a_crash_is_never_the_planted_fault_caught():
    crashed = {"outcome": "fail", "stop": "page_crashed", "reason": "the page crashed at x; last step: y",
               "checks": [{"check": "text 'ready'", "ok": False}]}
    graded, why = verdict.planted(crashed, "text 'ready'")
    assert graded == "fail" and why.startswith("the page crashed, which is never the planted fault caught")
    assert verdict.gate([graded]) == "FAIL"


def test_smoke_fails_the_crashed_page_and_leaves_it(monkeypatch):
    left = []
    tab = SimpleNamespace(page_crash=lambda: {"url": "http://127.0.0.1/game"},
                          recover_crash=lambda: left.append(True))
    result = {"name": "game", "url": "http://127.0.0.1/game"}
    smoke._crashed(tab, result, "http://127.0.0.1/game")
    assert result["outcome"] == "fail" and result["stop"] == "page_crashed"
    assert result["reason"] == "the page crashed at http://127.0.0.1/game; last step: reading the page"
    assert left == [True]


def test_a_crash_on_jevs_goal_names_jevs_last_action():
    scenario = SimpleNamespace(name="play", url="http://127.0.0.1/", goal="Play a round", mode="readonly")
    result = runner._crashed_result(scenario, {"url": "http://127.0.0.1/game", "step": "Jev's goal"}, None,
                                    time.monotonic(), {"history": [{"step": 3, "action": "Play", "kind": "click"}]})
    assert result["outcome"] == "fail"
    assert result["reason"] == "the page crashed at http://127.0.0.1/game; last step: Jev's step 3: click 'Play'"


@pytest.fixture
def crashable():
    from test_ux_dom_live import CDP

    cdp = CDP(ENDPOINT)
    target = cdp.call("Target.createTarget", url="about:blank")["targetId"]
    sid = cdp.call("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
    tab = session.Session.__new__(session.Session)
    tab.browser = SimpleNamespace(target=target, call=lambda method, **params: cdp.call(method, session_id=sid,
                                                                                         **params))
    tab.jev = SimpleNamespace(cdp=cdp.call)
    tab.headless, tab.carried, tab.last_test_page, tab.guard_cfg, tab.agent = True, {}, None, None, None
    tab.hosts, tab.guard_opts, tab.device, tab.script_id, tab.loaded_at = set(), {}, None, None, 0
    tab.ux_observer, tab.ux_script_id = None, None
    tab.net = netlog.start(f"ws://127.0.0.1:{urlsplit(ENDPOINT).port}/devtools/page/{target}")
    tab.net.ask("Runtime.evaluate", expression="1")  # its Inspector.enable went first, on the same link
    yield tab, cdp
    tab.net.stop()
    cdp.call("Target.closeTarget", targetId=target)
    cdp.socket.close()


@pytest.mark.skipif(not ENDPOINT or os.uname().sysname != "Linux", reason="requires approved CPU fixture browser")
def test_browser_hears_a_real_renderer_crash_and_the_next_page_loads(crashable, site):
    tab, _cdp = crashable
    tab.navigate(site + "/")
    assert tab.doing == "reading the page"
    # chrome://kill ends the renderer, as a real crash does. Page.crash and chrome://crash only hang it in headless
    # Chromium 153 on Linux, with no crash event (measured on the test box, 1 Nov).
    tab.call("Page.navigate", url="chrome://kill")
    deadline = time.monotonic() + 10
    while tab.net.crashed is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert tab.net.crashed is not None
    began = time.monotonic()
    with pytest.raises(session.PageCrashed):
        tab.evaluate("1")
    assert time.monotonic() - began < 2  # no call waits on the dead page
    assert tab.page_crash()["url"] in {site + "/", "chrome://kill/"}  # the browser's word for the dead tab
    assert tab.page_crash()["step"] == "reading the page"  # the kill went by a raw call, not by navigate
    tab.recover_crash()
    tab.navigate(site + "/")
    assert tab.evaluate("1 + 1") == 2 and tab.page_crash() is None


def test_a_multiplayer_client_whose_page_crashes_fails_the_test(monkeypatch, tmp_path):
    from test_clients import FakeJudge, FakePlayer
    from test_clients import suite as multiplayer

    from qajev import clients

    class Crashing(FakePlayer):
        crash = None

        def run_hook(self, hook, url):
            if hook.get("js") == "game.walk()" and self.n == 2:
                self.crash = {"url": self.url, "step": None}
                raise session.PageCrashed(f"the page crashed at {self.url}")
            return True

        def page_crash(self):
            return self.crash

    made = []

    def player(*_a, **_kw):
        made.append(Crashing(10))
        made[-1].n = len(made)
        return made[-1]

    monkeypatch.setattr(session, "Session", player)
    monkeypatch.setattr(session, "Tab", lambda *a, **k: FakeJudge())
    s = multiplayer(steps=[{"all": {"js": "game.join()"}}, {"all": {"js": "game.walk()"}}],
                    clients=3, expect={"text": "Room"}).scenarios[0]
    result = clients.run(s, ledger=None, hosts={"127.0.0.1"}, run_dir=tmp_path,
                         suite_meta={"headless": True, "guard": {}})
    assert result["outcome"] == "fail" and result["stop"] == "page_crashed"
    assert result["page_crashed"]["client"] == "p2"
    assert result["reason"].startswith(f"the page crashed at {made[1].url} (client p2); last step: step 2")


def test_a_sign_in_page_that_crashes_fails_the_tests_and_a_failed_sign_in_stays_harness():
    crashed = SimpleNamespace(page_crash=lambda: {"url": "http://127.0.0.1/login", "step": "opening"})
    done = {"account": "test", "ok": False, "reason": "TimeoutError: the page crashed at http://127.0.0.1/login"}
    runner._sign_in_crash(crashed, done)
    assert done["page_crashed"] == {"url": "http://127.0.0.1/login", "last_step": "signing in"}
    assert runner.sign_in_grade(done) == ("fail", "the page crashed at http://127.0.0.1/login while signing in")
    plain = {"account": "test", "ok": False, "reason": "the password was refused"}
    runner._sign_in_crash(SimpleNamespace(page_crash=lambda: None), plain)
    assert runner.sign_in_grade(plain) == ("harness", "sign-in failed: the password was refused")


def test_an_electron_page_crash_fails_its_step_once_and_an_asked_crash_does_not():
    from qajev import native

    game = SimpleNamespace(errors=[])
    stalled = {"name": "play a round", "outcome": "harness", "stop": "browser_error", "reason": "the app stopped"}
    native._crash_grade(game, stalled)
    assert stalled["outcome"] == "harness"  # no crash: as before
    game.errors.append(native.PAGE_CRASHED)
    native._crash_grade(game, stalled)
    assert stalled["outcome"] == "fail" and stalled["stop"] == "page_crashed"
    assert stalled["reason"] == "the page crashed during the step 'play a round'; the app stopped"
    later = {"name": "the menu", "outcome": "pass", "stop": "checked", "reason": "all checks passed"}
    native._crash_grade(game, later)
    assert later["outcome"] == "pass"  # the crash was the earlier step's
    game.errors = [native.PAGE_CRASHED]  # a relaunch: a new list, and a new crash
    native._crash_grade(game, later)
    assert later["outcome"] == "fail"
