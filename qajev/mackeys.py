"""Mac keys: real key presses on the Mac itself, for a native app's paste, shortcuts and typing (Twinbox, 14 Oct).

A play suite's `keys:` step:

    - name: Paste a box name
      about: A name copied elsewhere pastes into the new box's name field with a real Cmd+V.
      keys:
        app: com.hanamorilabs.twinbox      # the app that gets the keys (its bundle id)
        clipboard: qa-paste-7f3            # first, put this text on the clipboard (optional)
        click: new-box.name                # then a real click on this control of the app (optional)
        press: [cmd+v]                     # keys and chords in order; {type: "some text"} types it key by key
      expect:
        state: {new_box_name: qa-paste-7f3}

Input pushed into an app through its bridge skips macOS, so it cannot prove a paste, a shortcut or focus. These keys
are macOS key events from one small helper, qajev-keys, which a QAJev Cloud per-run Mac builds and trusts for
Accessibility (qajev-cloud's qcloud/keys.py: one TCC row). The step runs only where that helper is found and trusted.
Anywhere else, this Mac too, it presses nothing and stops as harness. The helper presses each key only while `app` is
the frontmost app, and stops at the first key it cannot send there. The suite is refused when it loads if it names the
system's own shortcuts (quit, close, hide, minimize, switch apps or Spaces, Spotlight, force quit) or a system app.
Key codes are a US keyboard's, as the cloud Macs have.

`click` names one of the app's actions (its bridge id). QAJev reads that action's x and y (egui points from the
drawing area's top-left) and the app's `state.window` {x, y, zoom} (the drawing area's top-left in macOS screen
points, and screen points per egui point), and clicks at x + action.x * zoom, y + action.y * zoom.

The helper's commands (each prints "trusted: true|false" first, and exits 0 when it did what it says):
    qajev-keys front BUNDLE                  that app is brought to the front and is frontmost
    qajev-keys clip TEXT                     the clipboard holds TEXT (read back)
    qajev-keys click BUNDLE X Y              one left click at screen point X,Y while BUNDLE was frontmost
    qajev-keys keys BUNDLE CODE:MODS ...     each key posted while BUNDLE was frontmost; MODS is cmd+shift+alt+ctrl
"""

import re
import subprocess
from pathlib import Path

from . import keys as page_keys
from .native import NativeError, _step, _step_result, play

HELPER = Path.home() / ".qcloud-keys" / "qajev-keys"
KEYS_KEYS = {"app", "clipboard", "click", "press"}
MAX_TYPED = 200
MAX_CLIPBOARD = 1000
# The other things a step can do: a keys step only presses keys, then checks what they did
OTHER_ACTS = ("goal", "play", "js", "pad", "open_url", "idle", "crash_renderer", "relaunch", "handoff")

# macOS virtual key codes (HIToolbox Events.h, a US keyboard), by the key's DOM code as keys.py names it
MAC_CODES = {
    "KeyA": 0, "KeyS": 1, "KeyD": 2, "KeyF": 3, "KeyH": 4, "KeyG": 5, "KeyZ": 6, "KeyX": 7, "KeyC": 8, "KeyV": 9,
    "KeyB": 11, "KeyQ": 12, "KeyW": 13, "KeyE": 14, "KeyR": 15, "KeyY": 16, "KeyT": 17, "Digit1": 18, "Digit2": 19,
    "Digit3": 20, "Digit4": 21, "Digit6": 22, "Digit5": 23, "Equal": 24, "Digit9": 25, "Digit7": 26, "Minus": 27,
    "Digit8": 28, "Digit0": 29, "BracketRight": 30, "KeyO": 31, "KeyU": 32, "BracketLeft": 33, "KeyI": 34,
    "KeyP": 35, "Enter": 36, "KeyL": 37, "KeyJ": 38, "Quote": 39, "KeyK": 40, "Semicolon": 41, "Backslash": 42,
    "Comma": 43, "Slash": 44, "KeyN": 45, "KeyM": 46, "Period": 47, "Tab": 48, "Space": 49, "Backquote": 50,
    "Backspace": 51, "Escape": 53, "Delete": 117, "ArrowLeft": 123, "ArrowRight": 124, "ArrowDown": 125,
    "ArrowUp": 126,
}
# keys.MODIFIERS bits -> the helper's names
_MODS = {4: "cmd", 2: "ctrl", 1: "alt", 8: "shift"}
# With Cmd or Ctrl: quit, close, hide, minimize, new window or tab, reload, address bar, print, switch apps or
# windows, Spotlight, force quit; and Ctrl+arrows switch Spaces
SYSTEM_SHORTCUTS = {"KeyQ", "KeyW", "KeyH", "KeyM", "KeyN", "KeyT", "KeyR", "KeyL", "KeyP", "Tab", "Space",
                    "Escape", "Backquote"}
