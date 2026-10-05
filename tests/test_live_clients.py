"""Multiplayer scenarios (clients.py) end to end: `qajev run` drives five isolated clients in QAJev's own throwaway
headless Chrome against a local WebSocket room. One scenario passes; in another the room withholds the chat from one
player, and the across check must fail on it. No model calls.

QAJEV_LIVE=1 to run (starts a local Chrome). Its own file: `qajev run` stops the browser daemon a run used.
"""

import http.server
import json
import os
import subprocess
import sys
import threading
from functools import partial
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("QAJEV_LIVE") != "1", reason="set QAJEV_LIVE=1 (starts a local Chrome)")

SITE = Path(__file__).parent / "fixtures" / "site"


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


@pytest.fixture(scope="module")
def site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(SITE)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def room():
    """A room server: players join with ?room&player, everyone gets the roster and every chat (in order, with a
    sequence number), except a player named by the room's ?drop, who never gets a chat."""
    from websockets.sync.server import serve

    rooms, lock = {}, threading.Lock()

    def send(ws, message):
        try:
            ws.send(json.dumps(message))
        except Exception:  # noqa: BLE001 (a player that left)
            pass

    def handler(ws):
        q = {k: v[0] for k, v in parse_qs(urlsplit(ws.request.path).query).items()}
        name, player = q["room"], q["player"]
        with lock:
            r = rooms.setdefault(name, {"players": {}, "seq": 0, "drop": q.get("drop") or ""})
            r["players"][player] = ws
            roster = sorted(r["players"])
            everyone = list(r["players"].values())
        for other in everyone:
            send(other, {"type": "roster", "players": roster})
        try:
            for raw in ws:
                m = json.loads(raw)
                if m.get("type") != "chat":
                    continue
                with lock:
                    r["seq"] += 1
                    out = {"type": "chat", "from": player, "text": m["text"], "seq": r["seq"]}
                    targets = [(p, w) for p, w in r["players"].items()]
                for p, w in targets:
                    if r["drop"] and p.startswith(r["drop"] + "-"):
                        continue  # this player misses every chat: the failure the across check must catch
                    send(w, out)
        finally:
            with lock:
                if r["players"].get(player) is ws:
                    del r["players"][player]
                roster = sorted(r["players"])
                everyone = list(r["players"].values())
            for other in everyone:
                send(other, {"type": "roster", "players": roster})

    with serve(handler, "127.0.0.1", 0) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        yield server.socket.getsockname()[1]
        server.shutdown()


@pytest.fixture(scope="module")
def browser():
    from qajev import chrome

    record = chrome.start(f"selftest-clients-{os.getpid()}", headless=True, ephemeral=True)
    yield record
    chrome.stop(record["state_key"])
    assert not Path(record["profile_dir"]).exists(), "ephemeral profile must be deleted"


SUITE = """
name: multiplayer
device: desktop
settle: 3
scenarios:
  - name: five players share one room
    about: five isolated players join one room, see the same roster and every chat once, in order; one rejoins
    url: {site}/mp.html?room=qa-{{run}}&player={{client}}&ws={ws}
    clients: 5
    state: "({{id: game.id, players: game.players, chat: game.chat.map(m => m.seq + ':' + m.text)}})"
    steps:
      - all: {{wait_for: {{js: "game.ready && game.players.length === 5", timeout: 15}}}}
      - all: {{js: "game.say('hello from ' + game.id)"}}
        stagger: 100
      - snapshot: chat
        until: "game.chat.length >= 5"
        timeout: 5
        expect:
          - check: every player got the same five chats, in order
            js: >-
              clients.every(c => c.state && c.state.chat.length === 5
              && JSON.stringify(c.state.chat) === JSON.stringify(clients[0].state.chat))
      - client: p3
        reload: true
      - client: p3
        wait_for: {{js: "game.ready && game.players.length === 5", timeout: 15}}
    expect:
      text: ["Room qa-"]
      across:
        - check: five players, each in its own browser context
          js: "new Set(clients.map(c => c.state.id)).size === 5"
        - check: everyone sees the same roster
          js: "clients.every(c => JSON.stringify(c.state.players) === JSON.stringify(clients[0].state.players))"
        - check: p3 rejoined as the same player
          js: "clients.find(c => c.name === 'p3').state.id === snapshots.chat.find(c => c.name === 'p3').state.id"

  - name: a player misses the chat
    about: the room withholds every chat from p4; the across check must fail on it
    url: {site}/mp.html?room=qa-{{run}}&player={{client}}&ws={ws}&drop=p4
    clients: 5
    state: "({{id: game.id, chat: game.chat.map(m => m.seq + ':' + m.text)}})"
    steps:
      - all: {{wait_for: {{js: "game.ready && game.players.length === 5", timeout: 15}}}}
      - all: {{js: "game.say('hi')"}}
        jitter: 50
      - snapshot: chat
        until: "game.chat.length >= 5"
        timeout: 3
        expect:
          - check: every player got all five chats
            js: "clients.every(c => c.state && c.state.chat.length === 5)"
"""


def test_five_isolated_players_and_a_missed_message(site, room, browser, tmp_path):
    suite = tmp_path / "mp.qajev.yaml"
    suite.write_text(SUITE.format(site=site, ws=room))
    p = subprocess.run([sys.executable, "-m", "qajev", "run", str(suite), "--cdp-url", browser["cdp_url"], "--out",
                        str(tmp_path / "runs"), "--json"], capture_output=True, text=True, timeout=240)
    report = json.loads(p.stdout)
    by = {s["name"]: s for s in report["scenarios"]}
    ok, missed = by["five players share one room"], by["a player misses the chat"]

    assert ok["outcome"] == "pass", (ok["reason"], ok.get("checks"))
    names = {c["check"] for c in ok["checks"]}
    assert "snapshot chat: every player got the same five chats, in order" in names
    assert {"across: five players, each in its own browser context", "across: everyone sees the same roster",
            "across: p3 rejoined as the same player"} <= names
    assert {f"[p{i}] page shows 'Room qa-'" for i in range(1, 6)} <= names
    assert [c["name"] for c in ok["clients"]] == ["p1", "p2", "p3", "p4", "p5"]
    assert all(c["shot"] for c in ok["clients"])  # a screenshot per client
    starts = next(s["starts_ms"] for s in ok["steps"] if s.get("do") == "js")
    assert starts == [0, 100, 200, 300, 400]  # staggered by 100 ms

    assert missed["outcome"] == "fail", missed["reason"]
    assert "snapshot chat: every player got all five chats" in missed["reason"]
    late = next(s for s in missed["steps"] if s.get("snapshot") == "chat")
    assert late["timed_out"] == ["p4"]  # p4 never saw the chats arrive
    p4 = next(r for r in missed["snapshots"]["chat"] if r["name"] == "p4")
    assert json.loads(p4["state"])["chat"] == []
