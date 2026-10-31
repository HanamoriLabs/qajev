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
