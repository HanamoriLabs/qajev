"""Why a request failed, in Chrome's own words (net::ERR_NAME_NOT_RESOLVED, blocked by CORS, ...).

A page hears only that an image or a script failed: its error event carries no reason. Chrome's network events do,
so a thread holds a debugger link of its own to the page (a second client, as live.py's frames: never in the test's
way) and remembers the requests that failed. A "failed to load" finding then says why, or, when its own request did
not fail (a module served from cache whose import failed), which requests failed around it.

The same link lists every request whose address matches the run's block_urls, with its method and the document that
sent it, and whether Chrome blocked it: a readonly run can then show what a page (or a frame in it) tried to reach.
It also reads the guard's record inside each frame of the page (`drain_frames`): an iframe on another origin keeps its
own record, which the top page cannot read.
"""

import contextlib
import json
import threading
import time
from collections import OrderedDict

KEEP = 200  # failed requests remembered
LISTED = 500  # block-list requests kept between two takes
IDS = 5000  # request id -> address (loadingFailed names only the id)
AROUND = 15.0  # seconds: the failures "around" a failed resource, so an earlier test's do not leak into this one


def _bare(url):
    return str(url or "").split("#", 1)[0]


def blocks(url, patterns):
    """Whether Network.setBlockedURLs blocks `url`, matched as Chrome matches it: a pattern's parts between `*`s appear
    in the address in that order, anywhere in it (blink's InspectorNetworkAgent: no anchoring)."""
    url = str(url or "")
    for pattern in patterns or ():
        at = 0
        for part in str(pattern).split("*"):
            at = url.find(part, at)
            if at < 0:
                break
            at += len(part)
        else:
            return True
    return False


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

    def __init__(self, ws_url, block_urls=()):
        super().__init__(daemon=True, name="qajev-netlog")
        self.ws_url = ws_url
        self.halt = threading.Event()
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.urls = OrderedDict()  # request id -> (address, method, the document that sent it)
        self.failed = OrderedDict()  # address -> (reason, when)
        self.patterns = list(block_urls or ())
        self.block_list = []  # requests to a blocked address, since the last take
        self.listed = OrderedDict()  # request id -> its block_list entry, until Chrome says how it ended
        self.contexts = {}  # frame id -> its page-world execution context (Runtime), for drain_frames
        self.runtime = False
        self.replies = {}  # message id -> [answered, reply]
        self.next_id = 1
        self.ws = None

    def run(self):
        from websockets.sync.client import connect

        try:
            self.ws = connect(self.ws_url, open_timeout=5, max_size=32 * 2**20)
            with self.send_lock:
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
            if "id" in message and message["id"] in self.replies:
                waiter = self.replies[message["id"]]
                waiter[1] = message
                waiter[0].set()
            elif method == "Network.requestWillBeSent":
                rid, request = params.get("requestId"), params.get("request") or {}
                sent = (_bare(request.get("url")), str(request.get("method") or "GET").upper(),
                        str(params.get("documentURL") or "")[:300])
                self.urls[rid] = sent
                while len(self.urls) > IDS:
                    self.urls.popitem(last=False)
                if self.patterns and blocks(sent[0], self.patterns):
                    self._list(rid, sent)
            elif method == "Network.loadingFailed":
                rid = params.get("requestId")
                sent = self.urls.pop(rid, None)
                if sent:
                    self.failed.pop(sent[0], None)
                    self.failed[sent[0]] = (describe(params), time.monotonic())
                    while len(self.failed) > KEEP:
                        self.failed.popitem(last=False)
                entry = self.listed.pop(rid, None)
                if params.get("blockedReason") == "inspector":  # block_urls (setBlockedURLs) stopped it
                    if entry is None and sent and self.patterns:  # Chrome's reading of a pattern is the one that counts
                        entry = self._list(rid, sent)
                        self.listed.pop(rid, None)
                    if entry is not None:
                        entry["blocked"] = True
            elif method in ("Network.loadingFinished", "Network.responseReceived"):
                self.listed.pop(params.get("requestId"), None)
            elif method == "Runtime.executionContextCreated":
                context = params.get("context") or {}
                aux = context.get("auxData") or {}
                if aux.get("isDefault") and aux.get("frameId"):
                    self.contexts[aux["frameId"]] = context.get("id")
            elif method == "Runtime.executionContextDestroyed":
                gone = params.get("executionContextId")
                self.contexts = {f: c for f, c in self.contexts.items() if c != gone}
            elif method == "Runtime.executionContextsCleared":
                self.contexts = {}

    def _list(self, rid, sent):
        """Keep a request to a blocked address (under self.lock). Blocked stays False until Chrome says so."""
        entry = {"method": sent[1], "url": sent[0][:300], "frame": sent[2], "blocked": False}
        if len(self.block_list) < LISTED:
            self.block_list.append(entry)
        self.listed[rid] = entry
        while len(self.listed) > IDS:
            self.listed.popitem(last=False)
        return entry

    def take_block_list(self):
        """The requests to a blocked address since the last take: [{method, url, frame, blocked}]."""
        with self.lock:
            taken, self.block_list = self.block_list, []
        return taken

    def ask(self, method, wait=3.0, **params):
        """Send one command on this link and wait for its reply (this thread's reader hands it over)."""
        if self.ws is None or not self.is_alive():
            raise RuntimeError("the network log's link is closed")
        with self.lock:
            self.next_id += 1
            mid = self.next_id
            waiter = self.replies[mid] = [threading.Event(), None]
        try:
            with self.send_lock:
                self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
            if not waiter[0].wait(wait):
                raise TimeoutError(method)
        finally:
            with self.lock:
                self.replies.pop(mid, None)
        reply = waiter[1] or {}
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error'].get('message')}")
        return reply.get("result") or {}

    def drain_frames(self, frames, expression):
        """`expression`'s value in the page world of each frame in `frames` (frame ids): [(frame id, value)].
        Runtime is turned on at the first call only, so a page without frames never pays for it."""
        if not self.runtime:
            self.ask("Runtime.enable")  # Chrome reports the existing contexts before it replies
            self.runtime = True
        with self.lock:
            targets = [(f, c) for f, c in self.contexts.items() if f in frames]
        out = []
        for frame, context in targets:
            try:
                r = self.ask("Runtime.evaluate", expression=expression, contextId=context, returnByValue=True,
                             timeout=3000)
            except (RuntimeError, TimeoutError):
                continue
            value = (r.get("result") or {}).get("value")
            if value:
                out.append((frame, value))
        return out

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


def start(ws_url, block_urls=()):
    """A running Failures for the page at `ws_url`, or None without one."""
    if not ws_url:
        return None
    failures = Failures(ws_url, block_urls)
    failures.start()
    return failures
