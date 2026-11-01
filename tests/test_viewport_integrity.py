"""A passing script cannot validate evidence from a differently sized page viewport."""

import base64
import copy
import io
import itertools
from types import SimpleNamespace

import pytest
from PIL import Image

from qajev import clients, runner, session, suite, verdict, viewport


def scenario_page(monkeypatch, *, inner=(205, 900), image_size=(1280, 900), scale=1, zoom: float = 1, measured=None):
    stream = io.BytesIO()
    Image.new("RGB", image_size, (19, 47, 83)).save(stream, format="PNG")
    encoded = base64.b64encode(stream.getvalue()).decode()
    seen = {"calls": [], "checks": 0, "hooks": 0}
    view = measured if measured is not None else geometry(*inner, dpr=scale, zoom=zoom)

    def cdp(method, **params):
        seen["calls"].append((method, params))
        if method == "Runtime.evaluate":
            return {"result": {"value": copy.deepcopy(view)}}
        if method == "Page.getLayoutMetrics":
            return {"cssLayoutViewport": view.get("layoutViewport")}
        if method == "Page.captureScreenshot":
            return {"data": encoded}
        return {}

    class Page(session.Session):
        def __init__(self):
            self.browser = SimpleNamespace(call=cdp, target="fixture-target", session="fixture-session")
            self.jev = SimpleNamespace(cdp=lambda *_a, **_k: (_ for _ in ()).throw(
                AssertionError("viewport checks must use their own target")),
                admin=SimpleNamespace(ensure_daemon=lambda: None))
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


def run(page, *, device="desktop", shots=False, run_dir=None, looks=False):
    parsed = suite.parse({"base_url": "http://127.0.0.1", "device": device, "scenarios": [
        {"name": "viewport evidence", "url": "/fixture", "settle": 0,
         "before": [{"js": "window.fixtureAction=true"}],
         "expect": {"text": "ready", **({"looks": ["the viewport is valid"]} if looks else {})}}]})
    return runner.run_scenario(page, parsed.scenarios[0],
                               opts=runner.Options(shots=shots, ux="off", load_high=None),
                               hosts={"127.0.0.1"}, run_dir=run_dir)


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


@pytest.mark.parametrize("reply", [None, {}, {"data": "invalid base64"}, TimeoutError("capture timed out")])
def test_failed_capture_is_harness_and_keeps_the_available_measurement(monkeypatch, reply):
    page, seen = scenario_page(monkeypatch, inner=(1280, 900))
    original = page.browser.call

    def call(method, **params):
        if method == "Page.captureScreenshot":
            if isinstance(reply, Exception):
                raise reply
            return reply
        return original(method, **params)

    page.browser.call = call
    result = run(page)
    assert result["outcome"] == "harness" and "viewport geometry unavailable" in result["reason"]
    assert result["viewport"]["before"]["innerWidth"] == 1280
    assert seen["hooks"] == seen["checks"] == 0


def png(size=(1280, 900)):
    output = io.BytesIO()
    Image.new("RGB", size, (19, 47, 83)).save(output, format="PNG")
    return output.getvalue()


def geometry(width=1280, height=900, dpr=1, zoom: float = 1):
    return {"innerWidth": width, "innerHeight": height, "devicePixelRatio": dpr,
            "clientWidth": width, "clientHeight": height,
            "layoutViewport": {"clientWidth": width, "clientHeight": height},
            "visualViewport": {"width": width, "height": height, "scale": zoom}}


@pytest.mark.parametrize("field,value", [
    ("innerWidth", None), ("innerHeight", 0), ("devicePixelRatio", True), ("innerWidth", float("nan")),
    ("visualViewport", None), ("visualViewport", {"width": 1280, "height": 900, "scale": 0}),
])
def test_missing_or_invalid_page_geometry_is_not_a_pass(field, value):
    measured = {**geometry(), field: value}
    with pytest.raises(viewport.ViewportMismatch, match="could not be measured"):
        viewport.verify(measured, measured, png(), suite.DEVICES["desktop"])


def test_boolean_dpr_is_invalid_even_when_true_equals_one():
    measured = geometry(dpr=True)
    with pytest.raises(viewport.ViewportMismatch, match="could not be measured"):
        viewport.verify(measured, measured, png(), suite.DEVICES["desktop"])


@pytest.mark.parametrize("checks", [[], [{"check": "otherwise true", "ok": True}]])
def test_viewport_stop_is_harness_even_if_a_check_has_already_passed(checks):
    outcome, _ = verdict.classify("viewport_mismatch", checks, has_checks=True, stop_detail="viewport mismatch")
    assert outcome == "harness"


