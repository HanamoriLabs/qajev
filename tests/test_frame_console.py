"""Each frame's console messages and its pending or failed requests, in the report (verse-workshop, 12 Oct 2026).

A framed tool stayed on "loading…" for 40 s, and the report could not say whether it waited on a request or on its
own drawing. Now each test lists what its frames said (console log, info, warn and error, exceptions, rejections) and
their requests that failed or were still waiting, each with the frame's address.

The live half runs in QAJev's own throwaway Chrome, in a visible window (never headless): the page frames a same-site
page and a page on another site (tool.test, mapped to 127.0.0.1). Each frame logs, asks a server that never answers,
and asks a port nobody listens on. Nothing leaves the machine.
"""

import http.server
import os
import socket
import threading
import time
from functools import partial

import pytest

live = pytest.mark.skipif(os.environ.get("QAJEV_LIVE") != "1", reason="set QAJEV_LIVE=1 (starts a local Chrome)")

CROSS_SITE = "tool.test"


class Server(http.server.SimpleHTTPRequestHandler):
    """Serves the fixture; /hang never answers within a test."""

    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path.startswith("/hang"):
            time.sleep(30)
            return
        super().do_GET()


def closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


FRAME = """<!doctype html><title>rig</title><script>
console.log('rig: loading start');
console.info('rig: info line');
console.warn('rig: no GPU adapter yet');
fetch('/hang?asset=body.glb').catch(() => {});
fetch('http://127.0.0.1:{closed}/adjust.json').catch(() => {});
Promise.reject(new Error('rig: loader gave up'));
setTimeout(() => parent.postMessage('frame-done', '*'), 300);
</script>"""


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("frame-console")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Server, directory=str(root)))
    port, closed = server.server_address[1], closed_port()
    (root / "frame.html").write_text(FRAME.replace("{closed}", str(closed)))
    for name, host in (("page.html", "127.0.0.1"), ("cross.html", CROSS_SITE)):
        (root / name).write_text(
            "<!doctype html><title>workshop</title><p>Workshop</p><script>window.done = 0;"
            "addEventListener('message', (e) => { if (e.data === 'frame-done') window.done++; });</script>"
            f"<iframe src='http://{host}:{port}/frame.html?k=secret'></iframe>")
    server.daemon_threads = True  # a /hang request must not hold the shutdown
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield port, closed
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    from qajev import chrome

    old = os.environ.get("QAJEV_CHROME_FLAGS")
    os.environ["QAJEV_CHROME_FLAGS"] = f"{old or ''} '--host-resolver-rules=MAP {CROSS_SITE} 127.0.0.1'".strip()
    try:
        record = chrome.start(f"frame-console-{os.getpid()}", headless=False, ephemeral=True)  # visible, never headless
    finally:
        if old is None:
            os.environ.pop("QAJEV_CHROME_FLAGS", None)
        else:
            os.environ["QAJEV_CHROME_FLAGS"] = old
    yield record
    chrome.stop(record["state_key"])


def probe_after(site, browser, page_name):
    from qajev import session as session_mod
    from qajev.ledger import Ledger

    port, _ = site
    session_mod.configure_env(browser["cdp_url"])
    s = session_mod.Session(Ledger(0.0), headless=False, hosts={f"127.0.0.1:{port}"})
    try:
        s.set_device({"width": 1280, "height": 900, "mobile": False, "scale": 1})
        s.arm("readonly")
        assert s.navigate(f"http://127.0.0.1:{port}/{page_name}") is None
        deadline = time.monotonic() + 10
        while s.evaluate("window.done") < 1 and time.monotonic() < deadline:
            time.sleep(0.1)
        time.sleep(1.2)  # the hanging request is pending for more than a second
        return s.probe({})["probe"]
    finally:
        s.close()
        session_mod.stop_daemon()


def check(probe, frame, closed):
    said = {(c["level"], c["text"]) for c in probe.get("frame_console") or []}
    assert ("log", "rig: loading start") in said, probe.get("frame_console")
    assert ("info", "rig: info line") in said and ("warn", "rig: no GPU adapter yet") in said
    assert any(level == "rejection" and "rig: loader gave up" in text for level, text in said), said
    assert {c["frame"] for c in probe["frame_console"]} == {frame}
    asked = {(r["method"], r["url"].split("?")[0], r["state"].split()[0].rstrip(":"))
             for r in probe.get("frame_requests") or []}
    assert ("GET", f"{frame.rsplit('/', 1)[0]}/hang", "pending") in asked, probe.get("frame_requests")
    assert ("GET", f"http://127.0.0.1:{closed}/adjust.json", "failed") in asked, probe.get("frame_requests")
    assert {r["frame"] for r in probe["frame_requests"]} == {frame}


@live
def test_a_same_site_frames_console_and_its_pending_and_failed_requests_reach_the_probe(site, browser):
    port, closed = site
    check(probe_after(site, browser, "page.html"), f"http://127.0.0.1:{port}/frame.html?k=secret", closed)


@live
def test_a_frame_on_another_site_is_read_the_same_way(site, browser):
    port, closed = site
    check(probe_after(site, browser, "cross.html"), f"http://{CROSS_SITE}:{port}/frame.html?k=secret", closed)


# ---- the reports ----

def test_the_reports_list_the_frames_console_and_requests_without_query_strings(tmp_path):
    import html
    import json
    from types import SimpleNamespace

    from qajev import report

    frame = "http://127.0.0.1:8795/rig.html?key=secret"
    results = [{"name": "rig", "outcome": "fail", "reason": "loading never ended", "seconds": 40.0, "checks": [],
                "findings": [], "screens": [], "end_url": "http://127.0.0.1:8898/",
                "frame_console": [{"level": "warn", "text": "rig: no GPU adapter yet", "frame": frame}],
                "frame_requests": [{"method": "GET", "url": "http://127.0.0.1:8795/body.glb?sig=secret",
                                    "frame": frame, "state": "pending 38 s"},
                                   {"method": "GET", "url": "http://127.0.0.1:8796/adjust.json", "frame": frame,
                                    "state": "failed: net::ERR_CONNECTION_REFUSED"}]}]
    ledger = {"usd": 0.0, "usd_typesafe_estimated": 0.0, "usd_text": 0.0, "calls": {"typesafe": 0, "text": 0},
              "tokens": {"typesafe": 0, "text": 0}, "errors": 0, "text_cost_reported": False, "cap_usd": 1.0}
    data = report.build(SimpleNamespace(name="rig"), results, [ledger], browser={"cdp_url": "http://127.0.0.1:9350"},
                        started_at=time.time(), strict=False, interrupted=False, run_dir=tmp_path)
    report.write(tmp_path, data)
    for name in ("report.md", "report.html"):
        text = html.unescape((tmp_path / name).read_text())
        assert "Frame console (127.0.0.1:8795/rig.html): warn: rig: no GPU adapter yet" in text, name
        assert "Frame request: GET 127.0.0.1:8795/body.glb, pending 38 s, from 127.0.0.1:8795/rig.html" in text, name
        assert ("Frame request: GET 127.0.0.1:8796/adjust.json, failed: net::ERR_CONNECTION_REFUSED, from "
                "127.0.0.1:8795/rig.html") in text, name
        assert "secret" not in text
    got = json.loads((tmp_path / "report.json").read_text())["scenarios"][0]
    assert got["frame_console"][0]["level"] == "warn" and len(got["frame_requests"]) == 2
