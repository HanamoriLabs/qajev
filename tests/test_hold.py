"""A page that never stops moving (the rig tool: "217 fps" became "201 fps" before every move) made each of Jev's
decisions stale. After the first stale move, QAJev holds the page's animation frames while Jev looks and chooses, and
releases them once it has acted. Network messages and timers run on, so a multiplayer page keeps its feed."""

import json
import shutil
import subprocess

import pytest

from qajev import report_html, session
from qajev.session import Session


class Stale(Exception):
    pass


class Agent:
    """Jev's agent: each `act` is stale or not, from a script."""

    def __init__(self, stale_acts, log):
        self.stale_acts, self.log = list(stale_acts), log
        self.state = {"decisions": [], "page": {"fingerprint": "f"}, "status": "ready", "decision": None}

    def command(self, name, args=None):
        self.log.append(name)
        if name == "predict":
            self.state["decisions"].append({"choice": "a1"})
        if name == "act" and self.stale_acts.pop(0):
            raise Stale("Page changed since the decision. Choose again.")


def page(stale_acts, *, act_fails=False):
    log = []
    s = Session.__new__(Session)
    s.jev = type("Jev", (), {"browser": type("B", (), {"StalePage": Stale})})()
    s.agent = Agent(stale_acts, log)
    if act_fails:
        def broken(name, args=None):
            log.append(name)
            if name == "act":
                raise RuntimeError("the click missed")
        s.agent.command = broken
    s.holding, s.held, s.held_s, s.hold_loaded, s.last_stale = False, 0, 0.0, False, None
    s.evaluate = lambda expression, timeout_ms=15000: log.append(
        "hold" if ".hold()" in expression else "release" if ".release()" in expression else "install") or True
    s.call = lambda method, **params: log.append(method)
    s.require_guard = lambda: None
    s.settle_scroll = lambda: None
    s.observe = lambda: log.append("observe")
    s.why_stale = lambda reason, page, decision: ({"reason": reason}, {}, None)
    return s, log


def test_after_a_stale_move_jev_decides_on_a_held_page(monkeypatch):
    monkeypatch.setattr(session, "page_changes", lambda *a: [])
    s, log = page([True, False])
    assert s.tick() is False and s.holding  # the page moved under Jev: hold it from now on
    assert "Page.addScriptToEvaluateOnNewDocument" in log  # in each document it loads, too
    log.clear()
    assert s.tick() is True and s.held == 1
    # held, then read again: frames drawn between the last read and the hold must not make the choice stale
    assert log == ["hold", "observe", "predict", "act", "release"]


def test_the_report_gets_how_long_the_page_was_held(monkeypatch):
    clock = iter([10.0, 10.75, 20.0, 20.5])
    monkeypatch.setattr(session.time, "monotonic", lambda: next(clock))
    s, _ = page([False, False])
    s.holding = True
    assert s.tick() and s.tick()
    assert (s.held, s.held_s) == (2, 1.25)


def test_the_report_says_how_many_decisions_were_held_and_for_how_long():
    html = report_html._scenario(1, {"name": "lock the joint", "outcome": "pass", "held_decisions": 3,
                                     "held_seconds": 1.4})
    assert "held its animation while Jev chose (3 decision(s), 1.4 s)" in html


def test_each_test_starts_unheld_and_counts_only_its_own_holds(monkeypatch):
    monkeypatch.setattr(session, "page_changes", lambda *a: [])
    s, log = page([True, False, True])
    s.agent.pending_text = None
    assert s.tick() is False and s.tick() is True and s.held == 1
    s.reset_agent("the next test's goal")  # the next scenario: held only after its own first stale move
    assert (s.holding, s.held, s.held_s) == (False, 0, 0.0)
    assert s.tick() is False and s.holding
    assert log.count("Page.addScriptToEvaluateOnNewDocument") == 1  # added once; it waits unused until a hold


