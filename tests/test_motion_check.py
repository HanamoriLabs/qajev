"""expect.motion: a sequence of frames, not one still frame (29 Oct: a demo passed on one still frame per stage; a
person saw ghost trails, and a collapse that never played)."""

import http.server
import io
import os
import threading
import time
from urllib.parse import urlsplit

import pytest
from PIL import Image
from test_ux_dom_live import ENDPOINT
from test_ux_dom_live import no_model_initialization as no_model_initialization
from test_ux_dom_live import page as page

from qajev import pixels, plan, report, report_files, runner, session, suite, verdict, vision

REGION = {"name": "view", "x": 0, "y": 0, "width": 8, "height": 8}


def _suite(**extra):
    spec = {"name": "the collapse plays", "region": REGION, **extra}
    return suite.parse({"devices": ["desktop"], "scenarios": [{"name": "collapse", "about": "The tower falls.",
                        "url": "http://127.0.0.1:8877/", "expect": {"motion": spec}}]})


def test_a_motion_check_has_defaults_and_bounds():
    spec = _suite().scenarios[0].expect["motion"]
    assert {k: spec[k] for k in ("seconds", "frames", "min_changed", "min_moving", "tolerance", "looks", "do")} == {
        "seconds": 2, "frames": 8, "min_changed": 0.001, "min_moving": 1, "tolerance": 8, "looks": [], "do": []}
    assert "max_gap_ms" not in spec
    full = _suite(seconds=0.5, frames=3, max_gap_ms=17, looks="No ghost trails")
    assert full.scenarios[0].expect["motion"]["looks"] == ["No ghost trails"]
    assert _suite(do=[{"key": "Space"}]).scenarios[0].expect["motion"]["do"] == [{"key": "Space"}]
    for bad, says in (({"seconds": 0.4}, "seconds"), ({"seconds": 31}, "seconds"), ({"frames": 2}, "frames"),
                      ({"frames": 41}, "frames"), ({"frames": 4.0}, "frames"), ({"max_gap_ms": 16}, "max_gap_ms"),
                      ({"max_gap_ms": 5001}, "max_gap_ms"), ({"min_moving": 0}, "min_moving"),
                      ({"min_changed": 2}, "min_changed"), ({"looks": [{"claim": "x"}]}, "plain statements"),
                      ({"looks": [""]}, "plain statements"), ({"do": [{"tap": 1}]}, "do\\[0\\]"),
                      ({"name": ""}, "name"), ({"region": {"name": "v"}}, "region"), ({"fps": 60}, "fps"),
                      # the gaps come first, so the frames would miss a short effect the hooks start (redteam-1 M1)
                      ({"max_gap_ms": 100, "do": [{"key": "Space"}]}, "max_gap_ms and do cannot go together")):
        with pytest.raises(suite.SuiteError, match=says):
            _suite(**bad)


def test_a_multiplayer_test_cannot_check_motion():
    with pytest.raises(suite.SuiteError, match=r"expect: unknown key\(s\) \['motion'\]"):
        suite.parse({"scenarios": [{"name": "a", "url": "http://127.0.0.1/", "about": "x", "clients": ["p1", "p2"],
                                    "steps": [{"client": "p1", "js": "1"}],
                                    "expect": {"motion": {"name": "n", "region": REGION}}}]})


def test_the_plan_names_each_motion_check():
    parsed = _suite(max_gap_ms=100, looks=["No ghost trails"], frames=5, seconds=1)
    assert plan.absent_only(parsed.scenarios) == [] and suite.has_checks(parsed.scenarios[0].expect)
    assert verdict.planned(parsed.scenarios[0].expect) == [
        {"check": "motion 'the collapse plays' in region 'view'",
         "says": "the collapse plays: over 1 s, at least 100% of the 4 pairs of 5 frames of region view change by at "
                 "least 0.001 of their pixels, with RGB difference greater than 8"},
        {"check": "motion 'the collapse plays': frame gaps at most 100 ms",
         "says": "the collapse plays: over 1 s with no capture, the page's main thread runs an animation frame at "
                 "least every 100 ms (frames QAJev asks for: a busy main thread delays them, a page that stops drawing "
                 "does not)"},
        {"check": "motion 'the collapse plays' looks: No ghost trails",
         "says": "the collapse plays: on a sheet of the frames, No ghost trails"}]
    # every check has its words, so the test is described and its gate can pass
    assert plan.item(1, "collapse", "x", verdict.planned(parsed.scenarios[0].expect))["described"] is True


