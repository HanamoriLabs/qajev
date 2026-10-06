"""Real key presses for `key` hooks: which keys, and the plan for pressing them (once, a sequence repeated, held).

Real-key play-tests are the rule for every game change (Orchestrator, 5 Oct): Jev can click and type but not press a
game's keys, so a suite does it with hooks. The keys are an allow-list of plain key names, plus chords of them with
Shift, Ctrl, Alt or Meta ("Shift+A": verse1, 6 Oct). A chord never names a browser or system shortcut with Ctrl or
Meta (quit, close, reload, a new tab or window, the address bar, print). Each press is a trusted key event in the
page (Chrome's Input.dispatchKeyEvent, with the chord's modifiers bitmask): keyDown, then keyUp after `hold_ms`.
"""

MAX_HOLD_MS = 5000
MAX_REPEAT = 200
MAX_INTERVAL_MS = 2000
MAX_KEYS = 20
MAX_TOTAL_MS = 30_000

# name -> (key, code, windowsVirtualKeyCode, text typed or None)
_NAMED = {
    "Escape": ("Escape", "Escape", 27, None),
    "Enter": ("Enter", "Enter", 13, "\r"),
    "Tab": ("Tab", "Tab", 9, None),
    "Space": (" ", "Space", 32, " "),
    "Backspace": ("Backspace", "Backspace", 8, None),
    "Delete": ("Delete", "Delete", 46, None),  # on production the guard keeps it (and Backspace) in text fields
    "ArrowLeft": ("ArrowLeft", "ArrowLeft", 37, None),
    "ArrowUp": ("ArrowUp", "ArrowUp", 38, None),
    "ArrowRight": ("ArrowRight", "ArrowRight", 39, None),
    "ArrowDown": ("ArrowDown", "ArrowDown", 40, None),
}
_PUNCTUATION = {
    "`": ("Backquote", 192), "-": ("Minus", 189), "=": ("Equal", 187), "[": ("BracketLeft", 219),
    "]": ("BracketRight", 221), ";": ("Semicolon", 186), "'": ("Quote", 222), ",": ("Comma", 188),
    ".": ("Period", 190), "/": ("Slash", 191), "\\": ("Backslash", 220),
}


def _table():
    keys = {name: {"key": k, "code": c, "windowsVirtualKeyCode": vk, **({"text": t} if t else {})}
            for name, (k, c, vk, t) in _NAMED.items()}
    for ch in "abcdefghijklmnopqrstuvwxyz":
        keys[ch] = {"key": ch, "code": f"Key{ch.upper()}", "windowsVirtualKeyCode": ord(ch.upper()), "text": ch}
    for d in "0123456789":
        keys[d] = {"key": d, "code": f"Digit{d}", "windowsVirtualKeyCode": ord(d), "text": d}
    for ch, (code, vk) in _PUNCTUATION.items():
        keys[ch] = {"key": ch, "code": code, "windowsVirtualKeyCode": vk, "text": ch}
    # a key's code works as its name too ("KeyF", "Digit1", "Backquote"): easier to write in YAML than "`"
    keys.update({spec["code"]: spec for spec in list(keys.values())})
    return keys


KEYS = _table()


# CDP's modifiers bitmask, and the keys that, with Ctrl or Meta, are a browser's or the system's own shortcuts
MODIFIERS = {"alt": 1, "ctrl": 2, "control": 2, "meta": 4, "cmd": 4, "shift": 8}
SHORTCUTS = set("qwrtnlp")


def spec(name):
    """The key event fields for a key name, or a chord ("Shift+A", "Ctrl+Shift+KeyK"): case-insensitive for letters
    and modifiers. ValueError for anything else, and for a browser or system shortcut."""
    if isinstance(name, str) and "+" in name and name != "+":
        *mods, base = name.split("+")
        bits = 0
        for m in mods:
            bit = MODIFIERS.get(m.strip().lower())
            if not bit or bits & bit:
                raise ValueError(f"{name!r}: a chord is Shift, Ctrl, Alt or Meta (each once) + one plain key")
            bits |= bit
        out = dict(_plain(base))
        if bits & (2 | 4) and out["key"].lower() in SHORTCUTS:
            raise ValueError(f"refused: {name!r} is a browser or system shortcut (quit, close, reload, a new tab or "
                             "window, the address bar, print)")
        if bits & 8 and len(out["key"]) == 1 and out["key"].isalpha():
            out.update(key=out["key"].upper(), text=out["key"].upper())
        if bits & (1 | 2 | 4):
            out.pop("text", None)  # Ctrl+K types nothing
        return {**out, "modifiers": bits}
    return _plain(name)