# Keys never go to Apple's own apps (Finder, Terminal, Keychain Access, System Settings...: every com.apple. id), nor
# to a browser, a terminal or a password manager. Matched without case, as macOS matches bundle ids (redteam on #141).
SYSTEM_PREFIX = "com.apple."
SYSTEM_APPS = {"com.googlecode.iterm2", "dev.warp.warp-stable", "com.mitchellh.ghostty", "net.kovidgoyal.kitty",
               "org.alacritty", "com.github.wez.wezterm", "com.google.chrome", "com.google.chrome.canary",
               "org.mozilla.firefox", "com.microsoft.edgemac", "com.brave.browser", "company.thebrowser.browser",
               "com.1password.1password", "com.agilebits.onepassword7", "com.bitwarden.desktop"}
BUNDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,154}")


def chord(name):
    """A key or chord ("cmd+v", "Enter", "shift+tab") -> (label, "CODE:MODS"). ValueError for an unknown key and
    for a system shortcut."""
    if not isinstance(name, str) or not name:
        raise ValueError(f"a key is a name such as cmd+v or Enter, not {name!r}")
    *mods, base = name.split("+") if name != "+" else [name]
    bits = 0
    for m in mods:
        bit = page_keys.MODIFIERS.get(m.strip().lower())
        if not bit or bits & bit:
            raise ValueError(f"{name!r}: a chord is Cmd, Ctrl, Alt or Shift (each once) + one plain key")
        bits |= bit
    code = page_keys._plain(base.strip())["code"]
    if bits & (2 | 4) and code in SYSTEM_SHORTCUTS or bits & 2 and code.startswith("Arrow") \
            or bits & 4 and bits & 1 and code == "KeyD":  # Cmd+Alt+D hides the Dock
        raise ValueError(f"refused: {name!r} is a system shortcut (quit, close, hide, minimize, switch apps or "
                         "Spaces, Spotlight, force quit)")
    mods_text = "+".join(_MODS[b] for b in (4, 2, 1, 8) if bits & b)
    return name, f"{MAC_CODES[code]}:{mods_text}"


def typed(text):
    """Text typed key by key -> ["CODE:MODS", ...]: letters (capitals with Shift), digits, space and the unshifted
    punctuation of a US keyboard. ValueError for anything else."""
    if not isinstance(text, str) or not 1 <= len(text) <= MAX_TYPED:
        raise ValueError(f"type: takes text of 1 to {MAX_TYPED} characters")
    out = []
    for ch in text:
        name = "Space" if ch == " " else ch.lower() if ch.isascii() and ch.isalpha() else ch
        if name not in page_keys.KEYS or name in page_keys._NAMED and name != "Space":
            raise ValueError(f"type: cannot type {ch!r}; it types letters, digits, space and ` - = [ ] ; ' , . / \\ "
                             "(press other keys by name)")
        out.append(f"{MAC_CODES[page_keys.KEYS[name]['code']]}:{'shift' if ch.isupper() else ''}")
    return out


