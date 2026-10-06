"""Multiplayer scenarios (clients.py), without a browser: the suite format, when each client acts, and the report.
test_live_clients.py runs five real clients against a local room."""

import json
import random
import threading
import time
from types import SimpleNamespace

import pytest

from qajev import clients, report
from qajev.session import GuardMissing, HookFailed
from qajev.suite import SuiteError, parse

ROOM = "http://127.0.0.1:8765/play?room=qa-{run}&player={client}&slot={i}"


def suite(**scenario):
    return parse({"device": "desktop", "scenarios": [{"name": "room", "url": ROOM, "clients": 3,
                                                     "state": "({id: game.id})", **scenario}]})


def test_a_scenario_with_clients_names_them_and_parses_its_steps():
    s = suite(steps=[
        {"all": {"wait_for": {"js": "game.ready", "timeout": 20}}},
        {"all": {"js": "game.join()"}, "stagger": 200, "jitter": 50},
        {"client": "p1", "js": "game.chat('hi')"},
        {"client": ["p2", "p3"], "reload": True},
        {"snapshot": "joined", "until": "game.players.length === 3", "timeout": 5, "settle": 2,
         "expect": ["clients.length === 3", {"check": "same roster", "js": "true"}]},
    ], expect={"text": "Room", "across": "clients.every(c => c.state.id)"}).scenarios[0]
    assert [c["name"] for c in s.clients] == ["p1", "p2", "p3"] and s.clients[0]["url"] == ROOM
    kinds = [(st["kind"], st.get("clients"), st.get("stagger"), st.get("jitter")) for st in s.steps]
    assert kinds == [("all", ["p1", "p2", "p3"], 0, 0), ("all", ["p1", "p2", "p3"], 200, 50),
                     ("client", ["p1"], 0, 0), ("client", ["p2", "p3"], 0, 0), ("snapshot", None, None, None)]
    assert s.steps[3]["hook"] == {"reload": True}
    assert s.steps[4]["expect"] == [{"check": "clients.length === 3", "js": "clients.length === 3"},
                                    {"check": "same roster", "js": "true", "says": "same roster"}]  # named: its words
    assert s.expect["across"] == [{"check": "clients.every(c => c.state.id)", "js": "clients.every(c => c.state.id)"}]
    named = suite(clients=["host", {"name": "guest", "url": "http://127.0.0.1:8765/guest"}],
                  steps=[{"client": "guest", "js": "1"}], expect={"text": "Room"}).scenarios[0]
    assert named.clients == [{"name": "host", "url": ROOM}, {"name": "guest", "url": "http://127.0.0.1:8765/guest"}]


@pytest.mark.parametrize("change, error", [
    ({"goal": "Join the room"}, "steps, not a goal"),
    ({"clients": 1}, "from 2 to 12 clients"),
    ({"clients": 13}, "from 2 to 12 clients"),
    ({"clients": ["p1", "p1"]}, "names must be different"),
    ({"clients": ["p1", "no spaces"]}, "must be a name"),
    ({"steps": [{"client": "p9", "js": "1"}]}, "no client named ['p9']"),
    ({"steps": [{"all": {"js": "1"}, "client": "p1"}]}, "exactly one of all, client or snapshot"),
    ({"steps": [{"client": "p1", "js": "1", "stagger": 100}]}, "stagger and jitter belong to an `all` step"),
    ({"steps": [{"all": {"command": "ls"}}]}, "needs one hook"),
    ({"steps": [{"all": {"js": "1"}, "stagger": -5}]}, "must be milliseconds"),
    ({"steps": [{"snapshot": "bad name"}]}, "must be a name"),
    ({"expect": {"fetch": "/api"}}, "unknown key(s) ['fetch']"),
    ({"expect": {"across": "true"}, "state": None}, "expect.across reads each client's state"),
    ({"steps": [{"snapshot": "s"}], "state": None, "expect": {"text": "x"}}, "a snapshot reads each client's state"),
])
def test_a_malformed_scenario_with_clients_is_refused_with_the_reason(change, error):
    base = {"name": "room", "url": ROOM, "clients": 3, "state": "1", "steps": [{"all": {"js": "1"}}],
            "expect": {"text": "Room"}}
    scenario = {k: v for k, v in {**base, **change}.items() if v is not None}  # None: leave the key out
    with pytest.raises(SuiteError) as e:
        parse({"device": "desktop", "scenarios": [scenario]})
    assert error in str(e.value)


