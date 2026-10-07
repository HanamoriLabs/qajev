"""Native, Electron: QA a desktop game or app that is a web page inside Electron (I'm Him's Steam build).

QAJev starts the app itself, with a throwaway user-data folder (`--profile`, so no save, Steam Cloud folder or
setting of the player's is touched) and Chromium's DevTools port on 127.0.0.1. It then talks to the app's own
window over that port, the way the Godot bridge talks to a Godot game: an adapter script in the page
(bridges/web/adapters/<game>.js) describes the screen as text, labelled actions and the game's state, and input
goes into that page only (never the machine's mouse or keyboard). Same interface as native.GodotGame, so goal
steps, real-time play, reports and `qajev top` work unchanged.
"""

import base64
import contextlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from websockets.exceptions import ConnectionClosed

from . import chrome, game_profile
from .config import HOME
from .native import STATE, NativeError, NoAnswer, free_port, with_lists

WEB = Path(__file__).parent / "bridges" / "web"
ADAPTERS = WEB / "adapters"
OBSERVE = (WEB / "observe.js").read_text()
KEYS = {"Escape": 27, "Enter": 13, "Space": 32, "Tab": 9, "Backspace": 8, "ArrowLeft": 37, "ArrowUp": 38,
        "ArrowRight": 39, "ArrowDown": 40, "Shift": 16}


def adapter_path(adapter):
    if not adapter:
        return None
    path = Path(adapter).expanduser()
    if path.suffix == ".js" and path.is_file():
        return path.resolve()
    bundled = ADAPTERS / f"{adapter}.js"
    if bundled.is_file():
        return bundled
    known = sorted(p.stem for p in ADAPTERS.glob("*.js"))
    raise NativeError(f"no web adapter {adapter!r}: give a .js path or one of {known}")


def is_electron(path):
    """An .app bundle, or an Electron project folder (package.json with an electron dependency)."""
    path = Path(path).expanduser()
    if path.suffix == ".app":
        return True
    pkg = path / "package.json"
    if pkg.is_file():
        with contextlib.suppress(OSError, json.JSONDecodeError):
            data = json.loads(pkg.read_text())
            return "electron" in {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    return False


def command_for(app):
    """How to start the app: a packaged .app's own binary, or `electron <folder>` for a project folder (the folder's
    own node_modules electron, or $QAJEV_ELECTRON)."""
    app = Path(app).expanduser().resolve()
    if app.suffix == ".app":
        binaries = sorted((app / "Contents" / "MacOS").iterdir())
        if not binaries:
            raise NativeError(f"{app} has no binary in Contents/MacOS")
        return [str(binaries[0])]
    electron = Path(os.environ.get("QAJEV_ELECTRON") or app / "node_modules" / ".bin" / "electron")
    if not electron.exists():
        raise NativeError(f"{app}: no node_modules/.bin/electron (install the project's dependencies first)")
    return [str(electron), str(app)]


FREEZE_WINDOW = 3.0  # seconds of renderer CPU sampled once the app stops answering


def _ps():
    return subprocess.run(["ps", "-A", "-o", "pid=,ppid=,time=,command="], capture_output=True, text=True,
                          timeout=10).stdout


def _cpu_seconds(text):
    """ps `time`: macOS [h:]m:ss.cc, Linux [dd-]hh:mm:ss."""
    try:
        days, _, rest = text.rpartition("-")
        total = 0.0
        for part in rest.split(":"):
            total = total * 60 + float(part)
        return total + (int(days) * 86400 if days else 0)
    except ValueError:
        return None


def _renderer_times(table, root):
    """CPU seconds of each renderer process under `root` (Linux puts them under a zygote, so the whole tree)."""
    rows = []
    for line in table.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2], parts[3]))
    tree, grew = {root}, True
    while grew:
        more = {pid for pid, ppid, _t, _c in rows if ppid in tree} - tree
        tree |= more
        grew = bool(more)
    out = {}
    for pid, _ppid, cpu, command in rows:
        seconds = _cpu_seconds(cpu)
        if pid in tree and "--type=renderer" in command and seconds is not None:
            out[pid] = seconds
    return out


def renderer_cpu(root, window=FREEZE_WINDOW, ps=_ps, sleep=time.sleep):
    """The share of a CPU core the app's busiest renderer used over `window` seconds; None when there is none to
    measure. Asked only once the app has stopped answering, so its cost (two `ps`, 3 s) is paid only then."""
    try:
        before = _renderer_times(ps(), root)
        if not before:
            return None
        sleep(window)
        after = _renderer_times(ps(), root)
    except (OSError, subprocess.SubprocessError):
        return None
    used = [after[pid] - before[pid] for pid in before if pid in after]
    return max(used) / window if used else None



