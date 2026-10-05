"""Multiplayer scenarios (clients.py), without a browser: the suite format, when each client acts, and the report.
test_live_clients.py runs five real clients against a local room."""

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
                                    {"check": "same roster", "js": "true"}]
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
    assert max(started.values()) - min(started.values()) < 0.15
    assert len({v for _, v, e in done if e is None}) == 4  # each on its own thread
    assert clients._first_error(done, "step 2 (wait_for)") == (
        "hook_failed", "[p2] step 2 (wait_for): wait_for 'game.ready' timed out")
    staggered = {}
    clients.together(players[:3], lambda c: staggered.setdefault(c.name, time.monotonic()), [0.0, 0.1, 0.2])
    gaps = [staggered["p2"] - staggered["p1"], staggered["p3"] - staggered["p2"]]
    assert all(0.07 < g < 0.35 for g in gaps), gaps  # 0.1 s apart, not together (0) and not in sequence


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
