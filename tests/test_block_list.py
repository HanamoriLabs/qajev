"""What a page tried to send to a blocked host, with its method, from every frame (verse-workshop, 12 Oct 2026).

A readonly run with block_urls must prove a framed tool never saved on load. In 0.4.8 a GET to a blocked host was
listed nowhere, and a POST that the read-only guard stopped inside a frame on another origin never reached the report:
QAJev read the guard's record from the top page only.

The live half runs in QAJev's own throwaway Chrome, in a visible window (never headless): the page on one 127.0.0.1
port frames a page on another port (another origin, the same site, so the frame shares the page's target), and that
frame calls a third port that
block_urls names. A second page frames the same page under the name tool.test, which this Chrome maps to 127.0.0.1:
another site, so Chrome runs that frame as a target of its own, which 0.4.9 does not read (0.4.10 says so). Nothing
leaves the machine.
"""

import http.server
import os
import threading
import time
from functools import partial

import pytest

from qajev import netlog

live = pytest.mark.skipif(os.environ.get("QAJEV_LIVE") != "1", reason="set QAJEV_LIVE=1 (starts a local Chrome)")


# ---- Chrome's own match for Network.setBlockedURLs, and the list kept from the network events ----

@pytest.mark.parametrize(("pattern", "url", "hit"), [
    ("127.0.0.1:8796", "http://127.0.0.1:8796/adjust/body.json", True),  # no wildcard: anywhere in the address
    ("http://127.0.0.1:8796/*", "http://127.0.0.1:8796/adjust/body.json", True),
    ("*://*/logout*", "https://shop.example.com/account/logout?next=/", True),
    ("*://*/logout*", "https://shop.example.com/login", False),
    ("127.0.0.1:8796", "http://127.0.0.1:8797/adjust/body.json", False),
    ("*adjust*json", "http://127.0.0.1:8796/adjust/body.json", True),
    ("*json*adjust", "http://127.0.0.1:8796/adjust/body.json", False),  # the parts in order
])
def test_a_block_pattern_matches_as_chrome_matches_it(pattern, url, hit):
    assert netlog.blocks(url, [pattern]) is hit


def sent(rid, url, method="GET", document="http://127.0.0.1:8795/rig.html"):
    return {"method": "Network.requestWillBeSent",
            "params": {"requestId": rid, "request": {"url": url, "method": method}, "documentURL": document}}


def failed(rid, reason=None, error="net::ERR_BLOCKED_BY_CLIENT"):
    params = {"requestId": rid, "errorText": error}
    if reason:
        params["blockedReason"] = reason
    return {"method": "Network.loadingFailed", "params": params}


def test_every_request_to_a_blocked_host_is_listed_with_its_method_frame_and_whether_chrome_blocked_it():
    log = netlog.Failures("ws://unused", block_urls=["127.0.0.1:8796"])
    log.take(sent("1", "http://127.0.0.1:8796/adjust/body.json"))
    log.take(failed("1", "inspector"))
    log.take(sent("2", "http://127.0.0.1:8798/other.json"))  # not on the block list: not listed
    log.take(sent("3", "http://127.0.0.1:8796/adjust/body.json", method="PUT"))
    assert log.take_block_list() == [
        {"method": "GET", "url": "http://127.0.0.1:8796/adjust/body.json", "frame": "http://127.0.0.1:8795/rig.html",
         "blocked": True},
        {"method": "PUT", "url": "http://127.0.0.1:8796/adjust/body.json", "frame": "http://127.0.0.1:8795/rig.html",
         "blocked": False},  # Chrome has not said it blocked this one
    ]
    assert log.take_block_list() == []  # taken once, per test


def test_a_request_chrome_blocked_is_listed_even_when_the_pattern_reads_differently_here():
    log = netlog.Failures("ws://unused", block_urls=["some-pattern-chrome-reads-its-own-way"])
    log.take(sent("7", "http://127.0.0.1:8797/save", method="POST"))
    log.take(failed("7", "inspector"))
    assert log.take_block_list() == [{"method": "POST", "url": "http://127.0.0.1:8797/save",
                                      "frame": "http://127.0.0.1:8795/rig.html", "blocked": True}]


def test_without_block_urls_nothing_is_listed_and_failures_still_explain():
    log = netlog.Failures("ws://unused")
    log.take(sent("1", "http://127.0.0.1:8796/a.png"))
    log.take(failed("1", "inspector"))
    assert log.take_block_list() == []
    assert log.reason("http://127.0.0.1:8796/a.png", wait=0) == "net::ERR_BLOCKED_BY_CLIENT, blocked: inspector"


# ---- live: a framed tool on another origin calls a blocked host on load ----

CROSS_SITE = "tool.test"  # mapped to 127.0.0.1 in this Chrome: another site than the page's 127.0.0.1


class Recorder(http.server.SimpleHTTPRequestHandler):
    """Serves the fixture pages, and records every request that reaches it."""

    seen = []

    def log_message(self, *_):
        pass

    def _any(self):
        Recorder.seen.append((self.server.server_address[1], self.command, self.path))
        if self.command == "GET":
            return super().do_GET()
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    do_GET = do_POST = do_PUT = _any


