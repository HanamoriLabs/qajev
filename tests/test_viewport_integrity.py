"""A passing script cannot validate evidence from a differently sized page viewport."""

import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from qajev import runner, session, suite


def scenario_page(monkeypatch, *, inner=(205, 900), image_size=(1280, 900), scale=1):
    stream = io.BytesIO()
    Image.new("RGB", image_size, (19, 47, 83)).save(stream, format="PNG")
    encoded = base64.b64encode(stream.getvalue()).decode()
    seen = {"calls": [], "checks": 0, "hooks": 0}

    def cdp(method, **params):
        seen["calls"].append((method, params))
        if method == "Runtime.evaluate":
            return {"result": {"value": {"width": inner[0], "height": inner[1], "scale": scale,
                                           "innerWidth": inner[0], "innerHeight": inner[1],
                                           "devicePixelRatio": scale}}}
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


@pytest.mark.parametrize("device,inner,scale", [
    ("desktop", (1280, 900), 1), ("phone", (390, 844), 3), ("tablet", (820, 1180), 2),
])
def test_native_capture_and_declared_device_scale_keep_valid_scenarios_passing(monkeypatch, device, inner, scale):
    page, seen = scenario_page(monkeypatch, inner=inner,
                               image_size=(inner[0] * scale, inner[1] * scale), scale=scale)
    result = run(page, device=device)
    assert result["outcome"] == "pass"
    assert seen["checks"] > 0 and seen["hooks"] == 1
