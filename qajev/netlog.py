"""Why a request failed, in Chrome's own words (net::ERR_NAME_NOT_RESOLVED, blocked by CORS, ...).

A page hears only that an image or a script failed: its error event carries no reason. Chrome's network events do,
so a thread holds a debugger link of its own to the page (a second client, as live.py's frames: never in the test's
way) and remembers the requests that failed. A "failed to load" finding then says why, or, when its own request did
not fail (a module served from cache whose import failed), which requests failed around it.
"""

import contextlib
import json
import threading
import time
from collections import OrderedDict

KEEP = 200  # failed requests remembered
IDS = 5000  # request id -> address (loadingFailed names only the id)
AROUND = 15.0  # seconds: the failures "around" a failed resource, so an earlier test's do not leak into this one


def _bare(url):
    return str(url or "").split("#", 1)[0]


def describe(params):
    """A Network.loadingFailed event as words: "net::ERR_FAILED, CORS: MissingAllowOriginHeader"."""
    parts = [params.get("errorText") or "failed"]
    if params.get("blockedReason"):
        parts.append(f"blocked: {params['blockedReason']}")
    cors = (params.get("corsErrorStatus") or {}).get("corsError")
    if cors:
        parts.append(f"CORS: {cors}")
    if params.get("canceled") and "ERR_ABORTED" not in parts[0]:
        parts.append("canceled")
    return ", ".join(parts)


class Failures(threading.Thread):
    """The page's failed requests, as Chrome reports them, from a debugger link of its own to `ws_url`."""

    def __init__(self, ws_url):
        super().__init__(daemon=True, name="qajev-netlog")
        self.ws_url = ws_url
        self.halt = threading.Event()
        self.lock = threading.Lock()
        self.urls = OrderedDict()
        self.failed = OrderedDict()  # address -> (reason, when)
        self.ws = None

    def run(self):
        from websockets.sync.client import connect

        try:
            self.ws = connect(self.ws_url, open_timeout=5, max_size=32 * 2**20)
            self.ws.send(json.dumps({"id": 1, "method": "Network.enable", "params": {"maxPostDataSize": 0}}))
            while not self.halt.is_set():
                try:
                    self.take(json.loads(self.ws.recv(timeout=0.5)))
                except TimeoutError:
                    continue
        except Exception:  # noqa: BLE001 (the reasons are a nicety: a closed page must not end the run)
            pass
        finally:
            self.close()

    def take(self, message):
        method, params = message.get("method"), message.get("params") or {}
        with self.lock:
            if method == "Network.requestWillBeSent":
                self.urls[params.get("requestId")] = _bare((params.get("request") or {}).get("url"))
                while len(self.urls) > IDS:
                    self.urls.popitem(last=False)
            elif method == "Network.loadingFailed":
                url = self.urls.pop(params.get("requestId"), None)
                if url:
                    self.failed.pop(url, None)
                    self.failed[url] = (describe(params), time.monotonic())
                    while len(self.failed) > KEEP:
                        self.failed.popitem(last=False)

    def reason(self, url, wait=0.5):
        """Chrome's reason `url` failed, or None. Waits a moment: the page can hear of a failure first."""
        url, deadline = _bare(url), time.monotonic() + wait
        while True:
            with self.lock:
                found = self.failed.get(url)
            if found or time.monotonic() >= deadline or not self.is_alive():
                return found[0] if found else None
            time.sleep(0.05)

    def explain(self, url):
        """Words for a "failed to load" finding: why it failed, else which requests failed around it, else None."""
        own = self.reason(url)
        if own:
            return own
        now = time.monotonic()
        with self.lock:
            # a navigation cancels its leftovers (net::ERR_ABORTED): that is not why something failed
            others = [(u, r) for u, (r, t) in reversed(self.failed.items())
                      if u != _bare(url) and now - t < AROUND and "ERR_ABORTED" not in r][:2]
        if others:
            return "no network error for it; failed: " + "; ".join(f"{u} ({r})" for u, r in others)
        return None

    def close(self):
        if self.ws is not None:
            with contextlib.suppress(Exception):
                self.ws.close()
            self.ws = None

    def stop(self):
        self.halt.set()
        self.join(timeout=2)
        self.close()


def start(ws_url):
    """A running Failures for the page at `ws_url`, or None without one."""
    if not ws_url:
        return None
    failures = Failures(ws_url)
    failures.start()
    return failures