def plan(value):
    """A keys step's value -> {app, clipboard, press: [label], codes: ["CODE:MODS"]}. ValueError when malformed."""
    if not isinstance(value, dict):
        raise ValueError("keys: needs app: (the app's bundle id) and press: (the keys)")
    unknown = sorted(set(value) - KEYS_KEYS)
    if unknown:
        raise ValueError(f"unknown key(s) {unknown}; known: {sorted(KEYS_KEYS)}")
    app = value.get("app")
    if not isinstance(app, str) or not BUNDLE.fullmatch(app):
        raise ValueError("app: the bundle id of the app that gets the keys, such as com.hanamorilabs.twinbox")
    if app.lower().startswith(SYSTEM_PREFIX) or app.lower() in SYSTEM_APPS:
        raise ValueError(f"refused: {app} is a system app, a browser, a terminal or a password manager; keys go only "
                         "to the app under test")
    clip = value.get("clipboard")
    if clip is not None and (not isinstance(clip, str) or not 1 <= len(clip) <= MAX_CLIPBOARD):
        raise ValueError(f"clipboard: text of 1 to {MAX_CLIPBOARD} characters")
    click = value.get("click")
    if click is not None and (not isinstance(click, str) or not click.strip()):
        raise ValueError("click: the id of the app's control to click, such as new-box.name")
    press = value.get("press")
    press = press if isinstance(press, list) else [press]
    if not 1 <= len(press) <= page_keys.MAX_KEYS or press == [None]:
        raise ValueError(f"press: takes 1 to {page_keys.MAX_KEYS} keys, chords or {{type: text}}")
    labels, codes = [], []
    for item in press:
        if isinstance(item, dict):
            if set(item) != {"type"}:
                raise ValueError(f"a press is a key, a chord or {{type: text}}, not {item!r}")
            codes += typed(item["type"])
            labels.append(f"typed {item['type']!r}")
        else:
            label, code = chord(item)
            labels.append(label)
            codes.append(code)
    return {"app": app, "clipboard": clip, "click": click, "press": labels, "codes": codes}


def describe(p):
    said = f"{', '.join(p['press'])} in {p['app']}"
    first = [*([f"putting {p['clipboard']!r} on the clipboard"] if p["clipboard"] is not None else []),
             *([f"a click on {p['click']}"] if p["click"] else [])]
    return f"{said}, after {' and '.join(first)}" if first else said


def screen_point(obs, action_id):
    """Where a control of the app is on the screen: (x, y) in macOS screen points, or a reason it cannot be known."""
    action = next((a for a in obs.get("actions") or [] if a.get("id") == action_id), None)
    if action is None:
        return None, f"the app shows no control {action_id!r}"
    window = (obs.get("state") or {}).get("window")
    nums = (int, float)
    if not isinstance(window, dict) or not all(isinstance(window.get(k), nums) and not isinstance(window.get(k), bool)
                                               for k in ("x", "y", "zoom")) or window["zoom"] <= 0:
        return None, "the app does not say where its window is (state.window x, y and zoom)"
    if not all(isinstance(action.get(k), nums) and not isinstance(action.get(k), bool) for k in ("x", "y")):
        return None, f"the control {action_id!r} has no x and y"
    return (round(window["x"] + action["x"] * window["zoom"]), round(window["y"] + action["y"] * window["zoom"])), None


def planned_checks(value):
    """The checks a keys step makes before its expect checks, named as keys_step names them (for `qajev plan`)."""
    try:
        p = plan(value)
    except ValueError:
        return []
    return [{"check": f"{p['app']} was in front"},
            *([{"check": "the clipboard held the text"}] if p["clipboard"] is not None else []),
            *([{"check": f"{p['click']} was clicked"}] if p["click"] else []),
            {"check": f"the keys were pressed in {p['app']}", "says": describe(p)}]


def suite_bundle(app_spec):
    """The bundle id of a suite's `app:` when its command sits in a .app this machine has (its Info.plist), else
    None."""
    import plistlib

    command = (app_spec or {}).get("command") if isinstance(app_spec, dict) else None
    first = command[0] if isinstance(command, list) and command else command
    if not isinstance(first, str) or ".app/" not in first:
        return None
    info = Path(first[:first.index(".app/") + 4]).expanduser() / "Contents" / "Info.plist"
    try:
        bundle = plistlib.loads(info.read_bytes()).get("CFBundleIdentifier")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return bundle if isinstance(bundle, str) and bundle else None


