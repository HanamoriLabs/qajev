"""A virtual gamepad for `pad` hooks (web suites) and `pad:` steps (Electron games): press buttons, push sticks, pull
triggers, with no real device (Orchestrator, 7 Oct: I'M HIM supports a controller and key rebinding, but no plan
could test it).

Chrome's DevTools protocol has key, mouse and touch input, but no gamepad. So a script in the page replaces
navigator.getGamepads with one virtual pad: the W3C standard mapping, connected, a real id, and a timestamp that goes
up on every change (I'M HIM's pickPad reads only a connected, standard-mapped pad, the one touched last). The pad is
one live object: a game that keeps it from load or from gamepadconnected reads the same buttons QAJev sets. Only games
that read the Web Gamepad API see it (not Steam Input, SDL or a native module).
"""

import json

MAX_HOLD_MS = 5000
MIN_HOLD_MS = 34  # two frames at 60 fps: a game that polls once a frame sees every press
DEFAULT_HOLD_MS = 80
MAX_REPEAT = 200
MAX_INTERVAL_MS = 2000
MAX_BUTTONS = 20
MAX_TOTAL_MS = 30_000

# The W3C standard mapping: button name -> index. Home (16) is refused: it opens Steam's or the system's overlay.
BUTTONS = {"A": 0, "B": 1, "X": 2, "Y": 3, "LB": 4, "RB": 5, "LT": 6, "RT": 7, "View": 8, "Menu": 9, "LS": 10,
           "RS": 11, "DpadUp": 12, "DpadDown": 13, "DpadLeft": 14, "DpadRight": 15}
STICKS = {"left": (0, 1), "right": (2, 3)}  # stick -> (x axis, y axis); y is +1 down, as the standard mapping says
TRIGGERS = {"LT": 6, "RT": 7}
PAD_ID = "QAJev virtual pad (STANDARD GAMEPAD Vendor: 045e Product: 028e)"


def button(name):
    """A button name -> its index (case-insensitive). ValueError for Home and for anything else."""
    if isinstance(name, str) and name.lower() == "home":
        raise ValueError("refused: Home opens Steam's or the system's overlay")
    for known, index in BUTTONS.items():
        if isinstance(name, str) and name.lower() == known.lower():
            return index
    raise ValueError(f"unknown pad button {name!r}: one of {', '.join(BUTTONS)}")


def _number(value, name, low, top, whole=True):
    n = value
    ok = not isinstance(n, bool) and (isinstance(n, int) if whole else isinstance(n, (int, float)))
    if not ok or not low <= n <= top:
        raise ValueError(f"{name} must be a {'whole ' if whole else ''}number from {low} to {top}")
    return n


def plan(value):
    """A pad hook's or step's value -> the frames to set, each {"buttons": {index: value}, "axes": {index: value},
    "ms": how long it holds}; the last frame lets everything go. `pad: A` presses A once; `pad: {press: [A, DpadDown],
    repeat, interval_ms, hold_ms}` presses a sequence; `pad: {stick: left, x, y, hold_ms}` pushes a stick then centres
    it; `pad: {trigger: RT, value, hold_ms}` pulls a trigger. ValueError when it is malformed or past its bounds."""
    if not isinstance(value, dict):
        value = {"press": value}
    kinds = [k for k in ("press", "stick", "trigger") if k in value]
    if len(kinds) != 1:
        raise ValueError("a pad input is one of press, stick or trigger")
    kind = kinds[0]
    allowed = {"press": {"press", "repeat", "interval_ms", "hold_ms"}, "stick": {"stick", "x", "y", "hold_ms"},
               "trigger": {"trigger", "value", "hold_ms"}}[kind]
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unknown pad option(s) {sorted(unknown)} for {kind}: {', '.join(sorted(allowed))}")
    hold = _number(value.get("hold_ms", DEFAULT_HOLD_MS), "hold_ms", MIN_HOLD_MS, MAX_HOLD_MS)
    if kind == "stick":
        if value["stick"] not in STICKS:
            raise ValueError(f"stick is left or right, not {value['stick']!r}")
        ax, ay = STICKS[value["stick"]]
        x = _number(value.get("x", 0), "x", -1, 1, whole=False)
        y = _number(value.get("y", 0), "y", -1, 1, whole=False)
        frames = [{"buttons": {}, "axes": {ax: x, ay: y}, "ms": hold}]
    elif kind == "trigger":
        if value["trigger"] not in TRIGGERS:
            raise ValueError(f"trigger is LT or RT, not {value['trigger']!r}")
        v = _number(value.get("value", 1), "value", 0, 1, whole=False)
        frames = [{"buttons": {TRIGGERS[value["trigger"]]: v}, "axes": {}, "ms": hold}]
    else:
        names = value["press"] if isinstance(value["press"], list) else [value["press"]]
        if not 1 <= len(names) <= MAX_BUTTONS:
            raise ValueError(f"press takes 1 to {MAX_BUTTONS} buttons")
        indexes = [button(n) for n in names]
        repeat = _number(value.get("repeat", 1), "repeat", 1, MAX_REPEAT)
        gap = _number(value.get("interval_ms", 0), "interval_ms", 0, MAX_INTERVAL_MS)
        frames = []
        for _ in range(repeat):
            for i in indexes:
                frames.append({"buttons": {i: 1}, "axes": {}, "ms": hold})
                # let go between presses, long enough to be seen: the same button twice is two presses, not one hold
                frames.append({"buttons": {}, "axes": {}, "ms": max(gap, MIN_HOLD_MS)})
        frames.pop()
    frames.append({"buttons": {}, "axes": {}, "ms": 0})  # let everything go
    total = sum(f["ms"] for f in frames)
    if total > MAX_TOTAL_MS:
        raise ValueError(f"one pad input may take at most {MAX_TOTAL_MS // 1000} s (this one: {total / 1000:.1f} s)")
    return frames


