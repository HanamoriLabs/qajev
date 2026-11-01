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


def run(page, expect, **extra):
    parsed = suite.parse({"devices": ["desktop"], "scenarios": [{"name": "supported hall", "about": "fixture",
                          "url": "http://127.0.0.1:8765/", "expect": expect, **extra}]})
    return runner.run_scenario(page, parsed.scenarios[0], opts=runner.Options(shots=False),
                               hosts={"127.0.0.1:8765"}, run_dir=None)


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
    passed = run(page, {"looks": LOOKS, "status": 200})
    assert page.calls == [] and passed["outcome"] == "harness", passed
    failed = run(Page(jpeg(), status=203), {"looks": LOOKS, "status": 200})
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
