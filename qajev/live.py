"""The screen of a run as it happens, for `qajev dashboard`, and only while someone watches it.

While a person has a running run open, the dashboard's stream touches <run>/live/watching every few seconds. The
run then saves a frame of its screen every INTERVAL seconds to <run>/live/frame.jpg, over a debugger link of its own
to the same page (a second client: it never waits on the test's link, nor the test on it). Nobody watching: nothing
is captured, so an unwatched run costs nothing. Godot and mobile runs have no page link: no frames.

LIVE (José, 5 Oct: "click LIVE to watch"): while the dashboard streams a run to a viewer it touches
<run>/live/streaming, and the run captures at the live rate instead (5 frames a second, 1280 px wide; 2 when the
machine is busy; 1 during a real-time step such as a react hook or a held key, so a frame never steals the game's
time). The grabber may only look at its page and bring it to the front; the seconds watched live go in the report.
"""

import base64
import contextlib
import json
import os
import platform
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

INTERVAL = 2.0
FRESH = 6.0  # seconds a touch of `watching` lasts: the dashboard touches it every 2 s while the run is open
FPS, FPS_BUSY, FPS_REALTIME = 5.0, 2.0, 1.0
WIDTH, QUALITY = 1280, 60
MAX_VIEWERS = 2  # per run: watching must never slow the run down
BUSY_LOAD, BUSY_MEMORY = 60, 30  # load1 at or above, or memory free (%) below: the busy rate
# The only debugger methods the grabber may send: it looks at its page and brings it to the front, nothing else.
ALLOWED = {"Page.captureScreenshot", "Page.getLayoutMetrics", "Page.getNavigationHistory", "Page.bringToFront"}
REALTIME = threading.Event()  # set by the session while a real-time step runs (session.react, a held key)


def folder(run_dir):
    return Path(run_dir) / "live"


def touch(run_dir):
    """The dashboard's side: someone is watching this run now."""
    path = folder(run_dir)
    path.mkdir(parents=True, exist_ok=True)
    (path / "watching").touch()


def stream_touch(run_dir):
    """The dashboard's side: someone is watching this run LIVE now (the live rate); also watching."""
    touch(run_dir)
    (folder(run_dir) / "streaming").touch()


def _fresh(run_dir, name):
    try:
        return time.time() - (folder(run_dir) / name).stat().st_mtime < FRESH
    except OSError:
        return False


def watched(run_dir):
    return _fresh(run_dir, "watching")


def streaming(run_dir):
    return _fresh(run_dir, "streaming")


def frame(run_dir):
    """The latest frame: (path, its mtime), or None when there is none or it is stale."""
    path = folder(run_dir) / "frame.jpg"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return (path, mtime) if time.time() - mtime < FRESH + INTERVAL else None


_MACHINE = {"at": 0.0, "load1": 0.0, "mem_free": 100.0}


def machine():
    """load1 and memory free (%), sampled at most every 10 s (memory_pressure is a process on macOS)."""
    if time.monotonic() - _MACHINE["at"] > 10:
        _MACHINE["at"], _MACHINE["load1"] = time.monotonic(), os.getloadavg()[0]
        with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
            if platform.system() == "Darwin":
                out = subprocess.run(["memory_pressure", "-Q"], capture_output=True, text=True, timeout=5).stdout
                _MACHINE["mem_free"] = float(out.rsplit(":", 1)[1].strip().rstrip("%"))
            else:
                info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
                total, avail = (float(info[k].split()[0]) for k in ("MemTotal", "MemAvailable"))
                _MACHINE["mem_free"] = 100 * avail / total
    return _MACHINE["load1"], _MACHINE["mem_free"]


def interval(load1=None, mem_free=None):
    """Seconds between live frames: 5 a second; 2 when the machine is busy; 1 during a real-time step."""
    if REALTIME.is_set():
        return 1 / FPS_REALTIME
    if load1 is None or mem_free is None:
        load1, mem_free = machine()
    return 1 / (FPS_BUSY if load1 >= BUSY_LOAD or mem_free < BUSY_MEMORY else FPS)


def page_socket(cdp_http, target):
    """The debugger WebSocket of the page `target` in the Chrome at `cdp_http`, or None."""
    try:
        with urllib.request.urlopen(f"{cdp_http.rstrip('/')}/json/list", timeout=3) as r:
            pages = json.loads(r.read())
    except (OSError, ValueError):
        return None
    return next((p.get("webSocketDebuggerUrl") for p in pages if p.get("id") == target), None)


