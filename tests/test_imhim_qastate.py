"""The imhim adapter reports the game's own QA flags (window.__qaState) as state fields, so a play step can stop on
one (`until: {kaigun_beaten: true}`). Plain values only, at most 50 keys and 200 characters a value, and never over
the adapter's own fields. The adapter runs in Node with a stand-in page, as it runs in the game's page."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from qajev import native

ADAPTER = Path(__file__).resolve().parent.parent / "qajev" / "bridges" / "web" / "adapters" / "imhim.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="needs node to run the adapter")

PAGE = """
global.window = global;
global.document = { querySelector: () => null, querySelectorAll: () => [],
                    documentElement: { classList: { contains: () => false } }, body: null };
global.localStorage = { getItem: () => null };
global.location = { href: 'http://localhost/' };
require(%s);
const look = () => window.__qajevAdapter.observe({ actions: [], texts: [] }).state;
"""


def _looks(script):
    """The adapter's state at each `looks.push(look())` in `script`."""
    assert NODE
    code = PAGE % json.dumps(str(ADAPTER)) + "const looks = [];\n" + script + "\nconsole.log(JSON.stringify(looks));"
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_qa_flag_appears_as_a_state_field():
    [before, after] = _looks("looks.push(look()); window.__qaState = { kaigun_beaten: false, chapter: 3 };"
                             "looks.push(look());")
    assert "kaigun_beaten" not in before
    assert after["kaigun_beaten"] is False and after["chapter"] == 3


def test_only_plain_values_within_bounds_and_never_the_adapters_own_fields():
    [state] = _looks("""
      const flags = { screen: 'HACKED', overlay: true, setting_subtitles: 'off', long: 'x'.repeat(201),
                      fits: 'y'.repeat(200), obj: { a: 1 }, list: [1], nan: NaN, none: null };
      for (let i = 0; i < 60; i++) flags['f' + i] = i;
      window.__qaState = flags;
      looks.push(look());
    """)
    assert state["screen"] == "LOADING"  # the adapter's own screen, not the flag's
    assert "overlay" not in state and "setting_subtitles" not in state  # own fields, even when this look set none
    assert "long" not in state and state["fits"] == "y" * 200  # an oversized value is dropped
    assert not {"obj", "list", "nan", "none"} & set(state)
    assert len([k for k in state if k == "fits" or k.startswith("f") and k[1:].isdigit()]) == 50


class Replay:
    """A game whose looks are the adapter's states, in order (the last one repeats)."""

    errors = []

    def __init__(self, states):
        self.states, self.n = states, 0

    def call(self, **_):
        return {"ok": True, "pilot": True}

    def observe(self):
        state = self.states[min(self.n, len(self.states) - 1)]
        self.n += 1
        return {"screen": state["screen"], "fps": 60, "perf": {"frame_ms": 2.0, "memory_mb": 50.0}, "texts": [],
                "state": state, "actions": []}

    def act(self, _action):
        pass

    def shot(self, _path):
        return None


def test_a_play_step_stops_when_a_qa_flag_flips():
    states = _looks("""
      window.__qaState = { kaigun_beaten: false };
      looks.push(look()); looks.push(look());
      window.__qaState.kaigun_beaten = true;
      looks.push(look());
    """)
    game = Replay(states)
    r = native.play_for(game, name="beat Kaigun", seconds=30, sample=0.01, shots=False,
                        until={"kaigun_beaten": True})
    assert r["stop"] == "reached" and r["outcome"] == "pass"
    assert {"check": "reached kaigun_beaten = true", "ok": True, "detail": None} in r["checks"]
    assert game.n <= 4  # it stopped at the flip, not at the 30 s cap
