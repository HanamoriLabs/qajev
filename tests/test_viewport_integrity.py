"""A passing script cannot validate evidence from a differently sized page viewport."""

import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from qajev import clients, runner, session, suite, viewport


def scenario_page(monkeypatch, *, inner=(205, 900), image_size=(1280, 900), scale=1, zoom=1):
    stream = io.BytesIO()
    Image.new("RGB", image_size, (19, 47, 83)).save(stream, format="PNG")
    encoded = base64.b64encode(stream.getvalue()).decode()
    seen = {"calls": [], "checks": 0, "hooks": 0}

    def cdp(method, **params):
        seen["calls"].append((method, params))
        if method == "Runtime.evaluate":
            return {"result": {"value": {"width": inner[0], "height": inner[1], "scale": scale,
                                           "innerWidth": inner[0], "innerHeight": inner[1],
                                           "devicePixelRatio": scale,
                                           "visualViewport": {"width": inner[0], "height": inner[1], "scale": zoom}}}}
        if method == "Page.captureScreenshot":
            return {"data": encoded}
        return {}

    class Page(session.Session):
        def __init__(self):
            self.browser = SimpleNamespace(call=cdp, target="fixture-target", session="fixture-session")
            self.jev = SimpleNamespace(cdp=lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("viewport checks must use their own target")))
            self.device = self.user_agent = self.net = self.agent = None
            self.headless, self.ux_observer = True, None
            self.url = "http://127.0.0.1/fixture"

        def begin_test(self):
            pass

        def set_color_scheme(self, _scheme):
            pass

        def configure_ux(self, *_args):
            return None

        def arm(self, *_args):
            pass

        def check_host(self, _url):
            pass

        def navigate(self, url):
            self.url = url

        def probe(self, _expect):
            seen["checks"] += 1
            return {"url": self.url, "status": 200, "text": [True], "probe": {}}

        def run_hook(self, *_args):
            seen["hooks"] += 1

    monkeypatch.setattr(session, "_jev", None)
    return Page(), seen


def run(page, *, device="desktop"):
    parsed = suite.parse({"base_url": "http://127.0.0.1", "device": device, "scenarios": [
        {"name": "viewport evidence", "url": "/fixture", "settle": 0,
         "before": [{"js": "window.fixtureAction=true"}], "expect": {"text": "ready"}}]})
    return runner.run_scenario(page, parsed.scenarios[0],
                               opts=runner.Options(shots=False, ux="off", load_high=None),
                               hosts={"127.0.0.1"}, run_dir=None)


@pytest.mark.parametrize("inner", [(205, 900), (190, 900), (1280, 205)],
                         ids=["205px-window", "190px-window", "wrong-height"])
def test_mismatched_window_cannot_pass_even_when_every_scripted_check_would_pass(monkeypatch, inner):
    page, seen = scenario_page(monkeypatch, inner=inner)
    result = run(page)
    assert result["outcome"] == "harness", "a page/capture mismatch must never become a scripted PASS"
    assert "viewport" in result["reason"].lower()
    assert str(inner[0] if inner[0] != 1280 else inner[1]) in result["reason"]
    assert seen["checks"] == seen["hooks"] == 0, "geometry must be checked before scenario work"
    assert result["viewport"]["capture_pixels"] == [1280, 900]
    assert result["viewport"]["after"]["innerWidth"] == inner[0]
    assert result["viewport"]["after"]["innerHeight"] == inner[1]


@pytest.mark.parametrize("device,inner,scale", [
    ("desktop", (1280, 900), 1), ("phone", (390, 844), 3), ("tablet", (820, 1180), 2),
])
def test_native_capture_and_declared_device_scale_keep_valid_scenarios_passing(monkeypatch, device, inner, scale):
    page, seen = scenario_page(monkeypatch, inner=inner,
                               image_size=(inner[0] * scale, inner[1] * scale), scale=scale)
    result = run(page, device=device)
    assert result["outcome"] == "pass"
    assert seen["checks"] > 0 and seen["hooks"] == 1
    assert result["viewport"]["capture_pixels"] == [inner[0] * scale, inner[1] * scale]