@pytest.fixture(scope="module")
def ports(tmp_path_factory):
    root = tmp_path_factory.mktemp("block-list")
    servers = [http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Recorder, directory=str(root)))
               for _ in range(3)]
    page, frame, blocked = (s.server_address[1] for s in servers)
    for name, host in (("page.html", "127.0.0.1"), ("cross.html", CROSS_SITE)):
        (root / name).write_text(
            "<!doctype html><title>page</title><p>Workshop</p>"
            "<script>addEventListener('message', (e) => { if (e.data === 'frame-done') window.frameDone = true; });"
            "</script>"
            f"<iframe src='http://{host}:{frame}/frame.html?k=secret'></iframe>")
    (root / "frame.html").write_text(
        "<!doctype html><title>rig</title><script>(async () => {"
        f"  const url = 'http://127.0.0.1:{blocked}/adjust/body.json';"
        "  try { await fetch(url); } catch (e) {}"
        "  try { await fetch(url, { method: 'POST', body: '{}' }); } catch (e) {}"
        "  parent.postMessage('frame-done', '*');"
        "})();</script>")
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield page, frame, blocked
    for s in servers:
        s.shutdown()


@pytest.fixture(scope="module")
def browser():
    from qajev import chrome

    old = os.environ.get("QAJEV_CHROME_FLAGS")
    os.environ["QAJEV_CHROME_FLAGS"] = f"{old or ''} '--host-resolver-rules=MAP {CROSS_SITE} 127.0.0.1'".strip()
    try:
        record = chrome.start(f"block-list-{os.getpid()}", headless=False, ephemeral=True)  # visible, never headless
    finally:
        if old is None:
            os.environ.pop("QAJEV_CHROME_FLAGS", None)
        else:
            os.environ["QAJEV_CHROME_FLAGS"] = old
    yield record
    chrome.stop(record["state_key"])


def loaded(ports, browser, page_name):
    """Open `page_name` in a readonly session with the third port on block_urls; wait for its frame's calls; the
    session and its probe."""
    from qajev import session as session_mod
    from qajev.ledger import Ledger

    page, _, blocked = ports
    session_mod.configure_env(browser["cdp_url"])
    s = session_mod.Session(Ledger(0.0), headless=False, hosts={f"127.0.0.1:{page}"},
                            guard_opts={"block_urls": [f"http://127.0.0.1:{blocked}/*"]})
    s.set_device({"width": 1280, "height": 900, "mobile": False, "scale": 1})
    s.arm("readonly")
    s.navigate(f"http://127.0.0.1:{page}/{page_name}")
    deadline = time.monotonic() + 10
    while not s.evaluate("window.frameDone === true") and time.monotonic() < deadline:
        time.sleep(0.1)
    assert s.evaluate("window.frameDone === true"), "the framed page never finished its calls"
    time.sleep(0.3)  # Chrome's loadingFailed for the blocked GET can trail the page's own rejection
    return s, s.probe({})["probe"]


@pytest.fixture
def probe_after_load(ports, browser):
    from qajev import session as session_mod

    s = None
    try:
        s, probe = loaded(ports, browser, "page.html")
        yield probe, ports
    finally:
        if s is not None:
            s.close()
        session_mod.stop_daemon()


@pytest.fixture
def cross_site_probe(ports, browser):
    from qajev import session as session_mod

    s = None
    try:
        s, probe = loaded(ports, browser, "cross.html")
        yield probe, ports
    finally:
        if s is not None:
            s.close()
        session_mod.stop_daemon()


@live
def test_a_post_the_guard_stops_inside_a_frame_on_another_origin_reaches_the_report(probe_after_load):
    probe, (page, frame, blocked) = probe_after_load
    posts = [b for b in probe.get("blocked") or [] if b["method"] == "POST"]
    assert posts == [{"method": "POST", "url": f"http://127.0.0.1:{blocked}/adjust/body.json",
                      "frame": f"http://127.0.0.1:{frame}/frame.html?k=secret"}], probe.get("blocked")
    assert not [r for r in Recorder.seen if r[0] == blocked], "a request reached the blocked host"


@live
def test_a_get_to_a_blocked_host_is_listed_with_its_method_frame_and_blocked(probe_after_load):
    probe, (page, frame, blocked) = probe_after_load
    assert probe.get("block_list") == [{"method": "GET", "url": f"http://127.0.0.1:{blocked}/adjust/body.json",
                                        "frame": f"http://127.0.0.1:{frame}/frame.html?k=secret", "blocked": True}]
    assert not probe.get("frames_not_observed")  # a same-site frame is read


@live
def test_a_frame_on_another_site_is_named_as_not_observed_not_shown_as_an_empty_list(cross_site_probe):
    probe, (page, frame, blocked) = cross_site_probe
    assert probe.get("frames_not_observed") == [f"http://{CROSS_SITE}:{frame}/frame.html?k=secret"], probe


