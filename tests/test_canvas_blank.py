"""A test whose end screenshot shows the page's main canvas as one flat dark colour says nothing about the page.

workload-gpu, 31 Oct: a headless Chrome started with --disable-vulkan-surface drew every WebGL and WebGPU canvas
black in QAJev's screenshots, while the page kept drawing. Only the HTML panels over the 3D view were in the picture,
and the tests passed on their text checks.
"""

import json
import os
import subprocess
import sys

import pytest
from test_live import Recorder, site  # noqa: F401  (the fixture server)

from qajev import pixels, runner

LIVE = pytest.mark.skipif(os.environ.get("QAJEV_LIVE") != "1", reason="set QAJEV_LIVE=1 (starts a local Chrome)")


@pytest.mark.parametrize("lumas, flat", [
    ([11] * 300, True),                           # the 3D view's clear colour only
    ([11] * 295 + [200] * 5, True),               # a few points lit (a cursor, a mark): still one flat colour
    ([0] * 40, True),                             # black, with just enough points
    ([0] * 39, False),                            # too few points no panel covers: no judgement
    ([11] * 280 + [60] * 20, False),              # a dark scene with things in it
    ([200] * 300, False),                         # flat, but not dark: a light page or a white canvas
    ([25] * 300, False),                          # flat, but over the dark line
    ([i % 40 for i in range(300)], False),        # dark, but not one colour
])
def test_a_canvas_reads_flat_dark_only_when_nearly_all_its_points_are_one_dark_colour(lumas, flat):
    got = pixels.flat_dark(lumas)
    assert bool(got) is flat
    if flat:
        assert got["points"] == len(lumas) and got["median"] < 24


BLANK = {"points": 300, "median": 11, "flat": 1.0, "canvas": [1280, 900], "share": 1.0}


@pytest.mark.parametrize("outcome, graded", [("pass", "harness"), ("unverified", "harness"), ("fail", "fail"),
                                             ("stuck", "stuck"), ("harness", "harness")])
def test_a_flat_dark_canvas_turns_a_pass_into_harness_and_keeps_a_failure(outcome, graded):
    got, reason = runner.canvas_blank_grade(outcome, "all 1 check(s) passed", BLANK)
    assert got == graded
    if outcome in {"pass", "unverified", "fail"}:
        assert "the screenshot shows the page's main canvas as one flat dark colour" in reason
        assert "all 1 check(s) passed" in reason
    assert runner.canvas_blank_grade(outcome, "x", None) == (outcome, "x")


