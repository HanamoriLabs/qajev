"""The drag hook and expect.input: the view must change more with the test's input than in the same time with none
(29 Oct: a demo's camera did not follow the mouse, and a still frame of each stage could not tell)."""

import http.server
import io
import os
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from PIL import Image
from test_ux_dom_live import ENDPOINT
from test_ux_dom_live import no_model_initialization as no_model_initialization
from test_ux_dom_live import page as page

from qajev import plan, report, report_files, runner, session, suite, verdict

DRAG = {"drag": {"x": 100, "y": 80, "by": {"x": 120, "y": 0}, "ms": 300}}
REGION = {"name": "view", "x": 0, "y": 0, "width": 8, "height": 8}


def _suite(**extra):
    spec = {"name": "the camera follows the mouse", "region": REGION, "do": [DRAG], "min_changed": 0.2, **extra}
    return suite.parse({"devices": ["desktop"], "scenarios": [{"name": "camera", "about": "The mouse turns the view.",
                        "url": "http://127.0.0.1:8877/", "expect": {"input": spec}}]})


def test_a_drag_hook_names_a_start_point_and_a_move():
    hooks = suite.parse({"scenarios": [{"name": "a", "url": "http://127.0.0.1/", "about": "x", "expect": {"text": "a"},
                                        "before": [{"drag": {"selector": "canvas", "x": 0, "y": 5, "by": {"x": -40,
                                                    "y": 3.5}, "ms": 0, "steps": 1, "button": "right"}}]}]})
    assert hooks.scenarios[0].before[0]["drag"]["button"] == "right"
    for bad, says in (({"x": 1, "y": 1}, "must be"), ({"x": -1, "y": 1, "by": {"x": 1, "y": 0}}, "drag.x"),
                      ({"x": 1, "y": 1, "by": {"x": 0, "y": 0}}, "not both 0"),
                      ({"x": 1, "y": 1, "by": {"x": 1}}, "drag.by"),
                      ({"x": 1, "y": 1, "by": {"x": 1, "y": 0}, "ms": 10001}, "drag.ms"),
                      ({"x": 1, "y": 1, "by": {"x": 1, "y": 0}, "steps": 0}, "drag.steps"),
                      ({"x": 1, "y": 1, "by": {"x": 1, "y": 0}, "button": "back"}, "drag.button"),
                      ({"x": 1, "y": 1, "by": {"x": 1, "y": 0}, "selector": " "}, "drag.selector"),
                      ({"x": 1, "y": 1, "by": {"x": 1, "y": 0}, "speed": 2}, "must be")):
        with pytest.raises(suite.SuiteError, match=says):
            suite.parse({"scenarios": [{"name": "a", "url": "http://127.0.0.1/", "about": "x",
                                        "expect": {"text": "a"}, "before": [{"drag": bad}]}]})


@pytest.mark.parametrize("extra,says", [({"do": []}, "at least one input"), ({"min_changed": 0}, "min_changed"),
                                        ({"min_changed": 1.5}, "min_changed"), ({"tolerance": 256}, "tolerance"),
                                        ({"name": ""}, "name"), ({"region": {"name": "v", "x": 0}}, "region"),
                                        ({"do": [{"tap": 1}]}, "do\\[0\\]"), ({"wait": 1}, "wait")])
def test_an_input_check_is_refused_before_the_browser_when_malformed(extra, says):
    with pytest.raises(suite.SuiteError, match=says):
        _suite(**extra)


def test_a_multiplayer_test_cannot_check_input():
    with pytest.raises(suite.SuiteError, match=r"expect: unknown key\(s\) \['input'\]"):
        suite.parse({"scenarios": [{"name": "a", "url": "http://127.0.0.1/", "about": "x", "clients": ["p1", "p2"],
                                    "steps": [{"client": "p1", "js": "1"}],
                                    "expect": {"input": {"name": "n", "region": REGION, "do": [DRAG],
                                                         "min_changed": 0.1}}}]})