# Punctuation keys: their DOM code and Windows virtual key (games read e.code, e.g. "Backquote" for a dev menu).
PUNCT = {"`": ("Backquote", 192), "[": ("BracketLeft", 219), "]": ("BracketRight", 221), "-": ("Minus", 189),
         "=": ("Equal", 187), ";": ("Semicolon", 186), "'": ("Quote", 222), ",": ("Comma", 188),
         ".": ("Period", 190), "/": ("Slash", 191), "\\": ("Backslash", 220)}
PUNCT_BY_CODE = {code: ch for ch, (code, _vk) in PUNCT.items()}


def key_event(key):
    """Chromium key event fields for a key name: "Escape", "Enter", "i", "ArrowLeft", "`" or "Backquote"."""
    key = PUNCT_BY_CODE.get(key) or key
    if key in PUNCT:
        code, vk = PUNCT[key]
        return {"key": key, "code": code, "windowsVirtualKeyCode": vk, "text": key}
    if len(key) == 1:
        upper = key.upper()
        code = f"Key{upper}" if upper.isalpha() else f"Digit{key}" if key.isdigit() else ""
        return {"key": key, "code": code, "windowsVirtualKeyCode": ord(upper), "text": key}
    if key == "Space":
        return {"key": " ", "code": "Space", "windowsVirtualKeyCode": 32, "text": " "}
    fields = {"key": key, "code": key, "windowsVirtualKeyCode": KEYS.get(key, 0)}
    if key == "Enter":
        fields["text"] = "\r"
    return fields