def check_keys(steps, app_spec=None):
    """Each keys step, checked when the suite loads. NativeError names the step and what is wrong. When the suite's
    own app is a .app on this machine, a keys step may name only that app."""
    own = suite_bundle(app_spec)
    for i, step in enumerate(steps or []):
        if not isinstance(step, dict) or step.get("keys") is None:
            continue
        where = f"step {step.get('name') or f'step {i + 1}'!r}"
        try:
            p = plan(step["keys"])
        except ValueError as e:
            raise NativeError(f"{where}: keys: {e}") from None
        if own and p["app"].lower() != own.lower():
            raise NativeError(f"{where}: keys: refused: {p['app']} is not the suite's own app ({own})")
        acting = [k for k in OTHER_ACTS if step.get(k)]
        if acting:
            raise NativeError(f"{where}: a keys step only presses keys, then checks expect:; put "
                              f"{', '.join(acting)} in a step of its own")


def _run(args, timeout=60):
    """-> (exit code or None, stdout and stderr). PATH only: the helper needs nothing else."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    return r.returncode, (r.stdout + r.stderr).strip()


def _last(out):
    return (out.splitlines() or ["no output"])[-1][:300]


def _unknown(out):
    """The helper did not run the command (no answer, or exit 2: its usage, as an older helper answers a command it
    lacks). That says nothing about the app."""
    return f"the qajev-keys helper could not run this command (an older helper?): {_last(out)}"


def keys_step(game, *, name, value, expect=None, ledger=None, run_dir=None, shots=True, emit=None, helper=None,
              run=None, looks_folder=None):
    """A suite's `keys:` step: the app to the front, the clipboard set, the keys pressed (all through qajev-keys),
    then the step's `expect` checks, if it has any. Each part is a check; the first that fails ends the step."""
    p = plan(value)
    helper, run = Path(helper or HELPER), run or _run
    if not helper.is_file():
        return _step_result(name, "browser_error", [], "real keys run only on a QAJev Cloud Mac with the qajev-keys "
                                                       f"helper, and {helper} is not here: nothing was pressed")
    _step(emit, name, None, f"keys: {describe(p)}")
    code, out = run([str(helper), "front", p["app"]])
    if code is None or not out.startswith("trusted: true"):
        why = f"the helper did not run: {out[:200]}" if code is None else \
            "the helper is not trusted for Accessibility on this Mac"
        return _step_result(name, "browser_error", [], f"{why}: nothing was pressed")
    if code == 2:
        return _step_result(name, "browser_error", [], f"{_unknown(out)}: nothing was pressed")
    checks = [{"check": f"{p['app']} was in front", "ok": code == 0, "detail": None if code == 0 else _last(out)}]
    if code == 0 and p["clipboard"] is not None:
        code, out = run([str(helper), "clip", p["clipboard"]])
        if code in (None, 2):
            return _step_result(name, "browser_error", checks, f"{_unknown(out)}: no key was pressed")
        checks.append({"check": "the clipboard held the text", "ok": code == 0,
                       "detail": None if code == 0 else _last(out)})
    if code == 0 and p["click"]:
        try:
            at, why = screen_point(game.observe(), p["click"])
        except NativeError as e:
            return _step_result(name, "browser_error", checks, f"could not read the app before the click: {e}")
        if at is None:
            code, out = 1, why
        else:
            code, out = run([str(helper), "click", p["app"], str(at[0]), str(at[1])])
            if code in (None, 2):
                return _step_result(name, "browser_error", checks, f"{_unknown(out)}: no key was pressed")
        checks.append({"check": f"{p['click']} was clicked", "ok": code == 0,
                       "detail": None if code == 0 else _last(out)})
    if code == 0:
        code, out = run([str(helper), "keys", p["app"], *p["codes"]])
        if code in (None, 2):
            return _step_result(name, "browser_error", checks, f"{_unknown(out)}: no key was pressed")
        checks.append({"check": f"the keys were pressed in {p['app']}", "says": describe(p), "ok": code == 0,
                       "detail": None if code == 0 else _last(out)})
    if code != 0 or not expect:
        return _step_result(name, "reached", checks)
    return play(game, name=name, goal=None, expect=expect, budget={"actions": 0, "seconds": 0}, ledger=ledger,
                run_dir=run_dir, shots=shots, emit=emit, prior=checks, looks_folder=looks_folder)
