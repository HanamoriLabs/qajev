"""Why a run's moves went stale and why its resources failed to load, recorded as it runs (no browser: fakes and a
local WebSocket stand in for Jev and Chrome; test_live.py proves both against a real page)."""

import json
import threading
from types import SimpleNamespace

from qajev import netlog, top, verdict
from qajev import session as session_mod

URL = "http://127.0.0.1:8000/wardrobe"


def guard(text):  # Jev's guard for a button (jev_ultrafast/snapshot.js): its identity ... the text around it
    return [7, "button", "Character", None, None, None, None, False, None, None, None, None, None, text]


def page(text, around, *, url=URL, actions=("Character",)):
    return {"url": url, "title": "Wardrobe", "text": text, "scroll": {"y": 0},
            "actions": [{"id": f"e{i}", "node": 7 + i, "kind": "click", "label": label}
                        for i, label in enumerate(actions)] + [{"id": "wait", "kind": "wait", "label": "Wait"}],
            "page_key": [1.5, url, 0, 0, 1120, 780, []], "guards": {"7": guard(around)}, "fingerprint": text}


class StalePage(ValueError):
    pass


def fake_session(before, after, now):
    s = session_mod.Session.__new__(session_mod.Session)  # no browser: Jev's agent and the page are faked
    state = {"decisions": [], "page": before, "decision": None, "status": "ready"}

    def command(name, _body=None):
        if name == "predict":
            state["decisions"].append({"choice": "e0", "operation": "CLICK"})
            return
        raise StalePage("Target changed or is covered. Observe again.")

    s.jev = SimpleNamespace(browser=SimpleNamespace(StalePage=StalePage))
    s.agent = SimpleNamespace(state=state, command=command)
    s.require_guard = lambda: None
    s.evaluate = lambda expression, timeout_ms=0: now
    s.observe = lambda: state.update(page=after)
    s.last_stale = None
    return s, state


def test_a_stale_decision_records_jevs_reason_and_what_changed():
    # verse1's wardrobe (job 20261004-122053-a3e4): 27 decisions for "Character" at p 0.99, no action, no reason.
    before = page("Wardrobe\nScore 41", "Wardrobe Score 41")
    after = page("Wardrobe\nScore 42", "Wardrobe Score 42")
    now = {"target": None, "key": before["page_key"], "guard": guard("Wardrobe Score 42")}
    s, state = fake_session(before, after, now)

    assert s.tick() is False
    why = state["decisions"][-1]["stale"]
    assert why["reason"] == "Target changed or is covered. Observe again."
    assert why["target"] == "click 'Character': still clickable"
    assert "target's text around it: 'Wardrobe Score 41' → 'Wardrobe Score 42'" in why["changed"]
    assert "text: 'Score 41' → 'Score 42'" in why["changed"]
    assert s.last_stale is why and state["page"] is after and state["status"] == "ready"
    words = verdict.stale_words(why)
    assert words.startswith("Target changed or is covered. Observe again. click 'Character': still clickable; ")
    assert verdict.screens(state["decisions"])[0]["stale"] == words  # what qajev top and the dashboard show


def test_a_covered_target_is_named_and_a_new_page_lists_what_came_and_went():
    before = page("Shop", "Shop", actions=("Character", "Hats"))
    after = page("Checkout", "Checkout", url=URL + "/checkout", actions=("Character", "Pay"))
    now = {"target": 'covered by div#toast "Saved"', "key": [2.5, URL + "/checkout", 0, 0, 1120, 780, []],
           "guard": guard("Checkout")}
    s, state = fake_session(before, after, now)

    s.tick()
    why = state["decisions"][-1]["stale"]
    assert why["target"] == "click 'Character': covered by div#toast \"Saved\""
    assert why["changed"][:2] == ["a new page load", f"address: {URL!r} → {URL + '/checkout'!r}"]
    assert "controls: -'Hats', +'Pay'" in why["changed"] and len(why["changed"]) <= 6
    assert not any(c.startswith("address:") for c in why["changed"][2:])  # said once


def test_nothing_jev_reads_changed_says_so():
    assert verdict.stale_words({"reason": "Page changed since the decision. Choose again.", "changed": []}) == (
        "Page changed since the decision. Choose again. (nothing Jev reads changed: it moved or re-rendered)")
    assert verdict.stale_words(None) == ""


def test_a_failed_resource_finding_carries_the_reason():
    probe = {"errors": [{"kind": "resource", "detail": "http://h/main.js"},
                        {"kind": "resource", "detail": "http://cdn.example/three.js"},
                        {"kind": "resource", "detail": "IMG"}]}
    asked = []

    def why(url):
        asked.append(url)
        return "net::ERR_EMPTY_RESPONSE"

    found = verdict.findings_from_probe(probe, scenario="s", url="http://h/", first_party_hosts={"h"}, why=why)
    assert [f["detail"] for f in found] == ["http://h/main.js (net::ERR_EMPTY_RESPONSE)", "IMG"]
    assert asked == ["http://h/main.js"]  # never asked for a third party's resource, nor for a bare tag name
    plain = verdict.findings_from_probe(probe, scenario="s", url="http://h/", first_party_hosts={"h"})
    assert [f["detail"] for f in plain] == ["http://h/main.js", "IMG"]