def test_clients_keys_belong_to_a_scenario_with_clients_only():
    with pytest.raises(SuiteError, match="steps belongs to a scenario with clients"):
        parse({"scenarios": [{"name": "a", "url": ROOM, "expect": {"text": "x"}, "steps": []}]})
    with pytest.raises(SuiteError, match="expect.across belongs to a scenario with clients"):
        parse({"scenarios": [{"name": "a", "url": ROOM, "expect": {"across": "true"}}]})
    with pytest.raises(SuiteError, match="needs its own url"):
        parse({"base_url": "http://127.0.0.1:8765",
               "scenarios": [{"name": "a", "url": "/x", "expect": {"text": "x"}},
                             {"name": "b", "clients": 2, "expect": {"text": "x"}}]})
    with pytest.raises(SuiteError, match="ran in its own clients' tabs"):  # nothing continues the clients' page
        parse({"scenarios": [{"name": "a", "url": ROOM, "clients": 2, "expect": {"text": "x"}},
                             {"name": "b", "expect": {"text": "y"}}]})


def test_each_client_gets_its_own_address():
    assert clients.client_url(ROOM, "p2", 2, "a1b2c3") == ("http://127.0.0.1:8765/play?room=qa-a1b2c3&player=p2"
                                                          "&slot=2")


def test_stagger_and_jitter_say_when_each_client_acts():
    assert clients.offsets(4, 200, 0, random.Random(1)) == [0.0, 0.2, 0.4, 0.6]
    jittered = clients.offsets(5, 0, 80, random.Random(7))
    assert all(0 <= x <= 0.08 for x in jittered) and len(set(jittered)) > 1  # jitter: never before the start
    assert jittered == clients.offsets(5, 0, 80, random.Random(7))  # the same seed, the same offsets


def test_all_clients_act_at_the_same_instant_and_an_error_names_its_client():
    started = {}

    def act(c):
        started[c.name] = time.monotonic()
        time.sleep(0.2)
        if c.name == "p2":
            raise HookFailed("wait_for 'game.ready' timed out")
        return threading.get_ident()

    players = [clients.Client(f"p{i}", "u", None) for i in range(1, 6)]
    began = time.monotonic()
    done = clients.together(players, act)
    # together, not one after another: in sequence the five 0.2 s acts would take 1 s and start 0.8 s apart (the
    # margins are for slow CI runners: macOS on 3.13 started them 60 ms apart)
    assert time.monotonic() - began < 0.8
    assert max(started.values()) - min(started.values()) < 0.4
    assert len({v for _, v, e in done if e is None}) == 4  # each on its own thread
    assert clients._first_error(done, "step 2 (wait_for)") == (
        "hook_failed", "[p2] step 2 (wait_for): wait_for 'game.ready' timed out")
    # A busy runner can only start a thread late, never early: gaps of 46 and 135 ms were seen for a 100 ms stagger,
    # and once p1 started after p2. So the contract checked is: nobody before its own offset. Without the stagger,
    # p2 and p3 would start about 0.05 s after the call.
    staggered = {}
    t0 = time.monotonic()
    clients.together(players[:3], lambda c: staggered.setdefault(c.name, time.monotonic()), [0.0, 0.1, 0.2])
    for name, offset in (("p1", 0.0), ("p2", 0.1), ("p3", 0.2)):
        assert staggered[name] - t0 >= offset + 0.045, (name, staggered[name] - t0)  # 0.05 s start margin