def test_a_resize_during_capture_and_a_broken_image_are_refused():
    with pytest.raises(viewport.ViewportMismatch, match="changed during capture"):
        viewport.verify(geometry(width=205), geometry(), png(), suite.DEVICES["desktop"])
    with pytest.raises(viewport.ViewportMismatch, match="no valid captured image"):
        viewport.verify(geometry(), geometry(), b"not an image", suite.DEVICES["desktop"])


def scrolled(gutter=15):
    """The desktop page once it is taller than the window: a classic scrollbar takes `gutter` CSS px."""
    view = geometry()
    view["clientWidth"] -= gutter
    view["layoutViewport"]["clientWidth"] -= gutter
    view["visualViewport"]["width"] -= gutter
    return view


def moving_page(monkeypatch, reads):
    """A scenario page whose geometry reads come from `reads`, one per measure around each capture."""
    page, seen = scenario_page(monkeypatch, inner=(1280, 900))
    reads = iter(reads)
    monkeypatch.setattr(page, "_measure_viewport", lambda: copy.deepcopy(next(reads)))
    return page, seen


def capture_steps(seen):
    """The captures and layout settles, in order."""
    return [method if method == "Page.captureScreenshot" else "settle" for method, params in seen["calls"]
            if method == "Page.captureScreenshot" or params.get("expression") == viewport.SETTLE]


def test_a_late_font_scrollbar_during_capture_is_taken_again_after_the_page_settles(monkeypatch):
    page, seen = moving_page(monkeypatch, [geometry(), scrolled(), scrolled(), scrolled()])
    result = run(page)
    assert result["outcome"] == "pass", result["reason"]
    assert capture_steps(seen) == ["Page.captureScreenshot", "settle", "Page.captureScreenshot"]
    assert result["viewport"]["retakes"] == 1
    assert result["viewport"]["before"] == result["viewport"]["after"] == scrolled()
    assert seen["checks"] > 0


def test_a_page_that_keeps_moving_is_refused_after_three_captures(monkeypatch):
    page, seen = moving_page(monkeypatch, itertools.cycle([geometry(), scrolled()]))
    result = run(page)
    assert result["outcome"] == "harness" and result["stop"] == "viewport_mismatch"
    assert "page geometry changed during capture" in result["reason"]
    assert capture_steps(seen) == ["Page.captureScreenshot", "settle"] * 2 + ["Page.captureScreenshot"]
    assert result["viewport"]["retakes"] == 2
    assert seen["checks"] == seen["hooks"] == 0


def test_a_stable_capture_is_taken_once(monkeypatch):
    page, seen = moving_page(monkeypatch, [scrolled(), scrolled()])
    result = run(page)
    assert result["outcome"] == "pass", result["reason"]
    assert capture_steps(seen) == ["Page.captureScreenshot"]
    assert result["viewport"]["retakes"] == 0


def test_a_settle_that_fails_still_takes_the_capture_again_but_a_lost_browser_stops(monkeypatch):
    page, seen = moving_page(monkeypatch, [geometry(), scrolled(), scrolled(), scrolled()])
    evaluate = page.evaluate

    def settle_fails(expression, **kwargs):
        if expression == viewport.SETTLE:
            raise RuntimeError("page script failed: navigating")
        return evaluate(expression, **kwargs)

    monkeypatch.setattr(page, "evaluate", settle_fails)
    result = run(page)
    assert result["outcome"] == "pass", result["reason"]
    assert capture_steps(seen) == ["Page.captureScreenshot"] * 2

    page, _ = moving_page(monkeypatch, [geometry(), scrolled()])
    page.set_device(suite.DEVICES["desktop"])

    def lost(*_args, **_kwargs):
        raise session.TransportLost("the browser went away")

    monkeypatch.setattr(page, "evaluate", lost)
    with pytest.raises(session.TransportLost):
        page.verify_viewport()


def test_mobile_layout_zoom_is_normalized_without_changing_the_image_or_device(monkeypatch):
    page, _ = scenario_page(monkeypatch, inner=(980, 2121), image_size=(1170, 2532), scale=3, zoom=390 / 980)
    result = run(page, device="phone")
    assert result["outcome"] == "pass", result["reason"]
    assert result["viewport"]["capture_pixels"] == [1170, 2532]
    assert result["viewport"]["after"]["innerWidth"] == 980


def test_a_narrow_visual_viewport_cannot_hide_behind_correct_layout_dimensions(monkeypatch):
    measured = geometry()
    measured["visualViewport"]["width"] = 205
    page, seen = scenario_page(monkeypatch, measured=measured)
    result = run(page)
    assert result["outcome"] == "harness", result["reason"]
    assert seen["checks"] == seen["hooks"] == 0


