"""The virtual gamepad: what a `pad` hook or step sends, what QAJev refuses, and what a game reading the Web Gamepad
API sees (the page script runs in Node with a stand-in window, the way it runs before a page's own code)."""

import json
import shutil
import subprocess

import pytest

from qajev import native, pad, plan
from qajev.suite import SuiteError, load


def test_a_press_holds_each_button_then_lets_go():
    frames = pad.plan({"press": ["A", "dpaddown"], "repeat": 2, "interval_ms": 50, "hold_ms": 60})
    pressed = [f["buttons"] for f in frames]
    assert pressed == [{0: 1}, {}, {13: 1}, {}, {0: 1}, {}, {13: 1}, {}]  # the same button twice is two presses
    assert [f["ms"] for f in frames] == [60, 50, 60, 50, 60, 50, 60, 0]
    assert pad.plan("B") == [{"buttons": {1: 1}, "axes": {}, "ms": 80}, {"buttons": {}, "axes": {}, "ms": 0}]


def test_a_stick_and_a_trigger_hold_then_centre():
    stick = pad.plan({"stick": "left", "x": 1, "y": -0.5, "hold_ms": 500})
    assert stick == [{"buttons": {}, "axes": {0: 1, 1: -0.5}, "ms": 500}, {"buttons": {}, "axes": {}, "ms": 0}]
    assert pad.plan({"trigger": "RT", "value": 0.7})[0]["buttons"] == {7: 0.7}


@pytest.mark.parametrize("value, says", [
    ("Home", "refused: Home opens"),
    ("Start", "unknown pad button 'Start'"),
    ({"press": "A", "hold_ms": 10}, "hold_ms must be a whole number from 34"),
    ({"press": "A", "repeat": 201}, "repeat must be"),
    ({"stick": "left", "x": 2}, "x must be a number from -1 to 1"),
    ({"stick": "middle"}, "stick is left or right"),
    ({"trigger": "A"}, "trigger is LT or RT"),
    ({"press": "A", "stick": "left"}, "one of press, stick or trigger"),
    ({"press": "A", "for": 3}, "unknown pad option"),
    ({"press": "A", "repeat": 200, "hold_ms": 5000}, "at most 30 s"),
])
def test_a_bad_pad_input_is_refused(value, says):
    with pytest.raises(ValueError, match=says):
        pad.plan(value)


def test_suites_and_game_steps_refuse_a_bad_pad_before_the_run(tmp_path):
    (tmp_path / "s.yaml").write_text("name: s\nscenarios:\n  - name: t\n    about: x\n    url: http://localhost:1/\n"
                                     "    before: [{pad: Home}]\n    expect: {text: [x]}\n")
    with pytest.raises(SuiteError, match=r"before\[0\]\.pad: refused: Home"):
        load(tmp_path / "s.yaml")
    with pytest.raises(native.NativeError, match="step 'jump': pad: unknown pad button"):
        native.check_pad([{"name": "jump", "pad": "Jump"}])


def test_the_plan_says_what_a_pad_step_sends():
    [it] = plan.from_steps([{"name": "jump", "about": "A jumps.", "pad": {"press": "A", "repeat": 2},
                             "expect": {"screen": "JUMP"}}])
    assert [line["words"] for line in it["checks"]][:2] == ["pressed A 2 times on the virtual pad",
                                                             "the screen is JUMP"]


class Game:
    """An Electron game as pad_step sees it: the pad goes in once, each frame is a page script."""

    errors = []

    def __init__(self):
        self.pads, self.scripts = 0, []

    def use_pad(self):
        self.pads += 1

    def run_js(self, expression):
        self.scripts.append(expression)
        return len(self.scripts)

    def observe(self):
        return {"screen": "JUMP", "texts": [], "actions": [], "state": {}}

    def shot(self, _path):
        return None


class _NoLedger:
    def spent(self):
        return 0.0


def test_a_pad_step_sends_its_frames_then_runs_its_checks(monkeypatch):
    monkeypatch.setattr(native.time, "sleep", lambda s: None)
    game = Game()
    r = native.pad_step(game, name="jump", value="A", expect={"screen": "JUMP"}, ledger=_NoLedger())
    assert game.pads == 1 and len(game.scripts) == 2  # A down, then everything up
    assert '"0": 1' in game.scripts[0] and '"buttons": {}' in game.scripts[1]
    assert r["outcome"] == "pass" and r["checks"][0]["says"] == "pressed A on the virtual pad"
    no_page = native.pad_step(object(), name="jump", value="A")
    assert no_page["outcome"] == "harness" and "need an Electron app" in no_page["reason"]


NODE = shutil.which("node")

# A stand-in for the browser: enough of window, navigator and document for the pad script and a game's reads.
STAGE = """
const listeners = {};
global.window = global;
global.performance = { now: (() => { let t = 0; return () => (t += 16); })() };
global.navigator = {};
const listen = (k, f) => { (listeners[k] = listeners[k] || []).push(f); };
global.document = { readyState: 'loading', addEventListener: listen };
global.Event = class { constructor(type) { this.type = type; } };
global.dispatchEvent = (e) => (listeners[e.type] || []).forEach((f) => f(e));
global.addEventListener = listen;
global.setTimeout = (f) => f();
"""

# I'M HIM's pickPad (src/core/pads.ts): only a connected, standard-mapped pad, the one touched last.
GAME = """
function pickPad(pads) {
  let best = null;
  for (const p of pads) {
    if (!p || !p.connected || p.mapping !== 'standard') continue;
    if (!best || p.timestamp > best.timestamp) best = p;
  }
  return best;
}
"""


def _node(script):
    assert NODE
    out = subprocess.run([NODE, "-e", STAGE + pad.SHIM_JS + GAME + script], capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(not NODE, reason="needs node to run the page script")
def test_a_game_picks_the_virtual_pad_and_sees_each_press():
    frames = [pad.set_js(f) for f in pad.plan({"press": "A"})]
    seen = _node(f"""
      const connected = []; addEventListener('gamepadconnected', (e) => connected.push(e.gamepad.id));
      listeners.DOMContentLoaded.forEach((f) => f());
      const p = pickPad(navigator.getGamepads());
      const t0 = p.timestamp;
      const down = {frames[0]}; const pressed = pickPad(navigator.getGamepads()).buttons[0].pressed;
      const t1 = p.timestamp;
      const up = {frames[1]}; const released = !pickPad(navigator.getGamepads()).buttons[0].pressed;
      console.log(JSON.stringify({{id: p.id, mapping: p.mapping, connected: p.connected, pressed, released,
                                  rising: t1 > t0 && p.timestamp > t1, changes: up, events: connected}}));
    """)
    assert seen == {"id": pad.PAD_ID, "mapping": "standard", "connected": True, "pressed": True, "released": True,
                    "rising": True, "changes": 2, "events": [pad.PAD_ID]}


@pytest.mark.skipif(not NODE, reason="needs node to run the page script")
def test_a_game_that_reads_its_pad_only_once_at_load_still_sees_the_input():
    # The game keeps the pad it found at load and never asks again: the pad is one live object.
    frames = [pad.set_js(f) for f in pad.plan({"stick": "left", "x": 1, "hold_ms": 100})]
    seen = _node(f"""
      const atLoad = navigator.getGamepads()[0];
      {frames[0]};
      const pushed = atLoad.axes[0];
      {frames[1]};
      console.log(JSON.stringify({{kept: atLoad !== null, pushed, centred: atLoad.axes[0]}}));
    """)
    assert seen == {"kept": True, "pushed": 1, "centred": 0}