def _write(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class Frames(threading.Thread):
    """Saves a frame of the page at `ws_url` while the run is watched: every `interval` seconds, or at the live rate
    while it is streamed. `managed`: QAJev started this Chrome, so a viewer may bring the page to the front."""

    def __init__(self, run_dir, ws_url, interval=INTERVAL, managed=False):
        super().__init__(daemon=True, name="qajev-live")
        self.run_dir, self.ws_url, self.interval, self.managed = Path(run_dir), ws_url, interval, managed
        self.halt = threading.Event()
        self.ws, self.next_id = None, 0
        self.seconds_live = 0.0

    def send(self, method, params):
        """One debugger call on the grabber's own link, from the allow-list only."""
        if method not in ALLOWED:
            raise PermissionError(f"the live frame grabber may not call {method}")
        from websockets.sync.client import connect

        if self.ws is None:
            self.ws = connect(self.ws_url, open_timeout=5, max_size=32 * 2**20)
        self.next_id += 1
        ident = self.next_id
        self.ws.send(json.dumps({"id": ident, "method": method, "params": params}))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            message = json.loads(self.ws.recv(timeout=max(0.1, deadline - time.monotonic())))
            if message.get("id") == ident:
                return message.get("result") or {}
        raise TimeoutError(f"no answer to {method}")

    def capture(self, live=False):
        params = {"format": "jpeg", "quality": QUALITY if live else 50, "optimizeForSpeed": True}
        if live:  # at most WIDTH pixels wide: a 4K screen streams as 1280
            view = self.send("Page.getLayoutMetrics", {}).get("cssVisualViewport") or {}
            width, height = view.get("clientWidth") or WIDTH, view.get("clientHeight") or 720
            scale = min(1.0, WIDTH / width)
            params["clip"] = {"x": view.get("pageX", 0), "y": view.get("pageY", 0), "width": width, "height": height,
                              "scale": scale}
        return base64.b64decode(self.send("Page.captureScreenshot", params)["data"])

    def page_url(self):
        history = self.send("Page.getNavigationHistory", {})
        entries = history.get("entries") or []
        return (entries[history.get("currentIndex", -1)] if entries else {}).get("url") or ""

    def _front(self):
        request = folder(self.run_dir) / "front"
        if request.is_file():
            request.unlink(missing_ok=True)
            if self.managed:
                self.send("Page.bringToFront", {})

    def run(self):
        wait = 0.05  # the first look at once: a viewer already watching sees the test's first frame, not 2 s later
        last = time.monotonic()
        while not self.halt.wait(wait):
            now = time.monotonic()
            elapsed, last = now - last, now  # the real time since the last look: a slow capture still counts
            is_live = streaming(self.run_dir)
            wait = interval() if is_live else self.interval
            if not (is_live or watched(self.run_dir)):
                continue
            try:
                data = self.capture(live=True) if is_live else self.capture()
                if is_live:
                    self.seconds_live += elapsed
                    parts = urlsplit(self.page_url())  # the page's origin and path only: a query can hold a token
                    page = f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme else ""
                    _write(folder(self.run_dir) / "page.json", json.dumps({"url": page}).encode())
                self._front()
            except PermissionError:
                raise
            except Exception:  # noqa: BLE001 (a frame is a nicety: a closed page or a slow one must not end the run)
                self.close()
                continue
            _write(folder(self.run_dir) / "frame.jpg", data)

    def close(self):
        if self.ws is not None:
            with contextlib.suppress(Exception):
                self.ws.close()
            self.ws = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_exc):
        self.halt.set()
        self.join(timeout=self.interval + 12)
        self.close()
        if self.seconds_live:  # for the report: "watched live for N s", over every test of the run
            folder(self.run_dir).mkdir(parents=True, exist_ok=True)
            total = watched_seconds(self.run_dir) + self.seconds_live
            _write(folder(self.run_dir) / "watched.json", json.dumps({"seconds": round(total, 1)}).encode())


def watched_seconds(run_dir):
    """How long the run was watched live, for the report, or 0."""
    try:
        return float(json.loads((folder(run_dir) / "watched.json").read_text())["seconds"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def frames(run_dir, ws_url, managed=False):
    """Frames while watched; a no-op without a run folder or a page to look at."""
    return Frames(run_dir, ws_url, managed=managed) if run_dir and ws_url else contextlib.nullcontext()