def test_an_input_check_is_planned_positive_and_once():
    parsed = _suite()
    expect = parsed.scenarios[0].expect
    assert suite.has_checks(expect) and plan.absent_only(parsed.scenarios) == []
    assert verdict.planned(expect) == [{
        "check": "input 'the camera follows the mouse' changes region 'view'",
        "says": "the camera follows the mouse: with the input, at least 0.2 more of the pixels in region view change "
                "than in the same time with no input, with RGB difference greater than 8"}]


def test_the_no_input_control_waits_as_long_as_the_input_plans_to_take():
    assert runner._input_wait_s([DRAG, {"sleep": 0.5}, {"key": {"press": ["a", "d"], "repeat": 2, "hold_ms": 100}},
                                 {"js": "1"}]) == pytest.approx(0.3 + 0.5 + 0.4)
    assert runner._input_wait_s([{"js": "1"}]) == 0.2
    # a pad's held stick and a react hook's for_s count too (redteam-1 M1, #275)
    assert runner._input_wait_s([{"pad": {"stick": "left", "x": 1, "y": 0, "hold_ms": 2000}},
                                 {"react": {"js": "null", "for_s": 1.5}}]) == pytest.approx(2.0 + 1.5)


def png(dark):
    image = Image.new("RGB", (8, 8), "white")
    for x in range(dark):
        for y in range(8):
            image.putpixel((x, y), (0, 0, 0))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class View:
    """A stand-in tab: its region darkens `idle` more columns by itself in each wait (before the idle-after and the
    input-after captures) and `moved` more columns for the input."""

    def __init__(self, idle, moved):
        self.idle, self.moved, self.columns, self.calls = idle, moved, 0, []

    def set_device(self, *_): pass
    def arm(self, *_): pass
    def check_host(self, *_): pass
    def navigate(self, url): self.url = url
    def probe(self, *_): return {"url": self.url, "status": 200, "probe": {}}

    def run_hook(self, hook, url):
        self.calls.append(hook)
        self.columns += self.moved

    def capture_region_png(self, region):
        self.calls.append("capture")
        if self.calls.count("capture") in (2, 4):
            self.columns += self.idle
        return png(min(self.columns, 8))


@pytest.mark.parametrize("idle,moved,outcome,margin", [(0, 4, "pass", 0.5), (1, 3, "pass", 0.375),
                                                       (2, 0, "fail", 0.0), (2, 1, "fail", 0.125)])