def test_a_clients_error_becomes_the_stop_it_means():
    assert clients._first_error([("p1", None, None), ("p3", None, GuardMissing("guard absent on x"))], "opening") == (
        "guard_missing", "[p3] opening: guard absent on x")
    assert clients._first_error([("p1", None, RuntimeError("socket closed"))], "step 1 (js)") == (
        "browser_error", "[p1] step 1 (js): RuntimeError: socket closed")
    assert clients._first_error([("p1", 1, None)], "x") is None


def test_the_report_shows_each_client_and_when_each_reached_a_snapshot(tmp_path):
    ledger = {"usd": 0.0, "usd_typesafe_estimated": 0.0, "usd_text": 0.0, "calls": {"typesafe": 0, "text": 0},
              "tokens": {"typesafe": 0, "text": 0}, "errors": 0, "text_cost_reported": True, "cap_usd": 1.0}
    result = {"name": "room", "url": ROOM, "goal": None, "mode": "readonly", "outcome": "fail", "seconds": 4.0,
              "reason": "snapshot chat: every player got all five chats (returned False)", "findings": [],
              "screens": [], "checks": [{"check": "snapshot chat: every player got all five chats", "ok": False,
                                         "detail": "returned False"}],
              "clients": [{"name": "p1", "url": "u1", "shot": "shots/room-p1.jpg"},
                          {"name": "p2", "url": "u2", "shot": None}],
              "steps": [{"step": 1, "do": "js", "clients": ["p1", "p2"], "starts_ms": [0, 100], "ms": 140},
                        {"step": 2, "snapshot": "chat", "timed_out": ["p2"], "errors": [], "ms": 3010}],
              "snapshots": {"chat": [{"name": "p1", "at": 1000, "state": '{"chat": ["1:hi"]}'},
                                     {"name": "p2", "at": None, "timed_out": True, "state": '{"chat": []}'}]}}
    data = report.build(SimpleNamespace(name="mp"), [result], [ledger], browser={}, started_at=time.time(),
                        strict=False, interrupted=False, run_dir=tmp_path)
    report.write(tmp_path, data)
    md, html = (tmp_path / "report.md").read_text(), (tmp_path / "report.html").read_text()
    assert "- Clients: [p1](shots/room-p1.jpg), p2" in md
    assert "- Snapshot chat: p1 +0 ms, p2 timed out" in md
    assert 'alt="p1 at the end"' in html and "p2 (no screenshot)" in html
    assert "<td>0, 100</td>" in html and "timed out: p2" in html  # when each client acted; who never got there


def test_a_client_tab_starts_the_browser_daemon_and_keeps_qajevs_rules_in_its_own_context():
    # The first live run: a run whose scenarios all had clients never started the daemon (FileNotFoundError on its
    # socket), as only Jev's Browser started it. And the default context's download and permission rules do not
    # reach a new browser context.
    from qajev.session import Tab

    calls = []

    def cdp(method, session_id=None, **params):
        calls.append((method, params.get("browserContextId")))
        return {"Target.createBrowserContext": {"browserContextId": "ctx1"}, "Target.createTarget": {"targetId": "t1"},
                "Target.attachToTarget": {"sessionId": "s1"}}.get(method, {})

    tab = Tab(cdp, lambda: calls.append(("ensure_daemon", None)))
    assert calls[0] == ("ensure_daemon", None) and calls[1] == ("Target.createBrowserContext", None)
    assert ("Browser.setDownloadBehavior", "ctx1") in calls  # no downloads in this context either
    assert calls.count(("Browser.setPermission", "ctx1")) == 2  # microphone and camera denied here too
    assert ("Target.createTarget", "ctx1") in calls  # the tab lives in its own context
    tab.close()
    assert calls[-2:] == [("Target.closeTarget", None), ("Target.disposeBrowserContext", "ctx1")]