def test_frame_gaps_are_read_from_the_frame_times():
    assert runner.frame_gaps([0, 16, 33, 50, 250, 266]) == {"frames": 6, "seconds": 0.266, "fps": 18.8,
                                                           "longest_ms": 200, "p95_ms": 200}
    assert runner.frame_gaps([5])["longest_ms"] is None


def png(dark):
    image = Image.new("RGB", (8, 8), "white")
    for x in range(dark):
        for y in range(8):
            image.putpixel((x, y), (0, 0, 0))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class Scene:
    """A stand-in tab: each capture shows the next of `columns` (how many dark columns)."""

    def __init__(self, columns, times=None):
        self.columns, self.times, self.calls = list(columns), times, []
        self.jev = type("J", (), {"model": type("M", (), {"post_json": None})()})()

    def set_device(self, *_): pass
    def arm(self, *_): pass
    def check_host(self, *_): pass
    def navigate(self, url): self.url = url
    def probe(self, *_): return {"url": self.url, "title": "Tower", "status": 200, "probe": {}}
    def run_hook(self, hook, url): self.calls.append(hook)

    def frame_times(self, ms):
        self.calls.append(("frames", ms))
        return self.times

    def capture_region_png(self, region):
        self.calls.append("capture")
        return png(self.columns.pop(0))


def _run(monkeypatch, tmp_path, scene, **extra):
    parsed = _suite(**extra)
    monkeypatch.setattr(runner, "wait_for_quiet", lambda _: (True, 0))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    result = runner.run_scenario(scene, parsed.scenarios[0], opts=runner.Options(shots=False),
                                 hosts={"127.0.0.1:8877"}, run_dir=tmp_path)
    return parsed, result


@pytest.mark.parametrize("columns,extra,outcome,moving,still", [
    ([0, 1, 2, 3, 4], {}, "pass", 4, 0),
    ([2, 2, 2, 2, 2], {}, "fail", 0, 4),                       # a frozen view
    ([0, 1, 1, 1, 2], {}, "fail", 2, 2),                       # it stops midway
    ([0, 1, 1, 1, 2], {"min_moving": 0.5}, "pass", 2, 2),
])
def test_the_frames_must_keep_moving(monkeypatch, tmp_path, columns, extra, outcome, moving, still):
    scene = Scene(columns)
    parsed, result = _run(monkeypatch, tmp_path, scene, frames=5, seconds=1, do=[{"key": "Space"}], **extra)
    assert result["outcome"] == outcome, result["reason"]
    assert scene.calls == [{"key": "Space"}] + ["capture"] * 5
    metrics = result["motion"]["metrics"]
    assert (metrics["moving"], metrics["still_run"]) == (moving, still)
    assert len(result["motion"]["frames"]) == 5 and "sheet" not in result["motion"]
    assert report_files.image_paths({"scenarios": [result]}) == sorted(result["motion"]["frames"])
    data = report.build(parsed, [result], [], browser={}, started_at=time.time(), strict=False, interrupted=False,
                        run_dir=tmp_path)
    report.write(tmp_path, data)
    assert all(path in (tmp_path / "report.md").read_text() for path in result["motion"]["frames"])