@pytest.mark.parametrize("zoom,gutter", [(1, 15), (2, 0)])
def test_scrollbars_and_pinch_zoom_keep_matching_native_captures_valid(monkeypatch, zoom, gutter):
    measured = geometry(zoom=zoom)
    measured["clientWidth"] -= gutter
    measured["layoutViewport"]["clientWidth"] -= gutter
    measured["visualViewport"] = {"width": (1280 - gutter) / zoom, "height": 900 / zoom, "scale": zoom}
    page, _ = scenario_page(monkeypatch, inner=(1280, 900), measured=measured)
    result = run(page)
    assert result["outcome"] == "pass", result["reason"]
    assert result["viewport"]["scrollbar_css"] == [gutter, 0]


def test_dom_rounding_is_not_a_scrollbar_on_scaled_mobile_pages(monkeypatch):
    measured = geometry(980, 2121, dpr=3, zoom=390 / 980)
    measured["clientHeight"] = 2120
    measured["visualViewport"]["height"] = 844 / (390 / 980)
    page, _ = scenario_page(monkeypatch, measured=measured, image_size=(1170, 2532))
    result = run(page, device="phone")
    assert result["outcome"] == "pass", result["reason"]
    assert result["viewport"]["scrollbar_css"] == [0, 0]


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


def drift(measured):
    measured.update(geometry(205, 900))


def test_a_later_saved_capture_mismatch_overrides_passing_checks(monkeypatch, tmp_path):
    measured = geometry()
    page, seen = scenario_page(monkeypatch, inner=(1280, 900), measured=measured)
    page.run_hook = lambda *_a: drift(measured)
    result = run(page, shots=True, run_dir=tmp_path)
    assert result["outcome"] == "harness" and result["stop"] == "viewport_mismatch", result["reason"]
    assert seen["checks"] > 0, "the passing checks must actually have run before the bad capture"
    assert result["viewport"]["before"]["innerWidth"] == 1280
    assert result["capture_viewport"]["before"]["innerWidth"] == 205
    assert result["shot"] is None and not list(tmp_path.glob("shots/*"))


@pytest.mark.parametrize("kind", ["saved", "vision-jpeg", "vision-png"])
def test_each_full_capture_refuses_drift_without_accepting_an_image(monkeypatch, tmp_path, kind):
    page, _ = scenario_page(monkeypatch)
    page.set_device(suite.DEVICES["desktop"])
    with pytest.raises(viewport.ViewportMismatch, match="viewport mismatch"):
        if kind == "saved":
            page.screenshot(tmp_path / "bad.jpg")
        elif kind == "vision-jpeg":
            page.screen_image()
        else:
            page.screen_image_png()
    assert not (tmp_path / "bad.jpg").exists()


def test_a_rejected_vision_capture_is_not_retried_for_the_report(monkeypatch, tmp_path):
    measured = geometry()
    page, seen = scenario_page(monkeypatch, inner=(1280, 900), measured=measured)
    page.run_hook = lambda *_a: drift(measured)
    result = run(page, shots=True, run_dir=tmp_path, looks=True)
    assert result["outcome"] == "harness" and result["stop"] == "viewport_mismatch", result["reason"]
    assert sum(method == "Page.captureScreenshot" for method, _ in seen["calls"]) == 2
    assert not list(tmp_path.glob("shots/*"))


def test_one_clients_late_capture_fault_keeps_both_clients_cleanup(monkeypatch, tmp_path):
    measured = geometry()
    first, _ = scenario_page(monkeypatch, inner=(1280, 900), measured=measured)
    second, _ = scenario_page(monkeypatch, inner=(1280, 900))
    first.run_hook = lambda *_a: drift(measured)
    closed = []
    for index, page in enumerate((first, second)):
        page.close = lambda index=index: closed.append(index)
    pages = iter((first, second))
    monkeypatch.setattr(session, "Session", lambda *_a, **_kw: next(pages))
    monkeypatch.setattr(session, "Tab", lambda *_a: SimpleNamespace(close=lambda: None))
    parsed = suite.parse({"base_url": "http://127.0.0.1", "device": "desktop", "scenarios": [
        {"name": "late client frame", "url": "/fixture", "clients": 2, "settle": 0,
         "steps": [{"all": {"js": "void 0"}}], "expect": {"text": "ready"}}]})
    result = clients.run(parsed.scenarios[0], ledger=None, hosts={"127.0.0.1"}, run_dir=tmp_path,
                         suite_meta={"headless": True, "guard": {}})
    assert result["outcome"] == "harness" and result["stop"] == "viewport_mismatch", result["reason"]
    assert result["clients"][0]["shot"] is None
    assert result["clients"][0]["capture_viewport"]["before"]["innerWidth"] == 205
    assert result["clients"][1]["shot"]
    assert sorted(closed) == [0, 1]
