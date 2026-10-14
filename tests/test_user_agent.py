"""QAJev's Chrome says it is QAJev: "QAJev/<version>" on the end of the browser's own user agent (FlockTab1, 14 Oct).

José's pages server counts a page as seen when José opens it, and skips a browser whose user agent names QAJev. A
local QAJev Chrome looked like José's own, so a QAJev visit marked his pages as read. Now every page QAJev drives says
QAJev, from its first request, on a phone too, and keeps the browser's own user-agent client hints.

The live half runs in QAJev's own throwaway Chrome, in a visible window (never headless). Nothing leaves the machine.
"""

import http.server
import os
import threading
from functools import partial
from types import SimpleNamespace

import pytest

from qajev import __version__
from qajev import session as session_mod

live = pytest.mark.skipif(os.environ.get("QAJEV_LIVE") != "1", reason="set QAJEV_LIVE=1 (starts a local Chrome)")
CHROME = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 " \
         "Safari/537.36"
PHONE = {"width": 390, "height": 844, "mobile": True, "scale": 3, "platform": "iPhone",
         "ua": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"}
DESKTOP = {"width": 1280, "height": 900, "mobile": False, "scale": 1}


def test_the_name_goes_on_the_end_once():
    assert session_mod.with_qajev(CHROME) == f"{CHROME} QAJev/{__version__}"
    assert session_mod.with_qajev(session_mod.with_qajev(CHROME)) == f"{CHROME} QAJev/{__version__}"


def fake_session(monkeypatch, hints):
    monkeypatch.setattr(session_mod, "client_hints", lambda cdp, cdp_url: hints)
    s = session_mod.Session.__new__(session_mod.Session)
    s.device = s.user_agent = None
    s.jev = SimpleNamespace(cdp=None)
    s.calls = []
    s.call = lambda method, **params: s.calls.append((method, params)) or {}
    return s


def overrides(s):
    return [p for m, p in s.calls if m == "Emulation.setUserAgentOverride"]


def test_a_phone_says_qajev_and_the_desktop_gets_its_own_marked_agent_back(monkeypatch):
    hints = {"brands": [{"brand": "Google Chrome", "version": "141"}], "mobile": False, "platform": "macOS"}
    s = fake_session(monkeypatch, hints)
    s._own_user_agent(CHROME)
    s.set_device(PHONE)
    s.set_device(DESKTOP)
    mine = {"userAgent": f"{CHROME} QAJev/{__version__}", "userAgentMetadata": hints}
    assert overrides(s) == [mine, {"userAgent": f"{PHONE['ua']} QAJev/{__version__}", "platform": "iPhone"}, mine]


def test_without_client_hints_or_a_known_agent_nothing_is_invented(monkeypatch):
    s = fake_session(monkeypatch, None)  # Chrome gave no client hints
    s._own_user_agent(CHROME)
    assert overrides(s) == [{"userAgent": f"{CHROME} QAJev/{__version__}"}]
    s = fake_session(monkeypatch, None)
    s._own_user_agent("")  # the browser did not say: its own agent stays, unmarked
    s.set_device(DESKTOP)
    assert overrides(s) == [{"userAgent": ""}]


# ---- live: the page, its requests and its worker all say QAJev ----

PAGE = ("<!doctype html><title>ua</title><p>UA page</p><script>"
        "window.workerUA = new Promise(ok => { const w = new Worker('worker.js'); w.onmessage = e => ok(e.data); });"
        "</script>")


class Handler(http.server.SimpleHTTPRequestHandler):
    seen = []

    def do_GET(self):  # noqa: N802 (http.server's name)
        Handler.seen.append(self.headers.get("User-Agent", ""))
        super().do_GET()

    def log_message(self, *_):
        pass


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("user-agent")
    (root / "page.html").write_text(PAGE)
    (root / "worker.js").write_text("postMessage(navigator.userAgent);")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    from qajev import chrome

    record = chrome.start(f"user-agent-{os.getpid()}", headless=False, ephemeral=True)  # visible, never headless
    yield record
    chrome.stop(record["state_key"])


@live
def test_every_page_request_and_worker_says_qajev_and_keeps_its_client_hints(site, browser):
    from qajev.ledger import Ledger

    session_mod.configure_env(browser["cdp_url"])
    s = session_mod.Session(Ledger(0.0), headless=False, hosts={f"127.0.0.1:{site}"})
    mark = f" QAJev/{__version__}"
    try:
        s.set_device(DESKTOP)
        s.arm("readonly")
        Handler.seen.clear()
        assert s.navigate(f"http://127.0.0.1:{site}/page.html") is None
        ua = s.evaluate("navigator.userAgent")
        assert ua.endswith(mark) and "Chrome/" in ua
        assert s.evaluate("window.workerUA").endswith(mark)
        assert Handler.seen and all(h.endswith(mark) for h in Handler.seen)  # the first request already said it
        # the client hints are still the browser's own (an override without them leaves no brands)
        assert s.evaluate("navigator.userAgentData.brands.some(b => b.brand === 'Chromium')") is True
        s.set_device(PHONE)
        assert s.navigate(f"http://127.0.0.1:{site}/page.html") is None
        assert s.evaluate("navigator.userAgent") == PHONE["ua"] + mark
        s.set_device(DESKTOP)
        assert s.navigate(f"http://127.0.0.1:{site}/page.html") is None
        assert s.evaluate("navigator.userAgent") == ua
    finally:
        s.close()
        session_mod.stop_daemon()
