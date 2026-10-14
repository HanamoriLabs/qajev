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


# ---- a real Session's setup marks its tab, before anything else can load (redteam on #151) ----

def new_session(monkeypatch, version, hints=None):
    """Session.__init__ itself, on a fake browser: what it sends to its tab and to the browser."""
    tab, sent = [], []

    def cdp(method, **params):
        sent.append(method)
        if method == "Browser.getVersion":
            if isinstance(version, Exception):
                raise version
            return version
        return {}

    browser = SimpleNamespace(call=lambda method, **params: tab.append((method, params)) or {})
    jev = SimpleNamespace(cdp=cdp, browser=SimpleNamespace(MARKER="m"),
                          agent=SimpleNamespace(Agent=lambda url, task: SimpleNamespace(browser=browser)))
    hints = hints or {"brands": [{"brand": "Chromium", "version": "141"}], "mobile": False, "platform": "macOS"}
    monkeypatch.setattr(session_mod, "load", lambda ledger: jev)
    monkeypatch.setattr(session_mod, "client_hints", lambda cdp, cdp_url: hints)
    monkeypatch.setattr(session_mod.netlog, "start", lambda *a: None)
    for name in ("_keep_safe", "minimize", "page_socket"):
        monkeypatch.setattr(session_mod.Session, name, lambda self: None)
    s = session_mod.Session(None, headless=True)
    return s, tab, hints


def test_a_new_session_marks_its_tab_with_the_browsers_own_agent_and_hints(monkeypatch):
    s, tab, hints = new_session(monkeypatch, {"product": "Chrome/141.0", "userAgent": CHROME})
    marked = {"userAgent": f"{CHROME} QAJev/{__version__}", "userAgentMetadata": hints}
    assert [p for m, p in tab if m == "Emulation.setUserAgentOverride"] == [marked]
    assert s.user_agent == {"user_agent": marked["userAgent"], "hints": hints}  # what set_device restores


def test_a_browser_that_does_not_say_its_agent_keeps_it_unmarked(monkeypatch):
    s, tab, _ = new_session(monkeypatch, RuntimeError("no Browser.getVersion"))
    assert not [m for m, _ in tab if m == "Emulation.setUserAgentOverride"] and s.user_agent is None


# ---- headless Chrome says it is a normal Chrome: some sites refuse "HeadlessChrome" (QAJev Cloud, 14 Oct) ----

HEADLESS = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/155.0.0.0 " \
           "Safari/537.36"
LINUX_CHROME = HEADLESS.replace("HeadlessChrome/", "Chrome/")


def headless_hints():
    return {"brands": [{"brand": "Chromium", "version": "155"}, {"brand": "HeadlessChrome", "version": "155"},
                       {"brand": "Not)A;Brand", "version": "24"}],
            "fullVersionList": [{"brand": "Chromium", "version": "155.0.7000.1"},
                                {"brand": "HeadlessChrome", "version": "155.0.7000.1"},
                                {"brand": "Not)A;Brand", "version": "24.0.0.0"}],
            "mobile": False, "platform": "Linux"}


def test_headless_chrome_says_chrome_with_the_mark_and_google_chromes_hints(monkeypatch):
    read = headless_hints()
    s, tab, _ = new_session(monkeypatch, {"product": "HeadlessChrome/155.0.7000.1", "userAgent": HEADLESS}, read)
    mine = [p for m, p in tab if m == "Emulation.setUserAgentOverride"]
    assert [p["userAgent"] for p in mine] == [f"{LINUX_CHROME} QAJev/{__version__}"]
    hints = mine[0]["userAgentMetadata"]
    assert [b["brand"] for b in hints["brands"]] == ["Chromium", "Google Chrome", "Not)A;Brand"]
    assert [b["brand"] for b in hints["fullVersionList"]] == ["Chromium", "Google Chrome", "Not)A;Brand"]
    assert hints["fullVersionList"][1]["version"] == "155.0.7000.1" and hints["platform"] == "Linux"
    assert read == headless_hints()  # the kept copy for this Chrome is not changed
    assert s.headless  # QAJev still knows it is headless: only what pages are told changes


def test_a_desktop_after_a_phone_still_says_chrome(monkeypatch):
    s = fake_session(monkeypatch, headless_hints())
    s._own_user_agent(HEADLESS)
    s.set_device(PHONE)
    s.set_device(DESKTOP)
    said = [p["userAgent"] for p in overrides(s)]
    assert said[0] == said[2] == f"{LINUX_CHROME} QAJev/{__version__}"
    assert not [a for a in said if "Headless" in a]


# ---- client_hints: read once per Chrome, on its own loopback page, in a tab that always closes ----

HINTS = {"brands": [{"brand": "Chromium", "version": "141"}], "mobile": False, "platform": "Linux"}


def fake_cdp(answer):
    calls = []

    def cdp(method, session_id=None, **params):
        calls.append((method, params))
        if method == "Target.createTarget":
            return {"targetId": "t1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "s1"}
        if method == "Runtime.evaluate":
            if isinstance(answer, Exception):
                raise answer
            return {"result": {"value": answer}}
        return {}

    return cdp, calls


def test_hints_are_read_on_chromes_own_page_once_and_the_tab_closes(monkeypatch):
    monkeypatch.setattr(session_mod, "_HINTS", {})
    cdp, calls = fake_cdp(HINTS)
    assert session_mod.client_hints(cdp, "http://127.0.0.1:9333") == HINTS
    assert ("Target.createTarget", {"url": "http://127.0.0.1:9333/json/version", "background": True}) in calls
    assert ("Target.closeTarget", {"targetId": "t1"}) in calls
    calls.clear()
    assert session_mod.client_hints(cdp, "http://127.0.0.1:9333") == HINTS and calls == []  # kept: no second tab


def test_a_failed_read_closes_the_tab_and_is_tried_again_next_time(monkeypatch):
    monkeypatch.setattr(session_mod, "_HINTS", {})
    cdp, calls = fake_cdp(RuntimeError("page crashed"))
    assert session_mod.client_hints(cdp, "http://127.0.0.1:9333") is None
    assert ("Target.closeTarget", {"targetId": "t1"}) in calls
    calls.clear()
    session_mod.client_hints(cdp, "http://127.0.0.1:9333")
    assert calls and calls[0][0] == "Target.createTarget"  # not kept as "none"


def test_a_chrome_that_is_not_on_this_machine_opens_no_tab(monkeypatch):
    monkeypatch.setattr(session_mod, "_HINTS", {})
    cdp, calls = fake_cdp(HINTS)
    assert session_mod.client_hints(cdp, "http://box.example:9222") is None and calls == []


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