def _plain(name):
    if isinstance(name, str) and len(name) == 1 and name.isalpha():
        name = name.lower()
    if not isinstance(name, str) or name not in KEYS:
        raise ValueError(f"unknown key {name!r}: one plain key, a letter, a digit, punctuation such as '`', or "
                         f"{', '.join(_NAMED)}; or a chord such as Shift+A")
    return KEYS[name]


def plan(value):
    """A key hook's value -> {keys, repeat, interval_ms, hold_ms}. `key: f` presses f once; `key: {press: [f, j],
    repeat: 15, interval_ms: 30}` presses f then j, 15 times; `hold_ms` keeps each key down that long. ValueError
    when it is malformed or past its bounds."""
    if not isinstance(value, dict):
        value = {"press": value}
    unknown = set(value) - {"press", "repeat", "interval_ms", "hold_ms"}
    if unknown:
        raise ValueError(f"unknown key option(s) {sorted(unknown)}: press, repeat, interval_ms, hold_ms")
    keys = value.get("press")
    keys = keys if isinstance(keys, list) else [keys]
    if not 1 <= len(keys) <= MAX_KEYS:
        raise ValueError(f"press takes 1 to {MAX_KEYS} keys")
    for k in keys:
        spec(k)

    def number(name, default, top, low=0):
        n = value.get(name, default)
        if isinstance(n, bool) or not isinstance(n, int) or not low <= n <= top:
            raise ValueError(f"{name} must be a whole number from {low} to {top}")
        return n

    out = {"keys": keys, "repeat": number("repeat", 1, MAX_REPEAT, low=1),
           "interval_ms": number("interval_ms", 0, MAX_INTERVAL_MS),
           "hold_ms": number("hold_ms", 0, MAX_HOLD_MS)}
    total = len(keys) * out["repeat"] * (out["hold_ms"] + out["interval_ms"])
    if total > MAX_TOTAL_MS:
        raise ValueError(f"one key hook may take at most {MAX_TOTAL_MS // 1000} s (this one: {total / 1000:.1f} s)")
    return out


MAX_REACT_S = 180
MAX_ACTIONS = 10
# Frames a react policy may ask for: per tick and per hook (each is a screenshot; a policy asking every tick would
# otherwise take thousands).
MAX_SHOTS_TICK = 2
MAX_SHOTS_HOOK = 60


def react_plan(value):
    """A react hook's value -> {js, every_ms, for_s, until}. The policy `js` runs in the page every `every_ms` and
    returns key actions: a key name or {press: k} (down and up), {down: k}, {up: k}, a list of them, or nothing.
    `until` ends the hook when it holds; without it the hook runs for `for_s`. ValueError when malformed."""
    if not isinstance(value, dict) or not isinstance(value.get("js"), str) or not value["js"].strip():
        raise ValueError("react needs js: a policy expression that returns key actions")
    unknown = set(value) - {"js", "every_ms", "for_s", "until"}
    if unknown:
        raise ValueError(f"unknown react option(s) {sorted(unknown)}: js, every_ms, for_s, until")
    every = value.get("every_ms", 50)
    if isinstance(every, bool) or not isinstance(every, int) or not 20 <= every <= 1000:
        raise ValueError("every_ms must be a whole number from 20 to 1000")
    for_s = value.get("for_s", 30)
    if isinstance(for_s, bool) or not isinstance(for_s, (int, float)) or not 0 < for_s <= MAX_REACT_S:
        raise ValueError(f"for_s must be more than 0 and at most {MAX_REACT_S}")
    until = value.get("until")
    if until is not None and not isinstance(until, str):
        raise ValueError("until must be a JavaScript expression")
    return {"js": value["js"], "every_ms": every, "for_s": float(for_s), "until": until}


def actions(value):
    """A policy's answer -> [(what, key)], what in press/down/up; every key checked against the allow-list."""
    items = [] if value is None else value if isinstance(value, list) else [value]
    if len(items) > MAX_ACTIONS:
        raise ValueError(f"a policy returned {len(items)} actions at once (at most {MAX_ACTIONS})")
    out = []
    for item in items:
        if isinstance(item, str):
            item = {"press": item}
        if not isinstance(item, dict) or len(item) != 1 or next(iter(item)) not in {"press", "down", "up"}:
            raise ValueError(f"a key action is a key name, {{press: k}}, {{down: k}} or {{up: k}}, not {item!r}")
        what, name = next(iter(item.items()))
        spec(name)
        out.append((what, name.lower() if isinstance(name, str) and len(name) == 1 else name))
    return out