# ---- the reports: each request on its own line, its frame named, no query string ----

def test_the_reports_list_each_block_list_request_with_its_frame(tmp_path):
    import json
    import time
    from types import SimpleNamespace

    from qajev import report

    listed = [{"method": "GET", "url": "http://127.0.0.1:8796/adjust/body.json?token=secret",
               "frame": "http://127.0.0.1:8795/rig.html?key=secret", "blocked": True},
              *({"method": "GET", "url": f"http://127.0.0.1:8797/a{i}.png", "frame": "", "blocked": False}
                for i in range(25))]
    results = [{"name": "rig", "outcome": "pass", "reason": "", "seconds": 1.0, "checks": [], "findings": [],
                "screens": [], "end_url": "http://127.0.0.1:8898/", "block_list_requests": listed}]
    ledger = {"usd": 0.0, "usd_typesafe_estimated": 0.0, "usd_text": 0.0, "calls": {"typesafe": 0, "text": 0},
              "tokens": {"typesafe": 0, "text": 0}, "errors": 0, "text_cost_reported": False, "cap_usd": 1.0}
    data = report.build(SimpleNamespace(name="rig"), results, [ledger], browser={"cdp_url": "http://127.0.0.1:9350"},
                        started_at=time.time(), strict=False, interrupted=False, run_dir=tmp_path)
    report.write(tmp_path, data)
    md, html = (tmp_path / "report.md").read_text(), (tmp_path / "report.html").read_text()
    for text in (md, html):
        assert "Block list: GET 127.0.0.1:8796/adjust/body.json, blocked, from 127.0.0.1:8795/rig.html" in text
        assert "Block list: GET 127.0.0.1:8797/a0.png, not blocked" in text
        assert "Block list: 6 more in report.json" in text  # 26 listed, 20 shown
        assert "secret" not in text
    assert json.loads((tmp_path / "report.json").read_text())["scenarios"][0]["block_list_requests"] == listed


def test_the_reports_name_each_frame_not_observed_without_its_query(tmp_path):
    import json
    import time
    from types import SimpleNamespace

    from qajev import report

    results = [{"name": "rig", "outcome": "pass", "reason": "", "seconds": 1.0, "checks": [], "findings": [],
                "screens": [], "end_url": "http://127.0.0.1:8898/", "block_list_requests": [],
                "frames_not_observed": ["http://tool.test:8795/rig.html?key=secret"]}]
    ledger = {"usd": 0.0, "usd_typesafe_estimated": 0.0, "usd_text": 0.0, "calls": {"typesafe": 0, "text": 0},
              "tokens": {"typesafe": 0, "text": 0}, "errors": 0, "text_cost_reported": False, "cap_usd": 1.0}
    data = report.build(SimpleNamespace(name="rig"), results, [ledger], browser={"cdp_url": "http://127.0.0.1:9350"},
                        started_at=time.time(), strict=False, interrupted=False, run_dir=tmp_path)
    report.write(tmp_path, data)
    import html

    for name in ("report.md", "report.html"):
        text = html.unescape((tmp_path / name).read_text())
        assert ("Frame not observed: tool.test:8795/rig.html. QAJev's read-only guard and block_urls do not reach it, "
                "so it may send writes") in text, name
        assert "secret" not in text
    got = json.loads((tmp_path / "report.json").read_text())["scenarios"][0]
    assert got["frames_not_observed"] == ["http://tool.test:8795/rig.html?key=secret"]


@live
@pytest.mark.xfail(strict=True, reason="known gap until 0.4.11: a frame on another site is its own target, and QAJev's "
                                       "guard and block_urls do not reach it (12 Oct 2026)")
def test_a_frame_on_another_site_still_cannot_reach_a_blocked_host(cross_site_probe):
    probe, (page, frame, blocked) = cross_site_probe
    assert not [r for r in Recorder.seen if r[0] == blocked], Recorder.seen


# ---- until such frames are guarded, a run with one is never proven read-only ----

@pytest.mark.parametrize("outcome", ["pass", "fail", "stuck"])
def test_a_run_with_a_frame_on_another_site_is_harness_naming_the_frame(outcome):
    from qajev import verdict

    got = verdict.unguarded(outcome, "all checks passed", ["http://tool.test:8795/rig.html?key=secret"])
    assert got[0] == "harness"
    assert got[1].startswith("not proven read-only: QAJev's read-only guard and block_urls do not reach the frame on "
                             "another site (tool.test:8795/rig.html), so it may send writes; all checks passed")
    assert "secret" not in got[1]


def test_a_run_without_such_a_frame_or_a_skipped_one_keeps_its_outcome():
    from qajev import verdict

    assert verdict.unguarded("pass", "ok", []) == ("pass", "ok")
    assert verdict.unguarded("pass", "ok", None) == ("pass", "ok")
    assert verdict.unguarded("skipped", "machine busy", ["http://tool.test/"]) == ("skipped", "machine busy")
