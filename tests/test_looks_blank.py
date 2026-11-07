"""expect.looks never sends Clef a blank capture: a screen whose main canvas is one flat dark colour is not judged.

workload-gpu, 1 Nov: in rea's Crumble run, headless Chromium captured the 3D canvas blank dark. The end-screenshot
check graded the one test with no failed check as harness, but Clef judged the same blank canvas in four other tests
and graded them fail (p 0.05 to 0.12). A blank capture says nothing about how the page looks.
"""

import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from qajev import runner, suite
from qajev.session import Session

W, H = 64, 40
# 24 x 16 canvas points no other element covers, as CANVAS_POINTS gives them for a canvas over the whole view
POINTS = {"points": [[(i + 0.5) * W / 24, (j + 0.5) * H / 16] for i in range(24) for j in range(16)],
          "width": W, "size": [W, H], "share": 1.0}


def jpeg(draw=None):
    image = Image.new("RGB", (W, H), (8, 8, 10))  # a 3D view's clear colour
    if draw:
        draw(image)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def lit(image):  # a drawn scene: a lit floor and walls over most of the view
    for x in range(W):
        for y in range(H):
            if (x // 8 + y // 8) % 2:
                image.putpixel((x, y), (200, 180, 140))


class Page:
    blank_canvas = Session.blank_canvas  # the real reading, over the looks image's bytes

    def __init__(self, image, status=200, evaluate=None):
        self.image, self.status, self.calls = image, status, []
        self.jev = SimpleNamespace(model=SimpleNamespace(post_json=self.post))
        self._evaluate = evaluate

    def post(self, url, key, body):  # Clef: says no to every statement
        self.calls.append(body)
        return {"answers": {name: {"noul": 0.06} for name in body["questions"]}}

    def evaluate(self, expression, timeout_ms=None):
        return self._evaluate() if self._evaluate else POINTS

    def set_device(self, device): pass
    def arm(self, mode, speech=None): pass
    def check_host(self, url): pass
    def navigate(self, url): self.url = url
    def probe(self, expect): return {"url": self.url, "status": self.status, "probe": {}}
    def screenshot(self, path, marked=True): return None
    def screen_image(self): return self.image


def run(page, expect, run_dir=None, **extra):
    parsed = suite.parse({"devices": ["desktop"], "scenarios": [{"name": "supported hall", "about": "fixture",
                          "url": "http://127.0.0.1:8765/", "expect": expect, **extra}]})
    return runner.run_scenario(page, parsed.scenarios[0], opts=runner.Options(shots=False),
                               hosts={"127.0.0.1:8765"}, run_dir=run_dir)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(runner, "wait_for_quiet", lambda opts: (True, 0))


LOOKS = ["The hall stands on its pillars."]


def test_a_blank_canvas_never_reaches_clef_and_grades_harness_not_fail():
    page = Page(jpeg())
    result = run(page, {"looks": LOOKS})
    assert page.calls == [], "Clef judged a blank capture"
    assert result["outcome"] == "harness", result
    assert result["reason"].startswith("the looks were not judged: the screen shows the page's main canvas as one flat "
                                       "dark colour"), result["reason"]
    assert result["looks_blank"]["points"] == 384 and result["looks_blank"]["median"] < 24
    assert not any(c["check"].startswith("looks") for c in result["checks"])


def test_a_blank_canvas_with_other_checks_that_pass_is_harness_and_one_that_fails_still_fails():
    page = Page(jpeg())
    passed = run(page, {"looks": LOOKS, "status": 200}, settle=0)  # settle 0: a check that says no ends at once
    assert page.calls == [] and passed["outcome"] == "harness", passed
    failed = run(Page(jpeg(), status=203), {"looks": LOOKS, "status": 200}, settle=0)
    assert failed["outcome"] == "fail", failed
    assert "HTTP 203" in failed["reason"] and "the looks were not judged" in failed["reason"], failed["reason"]


def test_a_drawn_canvas_still_goes_to_clef_and_its_no_is_a_fail():
    page = Page(jpeg(lit))
    result = run(page, {"looks": LOOKS})
    assert len(page.calls) == 1 and result["outcome"] == "fail", result
    assert not result.get("looks_blank")


def test_canvas_dark_on_purpose_sends_the_dark_canvas_to_clef():
    page = Page(jpeg())
    result = run(page, {"looks": LOOKS}, canvas_dark="the hall is shown at night")
    assert len(page.calls) == 1 and result["outcome"] == "fail", result


def test_a_canvas_check_that_breaks_is_noted_and_clef_judges_as_before():
    def broken():
        raise RuntimeError("planted CANVAS_POINTS failure")

    page = Page(jpeg(), evaluate=broken)
    result = run(page, {"looks": LOOKS})
    assert len(page.calls) == 1 and result["outcome"] == "fail", result
    assert "planted CANVAS_POINTS failure" in result["looks_blank_error"]


def test_a_session_without_the_canvas_reading_sends_the_image_to_clef():
    class Native(Page):
        blank_canvas = None

    page = Native(jpeg())
    result = run(page, {"looks": LOOKS})
    assert len(page.calls) == 1 and result["outcome"] == "fail", result


def test_the_grade_keeps_a_failure_and_turns_everything_else_into_harness():
    blank = {"points": 384, "median": 8, "flat": 1.0}
    for outcome in ("pass", "fail", "stuck", "unverified", "harness"):
        got, reason = runner.looks_blank_grade(outcome, "r", blank, [{"check": "x", "ok": True}])
        assert got == "harness" and reason.startswith("the looks were not judged"), (outcome, got)
    got, reason = runner.looks_blank_grade("fail", "r", blank, [{"check": "x", "ok": False}])
    assert got == "fail" and reason.startswith("r; the looks were not judged")
    assert runner.looks_blank_grade("fail", "r", None, []) == ("fail", "r")


# canvas_capture: page and a dark screen: the page's own frame decides (runner.drawn_dark; test_dark_frame reads
# real frames in Chrome). redteam-1's known-answer tests, 3 Nov: a black-frame fault must fail on its look, not grade
# harness.
DARK = {"points": 384, "median": 0, "flat": 1.0}
CAPTURE = {"canvas_capture": {"page": "document.getElementById('view').toDataURL()"}}


class Framed(Page):
    """A page with canvas_capture: the readback in a frame callback gives `frame`, the one after the test `end`."""

    def __init__(self, image, frame, end=None):
        super().__init__(image)
        self.frame, self.end, self.counted, self.in_frame = frame, end or frame, 0, []

    def count_draws(self):
        self.counted += 1

    def page_canvas(self, spec, path, in_frame=False):
        self.in_frame.append(in_frame)
        got = self.frame if in_frame else self.end
        return {"image": None, "problem": None, "blank": None, "adapter": None, "gpu": None, "software": None, **got}


def test_a_dark_frame_the_page_drew_goes_to_clef_and_fails_on_its_look(tmp_path):
    page = Framed(jpeg(), {"blank": DARK, "draws": 2, "transparent": False})
    result = run(page, {"looks": LOOKS}, run_dir=tmp_path, **CAPTURE)
    assert len(page.calls) == 1, "Clef did not judge the page's dark frame"
    assert page.counted == 1 and page.in_frame[0] is True, page.in_frame  # counted before load, read in a frame
    assert result["outcome"] == "fail" and not result.get("looks_blank"), result
    assert result["looks_frame"]["dark"] is True and result["looks_frame"]["draws"] == 2
    assert [c["ok"] for c in result["checks"] if c["check"].startswith("looks")] == [False]


@pytest.mark.parametrize("frame, why", [
    ({"blank": DARK, "draws": 0, "transparent": False}, "no draw call in the readback's frame (redteam-1 O1)"),
    ({"blank": DARK, "draws": None, "transparent": False}, "no draw count"),
    ({"blank": DARK, "draws": 3, "transparent": True}, "a cleared, transparent buffer"),
    ({"blank": None, "draws": 3, "transparent": False}, "a lit frame: the screenshot lost the GPU layer"),
    ({"blank": DARK, "draws": 3, "transparent": False, "problem": "the readback expression failed: x"}, "no readback"),
])
def test_a_dark_screen_whose_frame_is_no_evidence_is_not_judged_and_grades_harness(tmp_path, frame, why):
    page = Framed(jpeg(), frame, end={"blank": None})
    result = run(page, {"looks": LOOKS}, run_dir=tmp_path, **CAPTURE)
    assert page.calls == [], why
    assert result["outcome"] == "harness" and result["looks_blank"], (why, result)
    assert result["looks_frame"]["dark"] is False, why


def test_without_canvas_capture_a_dark_screen_is_never_read_in_a_frame_and_stays_harness(tmp_path):
    page = Framed(jpeg(), {"blank": DARK, "draws": 2, "transparent": False})
    result = run(page, {"looks": LOOKS}, run_dir=tmp_path)
    assert page.calls == [] and page.in_frame == [] and page.counted == 0
    assert result["outcome"] == "harness" and "looks_frame" not in result, result


def test_a_lit_screen_with_canvas_capture_is_judged_as_before_without_a_frame_readback(tmp_path):
    page = Framed(jpeg(lit), {"blank": DARK, "draws": 2, "transparent": False}, end={"blank": None})
    result = run(page, {"looks": LOOKS}, run_dir=tmp_path, **CAPTURE)
    assert len(page.calls) == 1 and True not in page.in_frame and "looks_frame" not in result, result