@LIVE
def test_a_page_whose_3d_view_is_not_in_the_screenshot_is_harness_and_a_drawn_one_passes(site, tmp_path):  # noqa: F811
    suite = tmp_path / "canvas.qajev.yaml"
    suite.write_text(f"""name: canvas
base_url: {site}
devices: [desktop]
scenarios:
  - name: a 3D view that draws nothing
    about: fixture
    url: /canvas-dark.html
    expect: {{text: ["the hall stands"]}}
  - name: a 3D view that draws
    about: fixture
    url: /canvas-lit.html
    expect: {{text: ["the hall stands"]}}
  - name: a small dark canvas
    about: fixture
    url: /canvas-small.html
    expect: {{text: ["the thumbnail stands"]}}
  - name: a dark canvas half under a lit panel
    about: fixture
    url: /canvas-covered.html
    expect: {{text: ["the hall stands"]}}
  - name: dark on purpose
    about: fixture
    url: /canvas-dark.html
    canvas_dark: the hall is shown at night
    expect: {{text: ["the hall stands"]}}
  - name: the canvas check breaks
    about: fixture
    url: /canvas-dark.html
    expect: {{text: ["the hall stands"]}}
    after:
      - js: "document.elementsFromPoint = () => {{ throw new Error('planted elementsFromPoint') }}"
  - name: a dark canvas under a see-through layer
    about: fixture
    url: /canvas-layer.html
    expect: {{text: ["the hall stands"]}}
  - name: a dark canvas under a tinted layer
    about: fixture
    url: /canvas-layer.html?tinted
    expect: {{text: ["the hall stands"]}}
""")
    p = subprocess.run([sys.executable, "-m", "qajev", "run", str(suite), "--headless", "--ephemeral",
                        "--profile", f"canvas-{os.getpid()}", "--out", str(tmp_path / "out"), "--json", "--quiet",
                        "--load-high", "0"], capture_output=True, text=True, timeout=300,
                       env={**os.environ, "QAJEV_RUNNER": "local"})
    got = {r["name"]: r for r in json.loads(p.stdout)["scenarios"]}
    dark, lit = got["a 3D view that draws nothing"], got["a 3D view that draws"]
    assert dark["outcome"] == "harness", dark
    assert dark["reason"].startswith("the screenshot shows the page's main canvas as one flat dark colour"), dark
    assert dark["canvas_blank"]["points"] >= 300 and dark["canvas_blank"]["median"] < 24, dark
    assert lit["outcome"] == "pass" and not lit.get("canvas_blank"), lit
    small = got["a small dark canvas"]  # 16% of the view: under the fifth that makes a canvas the main one
    assert small["outcome"] == "pass" and not small.get("canvas_blank"), small
    covered = got["a dark canvas half under a lit panel"]  # only the points no element covers are read: all dark
    assert covered["outcome"] == "harness" and covered["canvas_blank"]["points"] < 0.5 * 384, covered
    purpose = got["dark on purpose"]
    assert purpose["outcome"] == "pass" and purpose.get("canvas_dark") == "the hall is shown at night", purpose
    assert purpose["reason"].endswith("(canvas_dark: the hall is shown at night)"), purpose
    broke = got["the canvas check breaks"]
    assert broke["outcome"] == "pass" and not broke.get("canvas_blank"), broke
    assert "planted elementsFromPoint" in (broke.get("canvas_blank_error") or ""), broke
    assert "the canvas check could not read the end screenshot" in broke["reason"], broke
    # A see-through layer that takes the pointer does not hide the canvas (rea's Crumble page, workload-gpu, 1 Nov)
    layer = got["a dark canvas under a see-through layer"]
    assert layer["outcome"] == "harness" and layer["canvas_blank"]["through"] >= 300, layer
    tinted = got["a dark canvas under a tinted layer"]  # a layer with a background covers the canvas: not read
    assert tinted["outcome"] == "pass" and not tinted.get("canvas_blank"), tinted
    # expect.looks reads the same points (tests/test_looks_blank.py); a looks suite needs Clef set up to start at all


# canvas_capture: page (the Orchestrator, 31 Oct): a suite whose canvas the browser cannot screenshot proves it by the
# canvas's own readback. QAJev runs the readback itself, saves it in the report, and grades it, not the screenshot.

def test_canvas_capture_is_a_page_expression_with_an_optional_hardware_gpu():
    from qajev import suite

    parsed = suite.parse({"base_url": "http://127.0.0.1:9", "canvas_capture": {"page": " view.toDataURL() "},
                          "scenarios": [{"name": "a", "url": "/", "expect": {"text": "x"}},
                                        {"name": "b", "url": "/", "expect": {"text": "x"},
                                         "canvas_capture": {"page": "grab()", "gpu": "hardware"}}]})
    a, b = parsed.scenarios
    assert a.canvas_capture == {"page": "view.toDataURL()", "gpu": None}
    assert b.canvas_capture == {"page": "grab()", "gpu": "hardware"}
    assert suite.parse({"base_url": "http://127.0.0.1:9", "scenarios": [
        {"name": "a", "url": "/", "expect": {"text": "x"}}]}).scenarios[0].canvas_capture is None
    for bad, says in (("grab()", "must be a mapping"), ({}, "canvas_capture.page"),
                      ({"page": "  "}, "canvas_capture.page"), ({"page": "g()", "gpu": "any"}, "must be hardware"),
                      ({"page": "g()", "shot": 1}, "shot")):
        with pytest.raises(suite.SuiteError, match=says):
            suite.parse({"base_url": "http://127.0.0.1:9", "canvas_capture": bad,
                         "scenarios": [{"name": "a", "url": "/", "expect": {"text": "x"}}]})