class ElectronGame:
    """An Electron app started by QAJev, driven through its own window. Use as a context manager."""

    engine = "electron"

    def __init__(self, app, *, adapter=None, args=(), headless=False, size=(1280, 800), start_wait=60.0, env=None,
                 hide=None, allow=None, profile=None, reset_profile=False):
        self.project = Path(app).expanduser().resolve()
        if not self.project.exists():
            raise NativeError(f"no app at {self.project}")
        self.adapter = adapter_path(adapter)
        self.hide, self.allow = list(hide or []), list(allow or [])  # a suite's own labels (native.with_lists)
        # "--game-dir=~/x" from a suite: ~ is the home folder (no shell expands it here)
        args = [f"{a.partition('=')[0]}={os.path.expanduser(a.partition('=')[2])}" if "=~" in a else a
                for a in map(str, args)]
        # A kept TEST profile (game_profile.py): a suite's own --profile=/--user-data-dir= folder, or a named one.
        # QAJev passes it as both flags and before nothing of its own, and never deletes it. Else a throwaway.
        given = {a.partition("=")[2] for a in args if a.startswith(("--profile=", "--user-data-dir="))}
        self.args = [a for a in args if not a.startswith(("--profile=", "--user-data-dir="))]
        try:
            if len(given) > 1 or (given and profile):
                raise game_profile.ProfileError("give one kept profile: --game-profile NAME, or the same folder in "
                                                "--profile= and --user-data-dir=")
            self.kept = game_profile.named(profile) if profile else (
                game_profile.check(given.pop(), self.project) if given else None)
            if reset_profile:
                if self.kept is None:
                    raise game_profile.ProfileError("--reset-game-profile empties a kept profile: name one")
                game_profile.reset(self.kept, self.project)
        except game_profile.ProfileError as e:
            raise NativeError(str(e)) from None
        self.headless, self.size, self.start_wait = headless, size, start_wait
        self.env = dict(env or {})
        self.proc = self.ws = None
        self.errors, self.log = [], []
        self.user_dir = None
        self.renderer_gone = False  # set by crash_renderer: the page is dead, the app runs on
        self._pending, self._next, self._lock = {}, 0, threading.Lock()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- lifecycle ----
    def start(self):
        from websockets.sync.client import connect

        started = time.monotonic()
        self.port = free_port()
        STATE.mkdir(parents=True, exist_ok=True)
        (HOME / "tmp").mkdir(parents=True, exist_ok=True)
        self.user_dir = str(self.kept) if self.kept else tempfile.mkdtemp(prefix=f"qajev-native-{os.getpid()}-",
                                                                          dir=HOME / "tmp")
        cmd = [*command_for(self.project), f"--profile={self.user_dir}", f"--user-data-dir={self.user_dir}",
               f"--remote-debugging-port={self.port}", "--remote-debugging-address=127.0.0.1", *self.args]
        self.proc = subprocess.Popen(cmd, env={**os.environ, **self.env}, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, stdin=subprocess.DEVNULL,
                                     start_new_session=True, preexec_fn=lambda: os.nice(10))
        threading.Thread(target=self._read_output, daemon=True).start()
        self.record = {"pid": self.proc.pid, "owner_pid": os.getpid(), "engine": "electron",
                       "project": str(self.project), "adapter": self.adapter.stem if self.adapter else None,
                       "port": self.port, "headless": False, "started_at": time.time(), "user_dir": self.user_dir,
                       "profile": {"folder": self.user_dir, "kept": self.kept is not None},
                       "identity": chrome.identity(self.proc.pid)}  # the reaper kills only this very process
        (STATE / f"{self.proc.pid}.json").write_text(json.dumps(self.record))
        page = None
        while time.monotonic() - started < self.start_wait:
            if self.proc.poll() is not None:
                self.close()
                raise NativeError(f"the app exited with {self.proc.returncode} before its window came up: "
                                  + " | ".join(self.log[-5:]))
            page = self._page()
            if page:
                break
            time.sleep(0.3)
        if not page:
            self.close()
            raise NativeError(f"no app window on DevTools port {self.port} within {self.start_wait:.0f} s")
        self.url = page["url"]
        self.page_ws = page["webSocketDebuggerUrl"]  # also for live.py's frames, as a second client
        self.ws = connect(self.page_ws, max_size=64 * 2**20, open_timeout=10)
        threading.Thread(target=self._read_ws, daemon=True).start()
        self.send("Runtime.enable")
        self.send("Page.enable")
        self.send("Inspector.enable")
        if self.adapter:
            source = self.adapter.read_text()
            self.send("Page.addScriptToEvaluateOnNewDocument", source=source)  # and after any reload
            self.evaluate(source)
        self.boot_seconds = round(time.monotonic() - started, 2)
        return self

    def _page(self):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=1) as r:
                targets = json.loads(r.read())
        except (OSError, ValueError):
            return None
        pages = [t for t in targets if t.get("type") == "page" and not t.get("url", "").startswith("devtools:")]
        ready = [t for t in pages if t.get("url") not in ("", "about:blank")]
        return (ready or [None])[0]

    def _read_output(self):
        if self.proc is None or self.proc.stdout is None:
            return
        for line in self.proc.stdout:
            self.log.append(line.rstrip())
            del self.log[:-200]

    def _read_ws(self):
        while self.ws is not None:
            try:
                message = json.loads(self.ws.recv())
            except Exception:  # closed or broken: pending calls fail on their timeout
                return
            if "id" in message:
                slot = self._pending.get(message["id"])
                if slot:
                    slot[1] = message
                    slot[0].set()
                continue
            method, params = message.get("method"), message.get("params") or {}
            if method == "Runtime.exceptionThrown":
                d = params.get("exceptionDetails") or {}
                text = (d.get("exception") or {}).get("description") or d.get("text") or "exception"
                self.errors.append(f"uncaught: {text}"[:300])
            elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
                parts = [str(a.get("value", a.get("description", ""))) for a in params.get("args") or []]
                self.errors.append(f"console.error: {' '.join(parts)}"[:300])
            elif method == "Inspector.targetCrashed":
                self.errors.append("the page crashed")

    def send(self, method, timeout=20.0, **params):
        if self.ws is None:
            raise NativeError("the app is not running")
        with self._lock:
            self._next += 1
            ident = self._next
            slot = [threading.Event(), None]
            self._pending[ident] = slot
        try:
            self.ws.send(json.dumps({"id": ident, "method": method, "params": params}))
            if not slot[0].wait(timeout):
                if self.proc is not None and self.proc.poll() is not None:
                    raise NativeError("the app closed (crashed or quit)")
                raise NoAnswer(f"no answer to {method} within {timeout:.0f} s", window=FREEZE_WINDOW,
                               cpu=renderer_cpu(self.proc.pid, FREEZE_WINDOW) if self.proc is not None else None)
        except OSError as e:
            raise NativeError(f"lost the app: {e}") from None
        except ConnectionClosed:  # the app quit or crashed between two looks
            raise NativeError("the app closed its window (crashed or quit)") from None
        finally:
            self._pending.pop(ident, None)
        answer = slot[1] or {}
        if "error" in answer:
            raise NativeError(f"{method}: {answer['error'].get('message')}")
        return answer.get("result") or {}

    def evaluate(self, expression):
        r = self.send("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise NativeError(f"page script failed: {(d.get('exception') or {}).get('description') or d.get('text')}")
        return (r.get("result") or {}).get("value")

    # ---- the GodotGame interface ----
    def call(self, **request):
        if request.get("op") == "pilot":
            on = bool(request.get("on"))
            got = self.evaluate(f"(() => {{ const a = window.__qajevAdapter; "
                                f"return !!(a && a.pilot && a.pilot({json.dumps(on)})); }})()")
            return {"ok": True, "pilot": bool(got)}
        raise NativeError(f"unknown op {request.get('op')!r}")

    def observe(self):
        obs = self.evaluate(OBSERVE) or {}
        obs.setdefault("ok", True)
        return with_lists(obs, self.hide, self.allow)

    def act(self, action):
        if action.get("kind") == "adapter":  # the adapter carries it out in the page (e.g. a game bot's decide())
            done = self.evaluate(f"(() => {{ const a = window.__qajevAdapter; "
                                 f"return a && a.act ? a.act({json.dumps(action)}) : null; }})()")
            out = {"ok": bool(done and done.get("ok"))}
            if done and done.get("label"):  # what was picked: a bot's own pick says so only once made
                out["label"] = done["label"]
            return out
        if action.get("kind") == "key" or ("key" in action and "x" not in action):
            fields = key_event(str(action["key"]))
            self.send("Input.dispatchKeyEvent", type="keyDown", **fields)
            time.sleep(float(action.get("hold", 0.08)))  # games read held keys per frame
            fields.pop("text", None)
            self.send("Input.dispatchKeyEvent", type="keyUp", **fields)
            return {"ok": True}
        x, y = float(action["x"]), float(action["y"])
        self.send("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        for kind in ("mousePressed", "mouseReleased"):
            self.send("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left", clickCount=1)
        return {"ok": True}

    def shot(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = self.send("Page.captureScreenshot", format="jpeg", quality=80)["data"]
        except (NativeError, KeyError):
            return None
        path.write_bytes(base64.b64decode(data))
        return path

    def use_pad(self):
        """The virtual pad (pad.SHIM_JS) in this page, and in any page it loads later before that page's own code.
        The game's first page loads before QAJev connects, so there the pad appears now, with a gamepadconnected
        event: a game that reads navigator.getGamepads as it plays sees it."""
        from . import pad

        if getattr(self, "_pad", False):
            return
        self.send("Page.addScriptToEvaluateOnNewDocument", source=pad.SHIM_JS)
        self.evaluate(pad.SHIM_JS)
        self._pad = True

    def run_js(self, expression):
        """A suite's `js:` step: the expression's value (a promise is awaited). A script error is a NativeError
        "page script failed: ..."; an error it schedules for later reaches the page as uncaught."""
        return self.evaluate(expression)

    def crash_renderer(self, wait=10.0):
        """A suite's `crash_renderer:` step: crash the page's renderer (Page.crash), as a renderer crash would, so the
        app's crash reporter can write and send its report. -> True when the app's main process is still running."""
        with contextlib.suppress(NativeError):  # the page dies before it answers
            self.send("Page.crash", timeout=3.0)
        until = time.monotonic() + wait
        while time.monotonic() < until and "the page crashed" not in self.errors and self.ws is not None:
            time.sleep(0.2)
        self.renderer_gone = True
        self.errors = [e for e in self.errors if e != "the page crashed"]  # asked for: not a finding
        time.sleep(1.0)
        return self.proc is not None and self.proc.poll() is None

    def close(self):
        ws, self.ws = self.ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                ws.close()
        if self.proc is not None and self.proc.poll() is None:
            # Our own process group only (start_new_session): the app and the helpers it started, nothing else.
            for sig, wait in ((signal.SIGTERM, 8), (signal.SIGKILL, 5)):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.proc.pid, sig)
                try:
                    self.proc.wait(wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
        if self.proc is not None:
            (STATE / f"{self.proc.pid}.json").unlink(missing_ok=True)
        if self.user_dir and self.kept is None:  # a kept test profile stays: the next plan starts from its saves
            shutil.rmtree(self.user_dir, ignore_errors=True)