@pytest.mark.parametrize("image_size,scale", [((205, 900), 1), ((1280, 205), 1), ((1280, 900), 2)])
def test_matching_layout_cannot_hide_wrong_capture_size_or_dpr(monkeypatch, image_size, scale):
    page, seen = scenario_page(monkeypatch, inner=(1280, 900), image_size=image_size, scale=scale)
    result = run(page)
    assert result["outcome"] == "harness" and "viewport" in result["reason"]
    assert seen["checks"] == seen["hooks"] == 0


def test_each_scenario_measures_even_when_the_device_profile_is_cached(monkeypatch):
    page, seen = scenario_page(monkeypatch, inner=(1280, 900))
    assert run(page)["outcome"] == run(page)["outcome"] == "pass"
    assert sum(method == "Page.captureScreenshot" for method, _ in seen["calls"]) == 2


def png(size=(1280, 900)):
    output = io.BytesIO()
    Image.new("RGB", size, (19, 47, 83)).save(output, format="PNG")
    return output.getvalue()


def geometry(width=1280, height=900, dpr=1, zoom=1):
    return {"innerWidth": width, "innerHeight": height, "devicePixelRatio": dpr,
            "visualViewport": {"width": width, "height": height, "scale": zoom}}


@pytest.mark.parametrize("field,value", [
    ("innerWidth", None), ("innerHeight", 0), ("devicePixelRatio", True), ("innerWidth", float("nan")),
    ("visualViewport", None), ("visualViewport", {"width": 1280, "height": 900, "scale": 0}),
])
def test_missing_or_invalid_page_geometry_is_not_a_pass(field, value):
    measured = {**geometry(), field: value}
    with pytest.raises(viewport.ViewportMismatch, match="could not be measured"):
        viewport.verify(measured, measured, png(), suite.DEVICES["desktop"])


def test_a_resize_during_capture_and_a_broken_image_are_refused():
    with pytest.raises(viewport.ViewportMismatch, match="changed during capture"):
        viewport.verify(geometry(width=205), geometry(), png(), suite.DEVICES["desktop"])
    with pytest.raises(viewport.ViewportMismatch, match="no valid captured image"):
        viewport.verify(geometry(), geometry(), b"not an image", suite.DEVICES["desktop"])


def test_mobile_layout_zoom_is_normalized_without_changing_the_image_or_device(monkeypatch):
    page, _ = scenario_page(monkeypatch, inner=(980, 2121), image_size=(1170, 2532), scale=3, zoom=390 / 980)
    result = run(page, device="phone")
    assert result["outcome"] == "pass", result["reason"]
    assert result["viewport"]["capture_pixels"] == [1170, 2532]
    assert result["viewport"]["after"]["innerWidth"] == 980


def test_a_client_with_wrong_geometry_stops_the_scenario_before_any_client_action(monkeypatch):
    wrong, wrong_seen = scenario_page(monkeypatch)
    good, good_seen = scenario_page(monkeypatch, inner=(1280, 900))
    for page in (wrong, good):
        page.close = lambda: None
    pages = iter((wrong, good))
    monkeypatch.setattr(session, "Session", lambda *_a, **_kw: next(pages))
    monkeypatch.setattr(session, "Tab", lambda *_a: SimpleNamespace(close=lambda: None))
    parsed = suite.parse({"base_url": "http://127.0.0.1", "device": "desktop", "scenarios": [
        {"name": "two viewport clients", "url": "/fixture", "clients": 2, "settle": 0,
         "steps": [{"all": {"js": "window.fixtureAction=true"}}], "expect": {"text": "ready"}}]})
    result = clients.run(parsed.scenarios[0], ledger=None, hosts={"127.0.0.1"}, run_dir=None,
                         suite_meta={"headless": True, "guard": {}})
    assert result["outcome"] == "harness" and result["stop"] == "viewport_mismatch"
    assert "[p1]" in result["reason"] and "205" in result["reason"]
    assert wrong_seen["hooks"] == good_seen["hooks"] == 0
    assert wrong_seen["checks"] == 0
    assert result["clients"][0]["viewport"]["capture_pixels"] == [1280, 900]