def test_long_frame_gaps_fail_their_own_check_and_are_read_before_the_captures(monkeypatch, tmp_path):
    scene = Scene([0, 1, 2], times=[0, 16, 33, 300, 316])
    _, result = _run(monkeypatch, tmp_path, scene, frames=3, seconds=1, max_gap_ms=100)
    assert scene.calls[:2] == [("frames", 1000), "capture"]
    motion, gaps = result["checks"]
    assert motion["ok"] is True and gaps["ok"] is False
    assert gaps["detail"].startswith("longest gap 267 ms")
    assert result["outcome"] == "fail"


def test_clef_judges_the_looks_on_a_sheet_of_the_frames(monkeypatch, tmp_path):
    seen = {}

    def look(post_json, image, statements, context, **_):
        seen.update(image=image, statements=statements, context=context)
        return [{"check": f"looks: {statements[0]}", "ok": False, "detail": "p 0.10", "probability": 0.1,
                 "judge": "Clef"}]

    monkeypatch.setattr(vision, "look", look)
    scene = Scene(list(range(8)))
    _, result = _run(monkeypatch, tmp_path, scene, frames=8, seconds=1, looks=["No ghost trails"])
    assert seen["image"].startswith("data:image/png;base64,") and seen["statements"] == ["No ghost trails"]
    assert seen["context"]["image"].startswith("6 frames of the region 'view', taken over ")
    assert result["motion"]["sheet_frames"] == [0, 1, 3, 4, 6, 7]
    assert result["checks"][1] == {"check": "motion 'the collapse plays' looks: No ghost trails", "ok": False,
                                   "says": "the collapse plays: on a sheet of the frames, No ghost trails",
                                   "detail": "p 0.10", "probability": 0.1, "judge": "Clef"}
    assert result["outcome"] == "fail"
    assert result["motion"]["sheet"] in report_files.image_paths({"scenarios": [result]})


def test_a_clef_failure_is_a_model_error_not_a_product_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(vision, "look", lambda *a, **k: (_ for _ in ()).throw(vision.VisionError("Clef said 503")))
    _, result = _run(monkeypatch, tmp_path, Scene(list(range(3))), frames=3, seconds=1, looks=["No ghost trails"])
    assert result["outcome"] == "harness" and "Clef said 503" in result["reason"]