def test_qajev_top_shows_why_a_decision_went_stale():
    decisions = [{"at": 0, "scenario": "wardrobe", "screen": "Shop", "chose": "Character", "p": 0.99, "ms": 300,
                  "stale": "Target changed or is covered. Observe again. click 'Character': covered by canvas"}]
    lines = [text for text, _ in top.render_decisions({"id": "j", "state": "running"}, decisions, width=160)]
    assert lines[-1].strip() == ("went stale: Target changed or is covered. Observe again. click 'Character': "
                                 "covered by canvas")


def events(*items):
    return [json.dumps({"method": m, "params": p}) for m, p in items]


def test_the_network_log_hears_chromes_reasons_on_a_link_of_its_own(monkeypatch):
    from websockets.sync.server import serve

    sent = [("Network.requestWillBeSent", {"requestId": "1", "request": {"url": "http://h/reset.png#top"}}),
            ("Network.requestWillBeSent", {"requestId": "2", "request": {"url": "https://cdn.example/three.js"}}),
            ("Network.requestWillBeSent", {"requestId": "3", "request": {"url": "http://h/old.js"}}),
            ("Network.requestWillBeSent", {"requestId": "4", "request": {"url": "https://api.example/x.json"}}),
            ("Network.loadingFailed", {"requestId": "1", "errorText": "net::ERR_EMPTY_RESPONSE"}),
            ("Network.loadingFailed", {"requestId": "2", "errorText": "net::ERR_NAME_NOT_RESOLVED"}),
            ("Network.loadingFailed", {"requestId": "3", "errorText": "net::ERR_ABORTED", "canceled": True}),
            ("Network.loadingFailed", {"requestId": "4", "errorText": "net::ERR_FAILED",
                                       "corsErrorStatus": {"corsError": "MissingAllowOriginHeader"}}),
            ("Network.loadingFailed", {"requestId": "99", "errorText": "net::ERR_FAILED"})]  # never seen: ignored
    asked, release = [], threading.Event()

    def chrome(ws):
        asked.append(json.loads(ws.recv()))
        for message in events(*sent):
            ws.send(message)
        release.wait(5)

    with serve(chrome, "127.0.0.1", 0) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        failures = netlog.start(f"ws://127.0.0.1:{server.socket.getsockname()[1]}")
        assert failures is not None
        try:
            assert failures.reason("http://h/reset.png", wait=5) == "net::ERR_EMPTY_RESPONSE"
            cors = failures.reason("https://api.example/x.json", wait=5)
            assert cors == "net::ERR_FAILED, CORS: MissingAllowOriginHeader"
            assert asked[0]["method"] == "Network.enable"
            # main.js loaded; its import did not: name the failures around it, newest first, never the aborted one
            assert failures.explain("http://h/main.js") == (
                "no network error for it; failed: https://api.example/x.json (net::ERR_FAILED, CORS: "
                "MissingAllowOriginHeader); https://cdn.example/three.js (net::ERR_NAME_NOT_RESOLVED)")
            assert failures.explain("http://h/reset.png") == "net::ERR_EMPTY_RESPONSE"
            monkeypatch.setattr(netlog, "AROUND", 0.0)  # an earlier test's failures are not this one's
            assert failures.explain("http://h/main.js") is None
        finally:
            release.set()
            failures.stop()
            server.shutdown()
    assert not failures.is_alive()
    assert netlog.start(None) is None  # no page link (Godot, mobile): no log


def test_the_network_log_stays_bounded_on_a_page_that_polls_a_dead_endpoint():
    # Orchestrator's review of #24: a long run on a page that keeps polling must not grow the log without end.
    log = netlog.Failures("ws://127.0.0.1:9/unused")  # never started: take() is fed directly

    def fail(n, url):
        log.take({"method": "Network.requestWillBeSent", "params": {"requestId": str(n), "request": {"url": url}}})
        log.take({"method": "Network.loadingFailed", "params": {"requestId": str(n), "errorText": "net::ERR_FAILED"}})

    for n in range(1000):  # the same dead endpoint, polled again and again: one entry, its latest failure
        fail(n, "http://h/api/poll")
    assert list(log.failed) == ["http://h/api/poll"] and not log.urls
    for n in range(netlog.KEEP + 50):  # every request a new address (a cache-busting query)
        fail(10_000 + n, f"http://h/api/poll?t={n}")
    assert len(log.failed) == netlog.KEEP  # the oldest are let go
    assert "http://h/api/poll" not in log.failed and f"http://h/api/poll?t={netlog.KEEP + 49}" in log.failed
    for n in range(netlog.IDS + 10):  # requests that never fail: their ids are let go too
        log.take({"method": "Network.requestWillBeSent", "params": {"requestId": f"ok{n}", "request": {"url": "http://h/"}}})
    assert len(log.urls) == netlog.IDS
