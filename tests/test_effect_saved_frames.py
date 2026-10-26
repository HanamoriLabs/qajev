import base64
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageChops, ImageStat

from qajev import report, runner, suite
from qajev.session import Session

FIXTURES = Path(__file__).parent / "fixtures" / "visible_effect"
PROVENANCE = json.loads((FIXTURES / "provenance.json").read_text())
REGION = PROVENANCE["region"]
VIEWPORT = PROVENANCE["viewport"]


class SavedFramesPage(Session):
    """Only the browser transport is replaced; capture, comparison and verdict are real."""

    def __init__(self, off, on):
        self.frames = {"off": off, "on": on}
        self.state, self.hooks, self.clips = "off", [], []
        self.browser = SimpleNamespace(target="saved-frame", session="fixture-session")

    def begin_test(self): pass
    def set_color_scheme(self, scheme): pass
    def set_device(self, device):
        assert device["width"] == VIEWPORT["width"] and device["height"] == VIEWPORT["height"]
        self.device = device
    def arm(self, mode, speech=None): pass
    def check_host(self, url): pass
    def navigate(self, url, timeout=30.0): self.url = url
    def probe(self, expect): return {"url": self.url, "status": 200, "probe": {}}
    def run_hook(self, hook, current_url):
        self.state = hook["js"]
        self.hooks.append(self.state)
    def evaluate(self, expression, timeout_ms=15000):
        return {**VIEWPORT, "scrollX": 0, "scrollY": 0,
                "innerWidth": VIEWPORT["width"], "innerHeight": VIEWPORT["height"], "devicePixelRatio": 1,
                "clientWidth": VIEWPORT["width"], "clientHeight": VIEWPORT["height"],
                "visualViewport": {**VIEWPORT, "scale": 1}}
    def call(self, method, **params):
        if method == "Page.getLayoutMetrics":
            return {"cssLayoutViewport": {"clientWidth": VIEWPORT["width"], "clientHeight": VIEWPORT["height"]}}
        assert method == "Page.captureScreenshot" and params["format"] == "png"
        if "clip" not in params:
            output = io.BytesIO()
            self.frames[self.state].save(output, format="PNG")
            return {"data": base64.b64encode(output.getvalue()).decode()}
        clip = params["clip"]
        self.clips.append(clip)
        x, y, width, height = (clip[key] for key in ("x", "y", "width", "height"))
        output = io.BytesIO()
        with self.frames[self.state].crop((x, y, x + width, y + height)) as image:
            image.save(output, format="PNG")
        return {"data": base64.b64encode(output.getvalue()).decode()}


def specification(name):
    return suite.parse({"name": name, "devices": ["desktop"], "scenarios": [{
        "name": name, "device": f"{VIEWPORT['width']}x{VIEWPORT['height']}",
        "about": "The named region must change visibly between the off and on captures.",
        "url": "http://127.0.0.1:8877/", "expect": {"effect": {
            "name": "visible region change", "region": REGION,
            "off": [{"js": "off"}], "on": [{"js": "on"}],
            "tolerance": PROVENANCE["tolerance"], "min_changed": PROVENANCE["min_changed"],
        }},
    }]})


def replay(off, on, name, expected, tmp_path, monkeypatch):
    parsed = specification(name)
    page = SavedFramesPage(off, on)
    monkeypatch.setattr(runner, "wait_for_quiet", lambda opts: (True, 0))
    result = runner.run_scenario(page, parsed.scenarios[0], opts=runner.Options(shots=False, ux="off"),
                                 hosts={"127.0.0.1:8877"}, run_dir=tmp_path)
    assert result["outcome"] == expected, result.get("reason")
    assert page.hooks == ["off", "on"]
    assert page.clips == [{key: REGION[key] for key in ("x", "y", "width", "height")} | {"scale": 1}] * 2
    metrics = result["effect"]["metrics"]
    assert (metrics["width"], metrics["height"], metrics["total"]) == (380, 300, 114000)
    assert metrics["tolerance"] == 15 and metrics["min_changed"] == 0.01
    built = report.build(parsed, [result], [], browser={}, started_at=0, strict=False,
                         interrupted=False, run_dir=tmp_path)
    assert built["gate"] == expected.upper()
    assert built["cost"]["usd"] == 0
    report.write(tmp_path, built)
    for state in ("off", "on", "difference"):
        with Image.open(tmp_path / result["effect"][state]) as image:
            assert image.format == "PNG" and image.size == (380, 300)
    return metrics


def test_saved_shadow_pair_fails_the_visible_effect_gate(tmp_path, monkeypatch):
    for row in PROVENANCE["images"].values():
        assert hashlib.sha256((FIXTURES / row["file"]).read_bytes()).hexdigest() == row["sha256"]
    with Image.open(FIXTURES / "off.jpg") as off_source, Image.open(FIXTURES / "on.jpg") as on_source:
        assert off_source.size == on_source.size == (1280, 900)
        with off_source.convert("RGB") as off, on_source.convert("RGB") as on:
            metrics = replay(off, on, "saved shadow pair", "fail", tmp_path, monkeypatch)
            bounds = (890, 560, 1270, 860)
            with off.crop(bounds) as off_region, on.crop(bounds) as on_region:
                with ImageChops.difference(off_region, on_region) as difference:
                    mean = sum(ImageStat.Stat(difference).mean) / 3
    assert metrics["ok"] is False and 0 < metrics["fraction"] < 0.001
    assert 0.5 < mean < 1.1
    (tmp_path / "measurement.json").write_text(json.dumps({
        "fixture": "saved-negative", "mean_absolute_rgb_difference": mean, "metrics": metrics,
    }, indent=2))


def test_changed_region_control_passes_the_visible_effect_gate(tmp_path, monkeypatch):
    size = (VIEWPORT["width"], VIEWPORT["height"])
    with Image.new("RGB", size, (192, 192, 192)) as off, Image.new("RGB", size, (192, 192, 192)) as on:
        # An independent synthetic pair: only a 180 by 120 patch inside the named region changes.
        x, y = REGION["x"] + 100, REGION["y"] + 100
        on.paste((40, 40, 40), (x, y, x + 180, y + 120))
        off.save(tmp_path / "synthetic-off-full.png")
        on.save(tmp_path / "synthetic-on-full.png")
        metrics = replay(off, on, "synthetic changed region", "pass", tmp_path, monkeypatch)
    assert metrics["ok"] is True and metrics["changed"] == 21600
    assert metrics["fraction"] == 21600 / 114000
    (tmp_path / "measurement.json").write_text(json.dumps({
        "fixture": "synthetic-positive", "metrics": metrics,
    }, indent=2))