DAEMON_LINE = 65536  # browser_harness's daemon reads a command as one line, at most 64 KiB (asyncio's default limit)


class FakePlayer:
    """A client's page: every hook works, and its state is `size` characters (a game's roster, gear and events)."""

    def __init__(self, size):
        self.size, self.jev = size, SimpleNamespace(cdp=None, admin=SimpleNamespace(ensure_daemon=lambda: None))

    def arm(self, mode): pass
    def set_device(self, device): pass
    def navigate(self, url): self.url = url
    def probe(self, expect): return {"url": self.url, "status": 200, "text": [True], "probe": {}}
    def why_failed(self, url): return None
    def run_hook(self, hook, url): return True
    def evaluate(self, expression, timeout_ms=15000): return {"at": 1, "state": {"blob": "x" * self.size}}
    def screenshot(self, path): return None
    def close(self): pass


class FakeJudge:
    """QAJev's judge tab, behind the daemon: a command longer than its line limit is refused with the daemon's own
    words, and `fail_on` (the nth across-check evaluation) raises as a crashed target would."""

    def __init__(self, cdp=None, ensure_daemon=None, fail_on=None):
        self.lines, self.judged, self.fail_on = [], 0, fail_on

    def call(self, method, **params):
        line = len(json.dumps({"method": method, "params": params, "session_id": "s" * 32})) + 1
        self.lines.append(line)
        if line > DAEMON_LINE:
            raise RuntimeError("Separator is found, but chunk is longer than limit")
        if "clients, snapshots" in params.get("expression", ""):
            self.judged += 1
            if self.judged == self.fail_on:
                raise RuntimeError("Target crashed")
        return {"result": {"value": {"value": True}}}

    def close(self): pass


def run_players(monkeypatch, tmp_path, size, fail_on=None):
    from qajev import session

    judge = FakeJudge(fail_on=fail_on)
    monkeypatch.setattr(session, "Session", lambda *a, **k: FakePlayer(size))
    monkeypatch.setattr(session, "Tab", lambda *a, **k: judge)
    s = suite(steps=[{"all": {"js": "game.join()"}},
                     {"snapshot": "joined", "expect": ["clients.length === 5"]},
                     {"all": {"js": "game.walk()"}, "stagger": 10},
                     {"snapshot": "walking", "expect": ["clients.every((c) => c.state.blob)"]},
                     {"client": "p1", "js": "game.chat('hi')"}],
              clients=5, expect={"text": "Room", "across": "clients.length === 5"}).scenarios[0]
    result = clients.run(s, ledger=None, hosts={"127.0.0.1"}, run_dir=tmp_path,
                         suite_meta={"headless": True, "guard": {}})
    return result, judge


def test_large_client_states_reach_the_judge_in_commands_the_daemon_accepts(monkeypatch, tmp_path):
    # verse1's five Verse players (5 Oct, 2 runs of 2): each check carried every client's state of every snapshot
    # inline, so the "walking" check was the first command over the daemon's 64 KiB line, and the run broke there.
    result, judge = run_players(monkeypatch, tmp_path, size=8000)
    assert result["stop"] == "checked", result["reason"]
    assert [st.get("snapshot") or st["do"] for st in result["steps"]] == ["js", "joined", "js", "walking", "js"]
    assert max(judge.lines) <= DAEMON_LINE
    assert result["outcome"] == "pass" and len(result["checks"]) == 2 + 5 + 1  # snapshots, each client, across