def png(colour, size=(64, 48), mode="RGB", drawn=False):
    import io

    from PIL import Image

    out = io.BytesIO()
    with Image.new(mode, size, colour) as image:
        if drawn:  # half the canvas lit: a scene, not one colour
            image.paste((200, 120, 40), (0, 0, size[0] // 2, size[1]))
        image.save(out, format="PNG")
    return out.getvalue()


def test_a_readback_is_read_on_a_grid_and_a_transparent_one_reads_black():
    assert pixels.flat_dark(pixels.grid_lumas(png((11, 11, 16))))
    assert pixels.flat_dark(pixels.grid_lumas(png((0, 0, 0, 0), mode="RGBA")))  # nothing drawn: transparent
    assert not pixels.flat_dark(pixels.grid_lumas(png((11, 11, 16), drawn=True)))
    for bad in (b"", b"not an image", b"GIF89a"):
        with pytest.raises(pixels.PixelError):
            pixels.grid_lumas(bad)


@pytest.mark.parametrize("kind", ["GIF", "BMP"])
def test_a_readback_in_another_image_format_is_refused(kind):
    import io

    from PIL import Image

    out = io.BytesIO()
    with Image.new("RGB", (64, 48), (200, 120, 40)) as image:
        image.save(out, format=kind)
    with pytest.raises(pixels.PixelError, match=f"the readback is {kind} image, not PNG, JPEG or WebP"):
        pixels.grid_lumas(out.getvalue())


@pytest.mark.parametrize("size", [(16385, 1), (1, 16385), (4097, 4097)])
def test_a_readback_over_the_size_limit_is_refused(size):
    with pytest.raises(pixels.PixelError, match="outside the limits"):
        pixels.grid_lumas(png(0, size=size, mode="L"))
    for fits in ((16384, 1), (4096, 4096)):  # the limits themselves are read: 16384 a side, 16777216 pixels
        assert pixels.grid_lumas(png(0, size=fits, mode="L"))


@pytest.mark.parametrize("adapter, software", [
    ({"vendor": "google", "architecture": "swiftshader", "description": "", "fallback": True}, "swiftshader"),
    ({"vendor": "mesa", "architecture": "", "description": "llvmpipe (LLVM 19)", "fallback": False}, "llvmpipe"),
    ({"vendor": "x", "architecture": "y", "description": "", "fallback": True}, "a fallback adapter"),
    ({"none": "the page got no WebGPU adapter"}, "the page got no WebGPU adapter"),
    (None, "could not be read"),
    ({"vendor": "nvidia", "architecture": "ada", "description": "NVIDIA L4", "fallback": False}, None),
])
def test_a_software_or_missing_webgpu_adapter_is_named(adapter, software):
    from qajev.session import software_adapter

    got = software_adapter(adapter)
    assert (got is None) if software is None else (software in got)


UNNAMED = {"vendor": "", "architecture": "", "description": "", "fallback": False}


@pytest.mark.parametrize("gpu, software", [
    (None, "the GPU could not be read"),
    ({"none": "the page has no WebGL to name its GPU"}, "the page has no WebGL to name its GPU"),
    ({"device": "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)), SwiftShader driver)"},
     "the browser's GPU is software (SwiftShader)"),
    ({"device": "llvmpipe (LLVM 19.1.7, 256 bits)"}, "the browser's GPU is software (llvmpipe)"),
    ({"device": ""}, "WebGL gives no renderer"),
    ({"device": "ANGLE (NVIDIA, NVIDIA L4 (0x000027B8) Direct3D11 vs_5_0 ps_5_0, D3D11)"}, None),
])
def test_an_unnamed_webgpu_adapter_is_proved_only_by_the_browsers_gpu(gpu, software):
    from qajev.session import software_adapter

    got = software_adapter(UNNAMED, gpu)
    assert (got is None) if software is None else (software in got), got
    assert software_adapter({**UNNAMED, "vendor": "nvidia"}, None) is None  # a named adapter needs no GPU info


def test_a_hardware_readback_reads_the_browsers_gpu_when_the_adapter_is_unnamed(tmp_path):
    from qajev.session import GPU_NAME_JS, WEBGPU_ADAPTER_JS, Session

    s = Session.__new__(Session)
    named = {WEBGPU_ADAPTER_JS: UNNAMED, GPU_NAME_JS: {"device": "Google SwiftShader"}}
    s.evaluate = lambda js, timeout_ms: None  # the readback: the page's own expression
    s.isolated_evaluate = lambda js, timeout_ms: named.get(js)
    got = s.page_canvas({"page": "null", "gpu": "hardware"}, tmp_path / "c.png")
    assert got["gpu"] == {"device": "Google SwiftShader"}, got
    assert got["software"] == "the browser's GPU is software (SwiftShader)", got


@pytest.mark.parametrize("capture, says", [
    ({"problem": "the readback expression gave nothing"}, "canvas_capture: page gave no readback image"),
    ({"software": "the page's WebGPU adapter is software (swiftshader)"}, "asks for a hardware GPU"),
])
def test_no_readback_or_a_software_gpu_turns_a_pass_into_harness_and_keeps_a_failure(capture, says):
    graded, reason = runner.canvas_capture_grade("pass", "ok", capture)
    assert graded == "harness" and says in reason and reason.endswith("; ok"), reason
    graded, reason = runner.canvas_capture_grade("fail", "bad", capture)
    assert graded == "fail" and reason.startswith("bad") and says in reason, reason
    assert runner.canvas_capture_grade("pass", "ok", {"problem": None, "software": None}) == ("pass", "ok")
    reason = runner.canvas_blank_grade("pass", "ok", {**BLANK, "readback": True})[1]
    assert reason.startswith("the canvas's own readback (canvas_capture: page) is one flat dark colour")


# A page script that claims an NVIDIA GPU in the page's own world: WebGL's renderer name, and a WebGPU adapter.
LYING_GPU = ("(() => { for (const C of [WebGLRenderingContext, WebGL2RenderingContext]) { "
             "const get = C.prototype.getParameter; C.prototype.getParameter = function (p) { "
             "return p === 37446 ? 'NVIDIA planted' : get.call(this, p); }; } "
             "if (navigator.gpu) navigator.gpu.requestAdapter = async () => ({info: {vendor: 'nvidia', "
             "architecture: 'planted', description: 'NVIDIA planted'}, isFallbackAdapter: false}); return true; })()")


# A WebGL without WEBGL_debug_renderer_info (redteam-1 L1, #228): its plain RENDERER says "WebKit WebGL" on every
# machine, so GPU_NAME_JS must name no GPU. The extension is hidden in the page's world, where the check runs too.
HIDE_RENDERER_NAME = ("(() => { for (const C of [WebGLRenderingContext, WebGL2RenderingContext]) { "
                      "const get = C.prototype.getExtension; C.prototype.getExtension = function (n) { "
                      "return n === 'WEBGL_debug_renderer_info' ? null : get.call(this, n); }; } return true; })()")


def names_no_gpu():
    from qajev.session import GPU_NAME_JS

    return (f"(() => {{ const r = {GPU_NAME_JS}; return r.none === \"the page's WebGL does not name its GPU "
            "(no WEBGL_debug_renderer_info)\" || JSON.stringify(r); })()")


NAMES_NO_GPU = names_no_gpu()


# A readback drawn in the page, half lit: the screenshot of /canvas-dark.html is flat dark, this image is not.
LIT_READBACK = ("(() => { const c = document.createElement('canvas'); c.width = 64; c.height = 48; "
                "const g = c.getContext('2d'); g.fillStyle = '#c87828'; g.fillRect(0, 0, 32, 48); "
                "return c.toDataURL(); })()")


@LIVE
def test_canvas_capture_grades_the_canvas_readback(site, tmp_path):  # noqa: F811
    lit, dark = "/canvas-lit.html", "/canvas-dark.html"
    grab = "document.getElementById('view').toDataURL()"
    suite = tmp_path / "readback.qajev.yaml"
    suite.write_text(f"""name: readback
base_url: {site}
devices: [desktop]
canvas_capture: {{page: "{grab}"}}
scenarios:
  - name: drawn
    about: fixture
    url: {lit}
    expect: {{text: ["the hall stands"]}}
  - name: nothing drawn
    about: fixture
    url: {dark}
    expect: {{text: ["the hall stands"]}}
  - name: no readback
    about: fixture
    url: {lit}
    expect: {{text: ["the hall stands"]}}
    canvas_capture: {{page: "null"}}
  - name: hardware asked
    about: fixture
    url: {lit}
    expect: {{text: ["the hall stands"]}}
    canvas_capture: {{page: "{grab}", gpu: hardware}}
  - name: WebGL without the renderer name
    about: fixture
    url: {lit}
    before:
      - js: {json.dumps(HIDE_RENDERER_NAME)}
    expect:
      js: {json.dumps(NAMES_NO_GPU)}
      says: {{js: "GPU_NAME_JS names no GPU when WebGL has no WEBGL_debug_renderer_info"}}
  - name: a page that says it has a GPU
    about: fixture
    url: {lit}
    before:
      - js: {json.dumps(LYING_GPU)}
    expect: {{text: ["the hall stands"]}}
    canvas_capture: {{page: "{grab}", gpu: hardware}}
  - name: dark screen, lit readback
    about: fixture
    url: {dark}
    expect: {{text: ["the hall stands"]}}
    canvas_capture: {{page: {json.dumps(LIT_READBACK)}}}
""")
    out = tmp_path / "out"
    p = subprocess.run([sys.executable, "-m", "qajev", "run", str(suite), "--headless", "--ephemeral",
                        "--profile", f"readback-{os.getpid()}", "--out", str(out), "--json", "--quiet",
                        "--load-high", "0"], capture_output=True, text=True, timeout=300,
                       env={**os.environ, "QAJEV_RUNNER": "local"})
    built = json.loads(p.stdout)
    got = {r["name"]: r for r in built["scenarios"]}
    drawn = got["drawn"]
    assert drawn["outcome"] == "pass", drawn
    assert drawn["canvas_capture"]["image"].endswith("-canvas.png") and not drawn.get("canvas_blank"), drawn
    assert (out / built["run_dir"].split("/out/", 1)[-1] / drawn["canvas_capture"]["image"]).is_file() or any(
        out.rglob("drawn-canvas.png")), drawn
    nothing = got["nothing drawn"]
    assert nothing["outcome"] == "harness" and nothing["canvas_blank"]["readback"] is True, nothing
    assert nothing["reason"].startswith("the canvas's own readback (canvas_capture: page) is one flat dark"), nothing
    none = got["no readback"]
    assert none["outcome"] == "harness", none
    assert none["reason"].startswith("canvas_capture: page gave no readback image: the readback expression gave"), none
    hardware = got["hardware asked"]  # QAJev's headless Chrome on a test machine has no hardware WebGPU
    assert hardware["outcome"] == "harness" and "asks for a hardware GPU" in hardware["reason"], hardware
    assert "SwiftShader" in hardware["canvas_capture"]["gpu"]["device"], hardware  # the test machine's software GL
    # The adapter is read in QAJev's own JS world, which still has navigator.gpu: no adapter, not "no WebGPU".
    assert hardware["canvas_capture"]["adapter"] == {"none": "the page got no WebGPU adapter"}, hardware
    unnamed = got["WebGL without the renderer name"]
    assert unnamed["outcome"] == "pass", unnamed
    lying = got["a page that says it has a GPU"]["canvas_capture"]  # the page's patches do not reach QAJev's world
    assert "SwiftShader" in lying["gpu"]["device"] and "planted" not in json.dumps(lying), lying
    assert lying["adapter"] == {"none": "the page got no WebGPU adapter"}, lying
    skipped = got["dark screen, lit readback"]  # with canvas_capture, the dark screenshot is not graded
    assert skipped["outcome"] == "pass" and not skipped.get("canvas_blank"), skipped


# A canvas dark on purpose says why (redteam-1 L4, #225): only words turn the flat dark grade off.

ONE = {"name": "a", "url": "/", "expect": {"text": "x"}}


def test_canvas_dark_is_a_scenarios_written_reason():
    from qajev import suite

    parsed = suite.parse({"base_url": "http://127.0.0.1:9",
                          "scenarios": [ONE, {**ONE, "name": "b", "canvas_dark": " a fade to black "}]})
    assert [s.canvas_dark for s in parsed.scenarios] == [None, "a fade to black"]


@pytest.mark.parametrize("bad", [True, "  ", 1, {"why": "x"}])
def test_canvas_dark_without_words_is_refused(bad):
    from qajev import suite

    with pytest.raises(suite.SuiteError, match="canvas_dark must say in words why the canvas is dark on purpose"):
        suite.parse({"base_url": "http://127.0.0.1:9", "scenarios": [{**ONE, "canvas_dark": bad}]})


def test_a_suite_level_canvas_dark_is_refused():  # one reason would turn the check off for every test (redteam-1 L5)
    from qajev import suite

    with pytest.raises(suite.SuiteError, match="canvas_dark goes on each scenario"):
        suite.parse({"base_url": "http://127.0.0.1:9", "canvas_dark": "a night scene", "scenarios": [ONE]})


@pytest.mark.parametrize("outcome", ["pass", "unverified", "fail"])
def test_a_canvas_dark_on_purpose_keeps_its_outcome_and_a_broken_check_is_named(outcome):
    got, reason = runner.canvas_blank_grade(outcome, "ok", BLANK, dark="a night scene")
    assert got == outcome and reason == "ok; the main canvas is one flat dark colour, which the suite says is on " \
                                        "purpose (canvas_dark: a night scene)", reason
    got, reason = runner.canvas_blank_grade(outcome, "ok", None, error="RuntimeError: gone")
    assert got == outcome and reason == "ok; the canvas check could not read the end screenshot (RuntimeError: gone)"
    assert runner.canvas_blank_grade(outcome, "ok", None, dark="a night scene") == (outcome, "ok")
    got, reason = runner.canvas_blank_grade(outcome, "ok", {**BLANK, "readback": True}, dark="a night scene")
    assert got == outcome and reason.endswith("(canvas_dark: a night scene)"), reason  # the readback, too


# redteam-1 B1 (#225): canvas_capture runs in QAJev's own tab only. A scenario with clients (clients.py) or a device
# browser (real_devices) reads no canvas, so it must never pass as if the readback had been graded.

PLAYERS = {"name": "duel", "url": "/", "clients": 2, "steps": [{"all": {"js": "1"}}], "expect": {"text": "x"}}


@pytest.mark.parametrize("data, says", [
    ({"scenarios": [{**PLAYERS, "canvas_capture": {"page": "grab()"}}]}, "does not run in a scenario with clients$"),
    ({"canvas_capture": {"page": "grab()"}, "scenarios": [PLAYERS]}, "give canvas_capture to each scenario"),
])
def test_canvas_capture_on_a_scenario_with_clients_is_refused(data, says):
    from qajev import suite

    with pytest.raises(suite.SuiteError, match=says):
        suite.parse({"base_url": "http://127.0.0.1:9", **data})
    assert suite.parse({"base_url": "http://127.0.0.1:9", "scenarios": [PLAYERS]}).scenarios[0].canvas_capture is None


def test_a_device_browser_run_of_a_canvas_capture_scenario_is_harness(monkeypatch):
    import contextlib
    from types import SimpleNamespace

    from qajev import mobile, native, suite

    parsed = suite.parse({"base_url": "http://127.0.0.1:9", "scenarios": [
        {"name": "view", "url": "/", "expect": {"text": "x"}, "canvas_capture": {"page": "grab()"}},
        {"name": "plain", "url": "/", "expect": {"text": "x"}}]})
    monkeypatch.setattr(mobile, "MobileApp", lambda *_a, **_kw: contextlib.nullcontext(None))
    monkeypatch.setattr(native, "run_session", lambda _g, steps, **_kw: [
        {"name": st["name"], "outcome": "pass", "reason": "all 1 check(s) passed"} for st in steps])
    results, _ = runner.real_device_run(parsed, parsed.scenarios, "android", SimpleNamespace(
        shots=False, typesafe_usd_per_call=0.0), 1.0, None, None)
    got = {r["name"]: r for r in results}
    view, plain = got["view (Android Chrome)"], got["plain (Android Chrome)"]
    assert view["outcome"] == "harness", view
    assert view["reason"].startswith("canvas_capture: page gave no readback image: not run in a device browser")
    assert plain["outcome"] == "pass" and "canvas_capture" not in plain, plain