def describe(value):
    """A pad input in plain words, for a step's check: "pressed A, DpadDown on the virtual pad"."""
    if not isinstance(value, dict):
        value = {"press": value}
    if "stick" in value:
        return f"pushed the {value['stick']} stick to ({value.get('x', 0)}, {value.get('y', 0)}) on the virtual pad"
    if "trigger" in value:
        return f"pulled {value['trigger']} to {value.get('value', 1)} on the virtual pad"
    names = value["press"] if isinstance(value["press"], list) else [value["press"]]
    times = f" {value['repeat']} times" if value.get("repeat", 1) > 1 else ""
    return f"pressed {', '.join(map(str, names))}{times} on the virtual pad"


def set_js(frame):
    """The page expression that puts one frame on the virtual pad. -> its change count (the pad must be there)."""
    state = {"buttons": {str(k): v for k, v in frame["buttons"].items()},
             "axes": {str(k): v for k, v in frame["axes"].items()}}
    return f"window.__qajevPad ? window.__qajevPad.set({json.dumps(state)}) : (() => {{ throw new Error(" \
           f"'no virtual pad in this page') }})()"


# Runs before the page's own code (Page.addScriptToEvaluateOnNewDocument), and again in a page that already loaded.
SHIM_JS = """
(() => {
  if (window.__qajevPad) return;
  const buttons = [];
  for (let i = 0; i < 17; i++) buttons.push({ pressed: false, touched: false, value: 0 });
  const pad = {
    id: %(id)s, index: 0, connected: true, mapping: 'standard',
    timestamp: (window.performance ? performance.now() : 0), buttons, axes: [0, 0, 0, 0], vibrationActuator: null,
  };
  let changes = 0;
  const list = () => [pad, null, null, null];
  try {
    Object.defineProperty(navigator, 'getGamepads', { value: list, configurable: true, writable: true });
  } catch (e) {
    navigator.getGamepads = list;
  }
  window.__qajevPad = {
    pad,
    set(state) {
      for (let i = 0; i < buttons.length; i++) {
        const v = Number((state.buttons || {})[i] || 0);
        buttons[i].value = v; buttons[i].pressed = v > 0; buttons[i].touched = v > 0;
      }
      for (let i = 0; i < 4; i++) pad.axes[i] = Number((state.axes || {})[i] || 0);
      const now = window.performance ? performance.now() : Date.now();
      pad.timestamp = Math.max(now, pad.timestamp + 0.001);  // always up, so pickPad takes the pad touched last
      return ++changes;
    },
    get changes() { return changes; },
  };
  const announce = () => {
    const ev = new Event('gamepadconnected');
    Object.defineProperty(ev, 'gamepad', { value: pad });
    window.dispatchEvent(ev);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', announce, { once: true });
  else setTimeout(announce, 0);
})();
""" % {"id": json.dumps(PAD_ID)}