def test_a_run_that_breaks_mid_scenario_is_never_a_pass_and_says_where_and_why(monkeypatch, tmp_path):
    from qajev import verdict

    result, _ = run_players(monkeypatch, tmp_path, size=10, fail_on=2)  # the "walking" check's evaluation
    assert result["outcome"] == "harness", result["reason"]
    assert verdict.gate([result["outcome"]]) == "INCOMPLETE"
    assert "step 4 (snapshot walking)" in result["reason"] and "Target crashed" in result["reason"]
    assert "not run: step 5 (js), the final checks" in result["reason"]
    assert result["not_run"] == ["step 5 (js)", "the final checks"]
    assert result["stop_detail"] == "step 4 (snapshot walking): RuntimeError: Target crashed"
    from qajev.mcp_server import _trim  # what qa_job and qa_run_suite hand an agent

    kept = _trim({"scenarios": [result], "run_dir": str(tmp_path)}, verbose=False)["scenarios"][0]
    assert kept["stop_detail"] == result["stop_detail"] and kept["not_run"] == result["not_run"]
    ledger = {"usd": 0.0, "usd_typesafe_estimated": 0.0, "usd_text": 0.0, "calls": {"typesafe": 0, "text": 0},
              "tokens": {"typesafe": 0, "text": 0}, "errors": 0, "text_cost_reported": True, "cap_usd": 1.0}
    data = report.build(SimpleNamespace(name="mp"), [{**result, "about": "x"}], [ledger], browser={},
                        started_at=time.time(), strict=False, interrupted=False, run_dir=tmp_path)
    report.write(tmp_path, data)
    assert "Target crashed" in (tmp_path / "report.html").read_text()
    assert data["gate"] == "INCOMPLETE"


def test_a_described_multiplayer_run_that_passes_is_a_pass_not_incomplete(monkeypatch, tmp_path):
    # verse1 (6 Oct, job 20261006-214802-091b): 13 of 13 checks passed and the gate said INCOMPLETE, because the
    # checks judged across clients lost their plain words on the way to the report.
    from qajev import session

    monkeypatch.setattr(session, "Session", lambda *a, **k: FakePlayer(10))
    monkeypatch.setattr(session, "Tab", lambda *a, **k: FakeJudge())
    s = suite(steps=[{"all": {"js": "game.join()"}},
                     {"snapshot": "joined", "expect": [{"js": "clients.length === 5", "says": "all five joined"}]}],
              clients=5, about="five players see the same room",
              expect={"text": "Room", "across": [{"js": "clients.length === 5", "says": "nobody dropped out"}]})
    result = clients.run(s.scenarios[0], ledger=None, hosts={"127.0.0.1"}, run_dir=tmp_path,
                         suite_meta={"headless": True, "guard": {}})
    assert result["outcome"] == "pass", result["reason"]
    ledger = {"usd": 0.0, "usd_typesafe_estimated": 0.0, "usd_text": 0.0, "calls": {"typesafe": 0, "text": 0},
              "tokens": {"typesafe": 0, "text": 0}, "errors": 0, "text_cost_reported": True, "cap_usd": 1.0}
    data = report.build(s, [result], [ledger], browser={}, started_at=time.time(), strict=False, interrupted=False,
                        run_dir=tmp_path)
    assert data.get("not_described") is None, data.get("not_described")
    assert data["gate"] == "PASS"
    words = [line["words"] for line in data["plan"][0]["checks"]]
    assert "all five joined" in " ".join(map(str, words)) and "nobody dropped out" in " ".join(map(str, words))


def test_a_command_over_the_daemons_limit_says_so_in_plain_words():
    from qajev.session import too_long

    error = RuntimeError("Separator is found, but chunk is longer than limit")
    said = too_long("Runtime.evaluate", {"expression": "x" * 70000}, error)
    assert said.startswith("Runtime.evaluate: a 68 KB command is over the browser daemon's 64 KB limit per command")
    assert "chunk is longer than limit" in said  # the daemon's own words stay, for a search
    assert too_long("Page.navigate", {}, RuntimeError("Target closed")) is None  # raised as it was
    huge = too_long("Runtime.evaluate", {"expression": "x" * 2_000_000}, BrokenPipeError(32, "Broken pipe"))
    assert huge.startswith("Runtime.evaluate: a 1953 KB command is over the browser daemon's 64 KB limit")