def test_a_page_that_never_went_stale_is_never_held():
    s, log = page([False, False])
    assert s.tick() and s.tick()
    assert "hold" not in log and not s.holding and s.held == 0


def test_a_failed_action_still_releases_the_page():
    s, log = page([], act_fails=True)
    s.holding = True
    with pytest.raises(RuntimeError, match="the click missed"):
        s.tick()
    assert log == ["hold", "observe", "predict", "act", "release"]


NODE = shutil.which("node")

STAGE = """
let t = 0; const queue = [];
global.window = global;
global.requestAnimationFrame = (cb) => { queue.push(cb); return queue.length; };
global.cancelAnimationFrame = () => {};
global.document = { getAnimations: () => anims };
const anims = [{ playState: 'running',
                 pause() { this.playState = 'paused'; }, play() { this.playState = 'running'; } }];
const frame = () => { const run = queue.splice(0); run.forEach((cb) => cb(t += 16)); };
"""


@pytest.mark.skipif(not NODE, reason="needs node to run the page script")
def test_held_frames_wait_and_run_on_release_while_timers_and_messages_go_on():
    assert NODE
    script = STAGE + session.HOLD_JS + """;
      let drawn = 0, messages = 0;
      const loop = () => { drawn++; requestAnimationFrame(loop); };
      requestAnimationFrame(loop); frame(); frame();
      const before = drawn;
      window.__qajevHold.hold();
      frame(); frame(); frame();
      messages += 3;  // a socket's messages are events, not frames: the hold does not touch them
      const during = drawn - before, paused = anims[0].playState;
      window.__qajevHold.release();
      frame(); frame();
      console.log(JSON.stringify({ before, during, after: drawn - before - during, paused, played: anims[0].playState,
                                   messages }));
    """
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    seen = json.loads(out.stdout)
    assert seen == {"before": 2, "during": 1, "after": 2, "paused": "paused", "played": "running", "messages": 3}


@pytest.mark.skipif(not NODE, reason="needs node to run the page script")
def test_a_frame_rate_counted_across_a_hold_is_still_the_real_rate():
    # A Verse check counts frames for 5 s and fails under 30 a second. A 2 s hold in that window draws nothing, so
    # the page divides by the time it was not held: window.__qajevHold.heldMs.
    assert NODE
    script = STAGE + """
      let clock = 0;
      Object.defineProperty(globalThis, 'performance', { value: { now: () => clock }, configurable: true });
      """ + session.HOLD_JS + """;
      let drawn = 0; const loop = () => { drawn++; requestAnimationFrame(loop); }; requestAnimationFrame(loop);
      const run = (ms) => { for (let n = 0; n < ms * 60 / 1000; n++) { clock += 1000 / 60; frame(); } };
      run(1500);
      window.__qajevHold.hold(); run(2000);
      window.__qajevHold.release(); run(1500);
      const h = window.__qajevHold;
      console.log(JSON.stringify({ holds: h.held, heldMs: Math.round(h.heldMs),
                                   fps: Math.round(drawn / ((5000 - h.heldMs) / 1000)) }));
    """
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"holds": 1, "heldMs": 2000, "fps": 60}


@pytest.mark.skipif(not NODE, reason="needs node to run the page script")
def test_the_hold_answers_after_the_frame_the_page_already_asked_for():
    # A frame the page asked for before the hold still draws once. The hold answers after it, so QAJev reads a still
    # page, and no later frame draws until the release.
    assert NODE
    script = STAGE + session.HOLD_JS + """;
      let drawn = 0; const loop = () => { drawn++; requestAnimationFrame(loop); }; requestAnimationFrame(loop);
      let answered = null;
      window.__qajevHold.hold().then(() => { answered = drawn; });
      frame();
      setTimeout(() => { frame(); frame(); console.log(JSON.stringify({ answered, drawn })); }, 0);
    """
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"answered": 1, "drawn": 1}
