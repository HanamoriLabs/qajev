"""click_at: a real mouse click at a point in an element, such as a WebGL canvas in an iframe (verse-games, 12 Oct).

`click: <selector>` clicks an element's center, and what sits at a 3D view's center depends on the camera. click_at
takes CSS pixels from the element's top-left corner (or viewport pixels without a selector), sends the same trusted
mouse events, and fails when something else covers that point.

The live half runs in QAJev's own throwaway Chrome in a visible window: never headless. The fixture page frames a
same-origin page whose canvas records where it was clicked.
"""

import http.server
import os
import threading
from functools import partial

import pytest

from qajev.suite import SuiteError, _hooks

live = pytest.mark.skipif(os.environ.get("QAJEV_LIVE") != "1", reason="set QAJEV_LIVE=1 (starts a local Chrome)")


# ---- the step, checked before the run ----

def test_a_click_at_step_takes_a_selector_and_a_point():
    assert _hooks([{"click_at": {"selector": "iframe#view", "x": 120, "y": 80.5}}], "before") == [
        {"click_at": {"selector": "iframe#view", "x": 120, "y": 80.5}}]
    assert _hooks([{"click_at": {"x": 10, "y": 20}}], "before")  # viewport pixels
    assert _hooks([{"click_at": {"selector": "iframe", "js": "window.bench0()"}}], "before")  # found at run time


@pytest.mark.parametrize("value", [{"selector": "canvas", "x": 1}, {"x": "10", "y": 2}, {"x": -1, "y": 2},
                                   {"x": True, "y": 2}, {"x": 1, "y": 2, "button": "right"}, "canvas", [1, 2],
                                   {"js": "p()", "x": 1, "y": 2}, {"js": 3}, {"js": " "}])
def test_a_bad_click_at_step_is_refused_before_the_run(value):
    with pytest.raises(SuiteError, match="click_at"):
        _hooks([{"click_at": value}], "before")


# ---- live: the click lands at that point in the frame's canvas ----

class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


@pytest.fixture(scope="module")
def port(tmp_path_factory):
    root = tmp_path_factory.mktemp("click-at")
    (root / "page.html").write_text(
        "<!doctype html><title>workshop</title><style>body{margin:0}"
        "iframe{position:absolute;left:40px;top:30px;width:300px;height:200px;border:0}"
        "#panel{position:absolute;left:240px;top:30px;width:100px;height:200px;background:#eee}</style>"
        "<iframe id=view src='frame.html'></iframe><div id=panel>outliner</div>"
        "<script>window.bench0 = async () => ({x: 33, y: 44}); window.nowhere = () => ({x: 'a'});</script>")
    (root / "frame.html").write_text(
        "<!doctype html><style>body{margin:0}canvas{display:block}</style><canvas width=300 height=200></canvas>"
        "<script>window.clicks = []; document.querySelector('canvas').addEventListener('pointerdown', (e) =>"
        " window.clicks.push({x: e.clientX, y: e.clientY, trusted: e.isTrusted}));</script>")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    from qajev import chrome

    record = chrome.start(f"click-at-{os.getpid()}", headless=False, ephemeral=True)
    yield record
    chrome.stop(record["state_key"])


@pytest.fixture
def page(port, browser):
    from qajev import session as session_mod
    from qajev.ledger import Ledger

    session_mod.configure_env(browser["cdp_url"])
    s = session_mod.Session(Ledger(0.0), headless=False, hosts={f"127.0.0.1:{port}"})
    try:
        s.set_device({"width": 1024, "height": 700, "mobile": False, "scale": 1})
        s.arm("readonly")
        url = f"http://127.0.0.1:{port}/page.html"
        assert s.navigate(url) is None
        yield s, url
    finally:
        s.close()
        session_mod.stop_daemon()


CLICKS = "document.getElementById('view').contentWindow.clicks"


@live
def test_click_at_lands_at_that_point_in_the_frames_canvas_as_a_real_click(page):
    s, url = page
    s.run_hook({"click_at": {"selector": "iframe#view", "x": 120, "y": 80}}, url)
    s.run_hook({"click_at": {"x": 50, "y": 40}}, url)  # viewport pixels: 10, 10 inside the frame
    assert s.evaluate(CLICKS) == [{"x": 120, "y": 80, "trusted": True}, {"x": 10, "y": 10, "trusted": True}]


@live
def test_click_at_a_point_something_covers_fails_and_names_it(page):
    from qajev.session import HookFailed

    s, url = page
    with pytest.raises(HookFailed, match="covered by div#panel"):
        s.run_hook({"click_at": {"selector": "iframe#view", "x": 250, "y": 80}}, url)  # under the outliner panel
    with pytest.raises(HookFailed, match="outside"):
        s.run_hook({"click_at": {"selector": "iframe#view", "x": 400, "y": 80}}, url)  # past the frame's edge
    assert s.evaluate(CLICKS) == []


@live
def test_click_at_takes_its_point_from_a_js_expression_at_run_time(page):
    s, url = page
    s.run_hook({"click_at": {"selector": "iframe#view", "js": "window.bench0()"}}, url)
    assert s.evaluate(CLICKS) == [{"x": 33, "y": 44, "trusted": True}]


@live
def test_click_at_fails_when_its_js_does_not_return_a_point(page):
    from qajev.session import HookFailed

    s, url = page
    with pytest.raises(HookFailed, match="two numbers"):
        s.run_hook({"click_at": {"selector": "iframe#view", "js": "window.nowhere()"}}, url)
    assert s.evaluate(CLICKS) == []