def test_only_the_change_the_input_adds_counts(tmp_path, monkeypatch, idle, moved, outcome, margin):
    parsed = _suite(min_changed=0.25)
    view = View(idle, moved)
    slept = []
    monkeypatch.setattr(runner, "wait_for_quiet", lambda _: (True, 0))
    monkeypatch.setattr(runner.time, "sleep", slept.append)
    result = runner.run_scenario(view, parsed.scenarios[0], opts=runner.Options(shots=False),
                                 hosts={"127.0.0.1:8877"}, run_dir=tmp_path)
    assert result["outcome"] == outcome, result["reason"]
    assert slept == [0.3]  # the no-input control waits as long as the drag takes, between its two captures
    assert view.calls == ["capture", "capture", "capture", DRAG, "capture"]
    metrics = result["input"]["metrics"]
    assert metrics["margin"] == pytest.approx(margin) and metrics["min_changed"] == 0.25
    assert metrics["idle"]["total"] == 64 and metrics["input"]["total"] == 64
    for key in report_files.INPUT_FRAMES:
        assert (tmp_path / result["input"][key]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert sorted(report_files.image_paths({"scenarios": [result]})) == sorted(
        result["input"][key] for key in report_files.INPUT_FRAMES)
    data = report.build(parsed, [result], [], browser={}, started_at=time.time(), strict=False, interrupted=False,
                        run_dir=tmp_path)
    report.write(tmp_path, data)
    for name in ("report.md", "report.html"):
        text = (tmp_path / name).read_text()
        assert all(result["input"][key] in text for key in report_files.INPUT_FRAMES)


class Clock:
    """time.monotonic and time.sleep for the runner: a sleep moves the clock, and so does each input hook."""

    def __init__(self, hook_s):
        self.now, self.hook_s = 100.0, hook_s

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += s


# 0.50 s is the bound for a 0.2 s control (1.25 x 0.2 + 0.25): it is judged, and 0.51 s is not (redteam-1 L1, #278)
@pytest.mark.parametrize("hook_s,outcome", [(0.2, "pass"), (0.45, "pass"), (0.50, "pass"), (0.51, "harness"),
                                            (1.0, "harness")])
def test_an_input_that_takes_longer_than_its_control_is_not_judged(tmp_path, monkeypatch, hook_s, outcome):
    """A click with no planned time gets a 0.2 s control: an input that then runs 1 s has 5 times longer to change by
    itself, so the two changes cannot be compared (redteam-1 M1, #275)."""
    clock = Clock(hook_s)
    view = View(0, 4)
    moves = view.run_hook
    view.run_hook = lambda hook, url: clock.sleep(clock.hook_s) or moves(hook, url)
    monkeypatch.setattr(runner, "wait_for_quiet", lambda _: (True, 0))
    monkeypatch.setattr(runner.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(runner.time, "sleep", clock.sleep)
    result = runner.run_scenario(view, _suite(do=[{"click": "#go"}]).scenarios[0], opts=runner.Options(shots=False),
                                 hosts={"127.0.0.1:8877"}, run_dir=tmp_path)
    assert result["outcome"] == outcome, result["reason"]
    if outcome == "harness":
        assert f"the input took {hook_s:.2f} s and the no-input control 0.20 s, so their changes cannot be compared" \
            in result["reason"]
        assert result["input"]["metrics"]["ok"] is False


def test_a_do_hook_that_fails_stops_the_test_as_a_failed_hook(tmp_path, monkeypatch):
    view = View(0, 4)

    def broken(hook, url):
        raise session.HookFailed("'#go' is covered by div#splash")

    view.run_hook = broken
    monkeypatch.setattr(runner, "wait_for_quiet", lambda _: (True, 0))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    result = runner.run_scenario(view, _suite(do=[{"click": "#go"}]).scenarios[0], opts=runner.Options(shots=False),
                                 hosts={"127.0.0.1:8877"}, run_dir=tmp_path)
    assert result["stop"] == "hook_failed" and result["outcome"] == "harness"
    assert "input 'the camera follows the mouse', the do hooks: '#go' is covered by div#splash" in result["reason"]
    assert "capture" not in result["reason"]


def test_a_capture_that_fails_is_harness_and_names_the_input_check(tmp_path, monkeypatch):
    view = View(0, 4)
    view.capture_region_png = lambda region: (_ for _ in ()).throw(RuntimeError("no frame"))
    monkeypatch.setattr(runner, "wait_for_quiet", lambda _: (True, 0))
    result = runner.run_scenario(view, _suite().scenarios[0], opts=runner.Options(shots=False),
                                 hosts={"127.0.0.1:8877"}, run_dir=tmp_path)
    assert result["outcome"] == "harness"
    assert "input 'the camera follows the mouse', capture 1 of 4: no frame" in result["reason"]


def _tab(at):
    sent = []
    tab = session.Session.__new__(session.Session)
    tab.browser = SimpleNamespace(call=lambda method, **params: sent.append((method, params)) or {})
    tab.crash, tab.net = None, None
    tab.evaluate = lambda expression, timeout_ms=0: at
    return tab, sent


def test_a_drag_presses_moves_in_steps_and_releases_where_it_ends(monkeypatch):
    monkeypatch.setattr(session.time, "sleep", lambda _: None)
    tab, sent = _tab({"x": 10, "y": 20, "covered": False})
    tab.drag({"x": 1, "y": 2, "by": {"x": 30, "y": -9}, "steps": 3, "button": "right"})
    mouse = [(p["type"], p["x"], p["y"], p.get("buttons")) for m, p in sent if m == "Input.dispatchMouseEvent"]
    assert mouse == [("mouseMoved", 10, 20, None), ("mousePressed", 10, 20, 2), ("mouseMoved", 20, 17, 2),
                     ("mouseMoved", 30, 14, 2), ("mouseMoved", 40, 11, 2), ("mouseReleased", 40, 11, 0)]


def test_a_drag_takes_its_ms_however_long_each_move_takes(monkeypatch):
    """Each move waits for the renderer (about a frame in headless Chrome): the pauses take that time off, so the
    drag ends at its ms, as the no-input control of an input check plans (redteam-1 M1, #275)."""
    now, slept = [10.0], []
    monkeypatch.setattr(session.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(session.time, "sleep", lambda s: slept.append(round(s, 6)) or now.__setitem__(0, now[0] + s))
    tab, sent = _tab({"x": 10, "y": 20, "covered": False})
    real = tab.browser.call

    def slow(method, **params):  # every event takes 16 ms
        now[0] += 0.016
        return real(method, **params)

    tab.browser.call = slow
    tab.drag({"x": 1, "y": 2, "by": {"x": 30, "y": 0}, "steps": 3, "ms": 300})
    assert slept == [0.1, 0.084, 0.084]  # 100 ms a move, less the 16 ms the move before it took
    assert now[0] == pytest.approx(10.0 + 2 * 0.016 + 0.3 + 2 * 0.016)  # move and press, 300 ms, last move, release


def test_a_drag_that_breaks_midway_still_lets_go_of_the_button(monkeypatch):
    monkeypatch.setattr(session.time, "sleep", lambda _: None)
    tab, sent = _tab({"x": 10, "y": 20, "covered": False})
    real = tab.browser.call

    def flaky(method, **params):
        if params.get("type") == "mouseMoved" and params.get("buttons"):
            raise RuntimeError("the page went away")
        return real(method, **params)

    tab.browser.call = flaky
    with pytest.raises(RuntimeError):
        tab.drag({"x": 1, "y": 2, "by": {"x": 30, "y": 0}})
    assert sent[-1][1]["type"] == "mouseReleased"


@pytest.mark.parametrize("at,says", [({"error": "no element matches 'canvas'"}, "no element matches"),
                                     ({"x": 1, "y": 1, "covered": True, "over": "div#hud"}, "covered by div#hud")])
def test_a_drag_from_a_missing_or_covered_point_fails_the_hook(at, says):
    tab, sent = _tab(at)
    with pytest.raises(session.HookFailed, match=says):
        tab.drag({"selector": "canvas", "x": 1, "y": 2, "by": {"x": 30, "y": 0}})
    assert sent == []


# A camera over a striped world, with a small marker that moves by itself. ?mouse=0: the camera ignores the mouse (the
# planted fault). ?spin=<px per s>: the whole world also scrolls by itself, so the view changes a lot with no input.
# The page counts the trusted moves it got with the button held, and adds up their movementX.
CAMERA = """<!doctype html><link rel="icon" href="data:,"><style>body{margin:0}</style>
<canvas id="view" width="320" height="200"></canvas><script>
const q = new URLSearchParams(location.search), follow = q.get('mouse') !== '0', spin = Number(q.get('spin') || 0);
const c = document.getElementById('view').getContext('2d');
let cam = 0, down = false, last = 0; window.moves = 0; window.movement = 0;
addEventListener('mousedown', e => { down = e.isTrusted; last = e.clientX; });
addEventListener('mouseup', () => { down = false; });
addEventListener('mousemove', e => {
  if (!down || e.buttons !== 1) return;
  window.moves++; window.movement += e.movementX;
  if (follow) cam += e.clientX - last;
  last = e.clientX;
});
const t0 = performance.now();
const frame = now => {
  const t = (now - t0) / 1000, x = cam + spin * t;
  for (let i = 0; i < 320; i += 4) {
    // stripes that do not repeat: a world of even stripes looks the same after a scroll of whole periods
    const k = Math.abs(Math.floor((i + x) / 20));
    c.fillStyle = (k * k * 7919) % 101 < 50 ? '#222' : '#ddd'; c.fillRect(i, 0, 4, 200);
  }
  c.fillStyle = '#e33'; c.fillRect(150 + 40 * Math.sin(t * 3), 90, 8, 8);
  requestAnimationFrame(frame);
};
requestAnimationFrame(frame);
</script>"""


@pytest.fixture
def camera():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            body = CAMERA.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.skipif(not ENDPOINT or os.uname().sysname != "Linux", reason="requires approved CPU fixture browser")
def test_browser_a_real_drag_turns_a_camera_and_a_camera_that_ignores_it_fails(page, camera, tmp_path, monkeypatch):
    tab, _ = page
    tab.hosts = {urlsplit(camera).netloc}
    monkeypatch.setattr(session, "require_transport", lambda: None)
    spec = {"name": "the camera follows the mouse", "region": {"name": "view", "x": 0, "y": 0, "width": 320,
            "height": 200}, "do": [{"drag": {"selector": "#view", "x": 60, "y": 100, "by": {"x": 150, "y": 0},
                                             "ms": 300, "steps": 15}}], "min_changed": 0.2}
    scenario = suite.parse({"devices": ["desktop"], "scenarios": [{"name": "camera", "about": "x", "url": camera,
                                                                   "expect": {"input": spec}}]}).scenarios[0]
    outcome = {}
    for query in ("?follow", "?mouse=0", "?mouse=0&spin=400"):
        tab.navigate(camera + "/" + query)
        check, evidence = runner._input_check(tab, scenario, tmp_path / query[1:].replace("&", "-"), camera)
        metrics = evidence["metrics"]
        outcome[query] = (check["ok"], round(metrics["idle"]["fraction"], 3), round(metrics["input"]["fraction"], 3),
                          tab.evaluate("window.moves"))
    assert outcome["?follow"][0] is True, outcome
    # The planted faults: a camera that ignores the mouse, and one whose world scrolls by itself as much as a drag
    # would move it (a check of the change alone passes there).
    assert outcome["?mouse=0"][0] is False and outcome["?mouse=0&spin=400"][0] is False, outcome
    assert outcome["?mouse=0&spin=400"][2] >= 0.2, outcome  # the view did change a lot: only the margin fails it
    assert all(moves == 15 for *_, moves in outcome.values()), outcome  # every move arrived, button held


@pytest.mark.skipif(not ENDPOINT or os.uname().sysname != "Linux", reason="requires approved CPU fixture browser")
def test_browser_a_drag_gives_the_page_movementX_in_its_direction(page, camera, monkeypatch):
    """A camera under pointer lock reads only movementX/Y, so QAJev's moves must carry it. Chrome sets it on
    synthetic moves, though not always the full CSS distance (60 px for a 150 px drag in headless Chromium 153)."""
    tab, _ = page
    tab.hosts = {urlsplit(camera).netloc}
    monkeypatch.setattr(session, "require_transport", lambda: None)
    tab.navigate(camera + "/")
    tab.drag({"selector": "#view", "x": 60, "y": 100, "by": {"x": 150, "y": 0}, "ms": 150, "steps": 10})
    moves, movement = tab.evaluate("[window.moves, window.movement]")
    assert moves == 10 and 0 < movement <= 150, movement
    tab.drag({"selector": "#view", "x": 200, "y": 100, "by": {"x": -150, "y": 0}, "ms": 150, "steps": 10})
    assert tab.evaluate("window.movement") < movement  # back the other way