def test_motion_looks_need_clef_and_are_refused_before_the_browser(monkeypatch):
    monkeypatch.delenv("QAJEV_JEV_PROVIDER", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-key-for-test")
    monkeypatch.setattr(runner.chrome, "reap", lambda *a, **k: pytest.fail("the run got past the Clef check"))
    with pytest.raises(runner.ConfigError, match=r"looks needs Clef.*'collapse' asks for it"):
        runner.run(_suite(looks=["No ghost trails"]), runner.Options())


def _dark(canvas_dark=None, **extra):
    scenario = {"name": "collapse", "about": "The tower falls.", "url": "http://127.0.0.1:8877/",
                "expect": {"motion": {"name": "the collapse plays", "region": REGION, "frames": 3, "seconds": 1,
                                      **extra}}}
    return suite.parse({"devices": ["desktop"], "scenarios": [
        {**scenario, **({"canvas_dark": canvas_dark} if canvas_dark else {})}]})


def test_frames_that_are_all_one_flat_dark_colour_are_harness_not_a_frozen_view(monkeypatch, tmp_path):
    """A capture without its GPU layer reads as one flat dark colour in every frame: that is not the page (redteam-1
    L3, #276). Clef is not asked about a sheet of blank frames."""
    monkeypatch.setattr(runner, "wait_for_quiet", lambda _: (True, 0))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    monkeypatch.setattr(vision, "look", lambda *a, **k: pytest.fail("Clef was asked about blank frames"))
    result = runner.run_scenario(Scene([8, 8, 8]), _dark(looks=["No ghost trails"]).scenarios[0],
                                 opts=runner.Options(shots=False), hosts={"127.0.0.1:8877"}, run_dir=tmp_path)
    assert result["outcome"] == "harness", result["reason"]
    assert "every frame of region 'view' is one flat dark colour (median grey 0)" in result["reason"]
    assert [c["broke"] for c in result["checks"]] == [True, True] and result["motion"]["flat_dark"]["median"] == 0
    # a region that is dark on purpose says so, and its frozen frames fail as frozen
    result = runner.run_scenario(Scene([8, 8, 8]), _dark("a night scene").scenarios[0],
                                 opts=runner.Options(shots=False), hosts={"127.0.0.1:8877"}, run_dir=tmp_path)
    assert result["outcome"] == "fail" and "0 of 2 frame pairs moved" in result["reason"], result["reason"]


def test_a_sheet_keeps_time_order_and_fits_its_width():
    frames = [png(i) for i in range(5)]
    with Image.open(io.BytesIO(pixels.sheet(frames, columns=3, max_width=3 * 8 + 8))) as out:
        assert out.size == (32, 20)
        assert out.getpixel((0, 0)) == (255, 255, 255)  # frame 0: no dark column
        assert out.getpixel((12, 0)) == (0, 0, 0) and out.getpixel((13, 0)) == (255, 255, 255)  # frame 1: one
        assert out.getpixel((15, 12)) == (0, 0, 0) and out.getpixel((16, 12)) == (255, 255, 255)  # frame 4: four
    with pytest.raises(pixels.PixelError, match="dimensions must match"):
        big = io.BytesIO()
        Image.new("RGB", (9, 8)).save(big, format="PNG")
        pixels.sheet([frames[0], big.getvalue()])


# A scene with a marker that moves each frame. ?freeze=1: it draws one frame and stops (the planted frozen view).
# ?stall=<ms>: every 500 ms the page's main thread is busy that long (the planted frame gaps).
SCENE = """<!doctype html><link rel="icon" href="data:,"><style>body{margin:0}</style>
<canvas id="view" width="240" height="120"></canvas><script>
const q = new URLSearchParams(location.search), freeze = q.get('freeze'), stall = Number(q.get('stall') || 0);
const c = document.getElementById('view').getContext('2d'), t0 = performance.now();
const frame = now => {
  const t = (now - t0) / 1000;
  c.fillStyle = '#fff'; c.fillRect(0, 0, 240, 120);
  c.fillStyle = '#26c'; c.fillRect(110 + 90 * Math.sin(t * 4), 40, 20, 40);
  if (!freeze) requestAnimationFrame(frame);
};
requestAnimationFrame(frame);
if (stall) setInterval(() => { const end = performance.now() + stall; while (performance.now() < end); }, 500);
</script>"""


@pytest.fixture
def scene():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            body = SCENE.encode()
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
def test_browser_a_moving_scene_passes_and_a_frozen_or_stalling_one_fails(page, scene, tmp_path, monkeypatch):
    tab, _ = page
    tab.hosts = {urlsplit(scene).netloc}
    monkeypatch.setattr(session, "require_transport", lambda: None)
    spec = {"name": "the marker moves", "region": {"name": "view", "x": 0, "y": 0, "width": 240, "height": 120},
            "seconds": 1.5, "frames": 6, "max_gap_ms": 200}
    scenario = suite.parse({"devices": ["desktop"], "scenarios": [{"name": "scene", "about": "x", "url": scene,
                                                                   "expect": {"motion": spec}}]}).scenarios[0]
    seen = {}
    for query in ("?smooth", "?freeze=1", "?stall=400"):
        tab.navigate(scene + "/" + query)
        checks, evidence, failed = runner._motion_check(tab, scenario, tmp_path / query[1:], scene, {})
        assert failed is None
        seen[query] = ([c["ok"] for c in checks], evidence["metrics"]["moving"], evidence["metrics"]["gaps"])
    assert seen["?smooth"][0] == [True, True], seen
    assert seen["?freeze=1"][0][0] is False and seen["?freeze=1"][1] == 0, seen   # the frozen view
    assert seen["?stall=400"][0][1] is False and seen["?stall=400"][2]["longest_ms"] >= 300, seen  # the gaps
