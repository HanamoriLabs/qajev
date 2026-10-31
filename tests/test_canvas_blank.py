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


def png(colour, size=(64, 48), mode="RGB"):
    import io

    from PIL import Image

    out = io.BytesIO()
    with Image.new(mode, size, colour) as image:
        if colour == "drawn":
            image.paste((200, 120, 40), (0, 0, size[0] // 2, size[1]))
        image.save(out, format="PNG")
    return out.getvalue()


def test_a_readback_is_read_on_a_grid_and_a_transparent_one_reads_black():
    assert pixels.flat_dark(pixels.grid_lumas(png((11, 11, 16))))
    assert pixels.flat_dark(pixels.grid_lumas(png((0, 0, 0, 0), mode="RGBA")))  # nothing drawn: transparent
    assert not pixels.flat_dark(pixels.grid_lumas(png("drawn")))
    for bad in (b"", b"not an image", b"GIF89a"):
        with pytest.raises(pixels.PixelError):
            pixels.grid_lumas(bad)


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


@pytest.mark.parametrize("capture, says", [
    ({"problem": "the readback expression gave nothing"}, "canvas_capture: page gave no readback image"),
    ({"software": "the page's WebGPU adapter is software (swiftshader)"}, "asks for a hardware GPU"),
])
def test_no_readback_or_a_software_gpu_turns_a_pass_into_harness_and_keeps_a_failure(capture, says):
    graded, reason = runner.canvas_capture_grade("pass", "ok", capture)
    assert graded == "harness" and reason.startswith(says) and reason.endswith("ok"), reason
    graded, reason = runner.canvas_capture_grade("fail", "bad", capture)
    assert graded == "fail" and reason.startswith("bad") and says in reason, reason
    assert runner.canvas_capture_grade("pass", "ok", {"problem": None, "software": None}) == ("pass", "ok")
    reason = runner.canvas_blank_grade("pass", "ok", {**BLANK, "readback": True})[1]
    assert reason.startswith("the canvas's own readback (canvas_capture: page) is one flat dark colour")


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
