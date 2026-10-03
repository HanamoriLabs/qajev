"""The screen of a run as it happens, for `qajev dashboard`, and only while someone watches it.

While a person has a running run open, the dashboard's stream touches <run>/live/watching every few seconds. The
run then saves a frame of its screen every INTERVAL seconds to <run>/live/frame.jpg, over a debugger link of its own
to the same page (a second client: it never waits on the test's link, nor the test on it). Nobody watching: nothing
is captured, so an unwatched run costs nothing. Godot and mobile runs have no page link: no frames.
"""

import base64
import contextlib
import json
import os
import threading
import time
import urllib.request
from pathlib import Path

INTERVAL = 2.0
FRESH = 6.0  # seconds a touch of `watching` lasts: the dashboard touches it every 2 s while the run is open


def folder(run_dir):
    return Path(run_dir) / "live"


def touch(run_dir):
    """The dashboard's side: someone is watching this run now."""
    path = folder(run_dir)
    path.mkdir(parents=True, exist_ok=True)
    (path / "watching").touch()


def watched(run_dir):
    try:
        return time.time() - (folder(run_dir) / "watching").stat().st_mtime < FRESH
    except OSError:
        return False


def frame(run_dir):
    """The latest frame: (path, its mtime), or None when there is none or it is stale."""
    path = folder(run_dir) / "frame.jpg"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return (path, mtime) if time.time() - mtime < FRESH + INTERVAL else None


def page_socket(cdp_http, target):
    """The debugger WebSocket of the page `target` in the Chrome at `cdp_http`, or None."""
    try:
        with urllib.request.urlopen(f"{cdp_http.rstrip('/')}/json/list", timeout=3) as r:
            pages = json.loads(r.read())
    except (OSError, ValueError):
        return None
    return next((p.get("webSocketDebuggerUrl") for p in pages if p.get("id") == target), None)


class Frames(threading.Thread):
    """Saves a frame of the page at `ws_url` every `interval` seconds while the run is watched."""

    def __init__(self, run_dir, ws_url, interval=INTERVAL):
        super().__init__(daemon=True, name="qajev-live")
        self.run_dir, self.ws_url, self.interval = Path(run_dir), ws_url, interval
        self.halt = threading.Event()
        self.ws, self.next_id = None, 0

    def capture(self):
        from websockets.sync.client import connect

        if self.ws is None:
            self.ws = connect(self.ws_url, open_timeout=5, max_size=32 * 2**20)
        self.next_id += 1
        ident = self.next_id
        self.ws.send(json.dumps({"id": ident, "method": "Page.captureScreenshot",
                                 "params": {"format": "jpeg", "quality": 50, "optimizeForSpeed": True}}))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            message = json.loads(self.ws.recv(timeout=max(0.1, deadline - time.monotonic())))
            if message.get("id") == ident:
                return base64.b64decode(message["result"]["data"])
        raise TimeoutError("no frame")

    def run(self):
        while not self.halt.wait(self.interval):
            if not watched(self.run_dir):
                continue
            try:
                data = self.capture()
            except Exception:  # noqa: BLE001 (a frame is a nicety: a closed page or a slow one must not end the run)
                self.close()
                continue
            path = folder(self.run_dir) / "frame.jpg"
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)

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


def frames(run_dir, ws_url):
    """Frames while watched; a no-op without a run folder or a page to look at."""
    return Frames(run_dir, ws_url) if run_dir and ws_url else contextlib.nullcontext()
