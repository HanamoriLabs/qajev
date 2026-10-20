import base64
import io
import time

import pytest
from PIL import Image

from qajev import plan, report, runner, suite, verdict
from qajev.session import Session


def effect(**extra):
    return {"name": "visible contrast", "region": {"name": "ground", "x": 2, "y": 3, "width": 8, "height": 8},
            "off": [{"js": "off"}], "on": [{"js": "on"}], "min_changed": 0.2, "tolerance": 8, **extra}


def specification(**extra):
    return suite.parse({"devices": ["desktop"], "scenarios": [{"name": "contrast", "about": "The effect draws.",
                        "url": "http://127.0.0.1:8877/", "expect": {"effect": effect(**extra)}}]})


def png(changed=False):
    image = Image.new("RGB", (8, 8), "white")
    if changed:
        for x in range(4):
            for y in range(4):
                image.putpixel((x, y), (0, 0, 0))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_effect_only_suite_is_described_positive_and_planned_once():
    parsed = specification()
    scenario = parsed.scenarios[0]
    assert suite.has_checks(scenario.expect) and not suite.page_checks(scenario.expect)
    assert len(verdict.planned(scenario.expect)) == 1
    assert plan.not_described(plan.from_suite(parsed.scenarios)) == []
    assert plan.absent_only(parsed.scenarios) == []


def test_yaml_on_off_keys_work_without_special_quoting():
    parsed = suite.loads("""devices: [desktop]
scenarios:
  - name: contrast
    about: The effect draws.
    url: http://127.0.0.1:8877/
    expect:
      effect:
        name: visible contrast
        region: {name: ground, x: 2, y: 3, width: 8, height: 8}
        off: [{js: offState()}]
        on: [{js: onState()}]
        min_changed: 0.2
""")
    assert parsed.scenarios[0].expect["effect"]["off"] == [{"js": "offState()"}]
    assert parsed.scenarios[0].expect["effect"]["on"] == [{"js": "onState()"}]


@pytest.mark.parametrize("extra", [{"min_changed": 0}, {"min_changed": True}, {"min_changed": float("nan")},
                                  {"min_changed": 1.1}, {"tolerance": -1}, {"tolerance": 256},
                                  {"tolerance": True}, {"off": []}, {"on": []}, {"name": ""},
                                  {"region": {"name": "area", "x": -1, "y": 0, "width": 8, "height": 8}},
                                  {"region": {"name": "", "x": 0, "y": 0, "width": 8, "height": 8}}])
def test_invalid_effect_spec_is_refused_before_the_browser(extra):
    with pytest.raises(suite.SuiteError, match="effect"):
        specification(**extra)


def test_nested_effect_commands_require_existing_command_consent():
    parsed = specification(off=[{"command": "printf off"}])
    assert parsed.scenarios[0].uses_commands
    with pytest.raises(runner.ConfigError, match="allow-commands"):
        runner.run(parsed, runner.Options())


class Page:
    def __init__(self, changed=True):
        self.changed, self.enabled, self.calls = changed, False, []

    def set_device(self, device): pass
    def arm(self, mode, speech=None): pass
    def check_host(self, url): pass
    def navigate(self, url): self.url = url
    def probe(self, expect): return {"url": self.url, "status": 200, "probe": {}}
    def screenshot(self, path, marked=True): return None
    def run_hook(self, hook, url):
        self.calls.append(hook)
        self.enabled = hook.get("js") == "on"
    def capture_region_png(self, region): return png(self.changed and self.enabled)


@pytest.mark.parametrize("changed, outcome", [(True, "pass"), (False, "fail")])
def test_effect_reads_real_frames_and_keeps_evidence_even_without_optional_shots(tmp_path, monkeypatch,
                                                                            changed, outcome):
    parsed = specification()
    page = Page(changed)
    monkeypatch.setattr(runner, "wait_for_quiet", lambda opts: (True, 0))
    result = runner.run_scenario(page, parsed.scenarios[0], opts=runner.Options(shots=False),
                                 hosts={"127.0.0.1:8877"}, run_dir=tmp_path)
    assert result["outcome"] == outcome
    assert page.calls == [{"js": "off"}, {"js": "on"}]
    evidence = result["effect"]
    assert evidence["metrics"]["changed"] == (16 if changed else 0)
    assert evidence["metrics"]["total"] == 64
    assert evidence["metrics"]["min_changed"] == 0.2
    assert evidence["metrics"]["tolerance"] == 8
    assert result["checks"][0]["check"] == verdict.planned(parsed.scenarios[0].expect)[0]["check"]
    for key in ("off", "on", "difference"):
        assert (tmp_path / evidence[key]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    data = report.build(parsed, [result], [], browser={}, started_at=time.time(), strict=False,
                        interrupted=False, run_dir=tmp_path)
    report.write(tmp_path, data)
    for name in ("report.md", "report.html"):
        text = (tmp_path / name).read_text()
        for key in ("off", "on", "difference"):
            assert evidence[key] in text
        assert "ground" in text and "0.2" in text and "64" in text


def test_failed_effect_capture_is_harness_and_names_the_missing_check(tmp_path, monkeypatch):
    parsed = specification()
    page = Page()

    def broken(region):
        raise RuntimeError("region capture failed")

    page.capture_region_png = broken
    monkeypatch.setattr(runner, "wait_for_quiet", lambda opts: (True, 0))
    result = runner.run_scenario(page, parsed.scenarios[0], opts=runner.Options(shots=False),
                                 hosts={"127.0.0.1:8877"}, run_dir=tmp_path)
    assert result["outcome"] == "harness"
    assert "effect" in result["reason"] and "capture" in result["reason"]


def test_changes_outside_the_named_region_do_not_satisfy_the_effect(tmp_path, monkeypatch):
    class Outside(Page):
        def capture_region_png(self, region):
            image = Image.new("RGB", (20, 20), "white")
            if self.enabled:
                image.putpixel((0, 0), (0, 0, 0))
            x, y = region["x"], region["y"]
            output = io.BytesIO()
            image.crop((x, y, x + region["width"], y + region["height"])).save(output, format="PNG")
            return output.getvalue()

    monkeypatch.setattr(runner, "wait_for_quiet", lambda opts: (True, 0))
    result = runner.run_scenario(Outside(), specification().scenarios[0], opts=runner.Options(shots=False),
                                 hosts={"127.0.0.1:8877"}, run_dir=tmp_path)
    assert result["outcome"] == "fail" and result["effect"]["metrics"]["changed"] == 0


def test_effect_planted_fault_and_mcp_keep_the_measurement_and_evidence(tmp_path, monkeypatch):
    from qajev import mcp_server

    parsed = specification()
    scenario = parsed.scenarios[0]
    scenario.expect_fail = {"check": verdict.effect_planned(scenario.expect["effect"])["check"],
                            "words": verdict.effect_planned(scenario.expect["effect"])["says"]}
    monkeypatch.setattr(runner, "wait_for_quiet", lambda opts: (True, 0))
    result = runner.run_scenario(Page(changed=False), scenario, opts=runner.Options(shots=False),
                                 hosts={"127.0.0.1:8877"}, run_dir=tmp_path)
    data = report.build(parsed, [result], [], browser={}, started_at=time.time(), strict=False,
                        interrupted=False, run_dir=tmp_path)
    assert data["gate"] == "PASS" and data["scenarios"][0]["graded"] == "pass"
    trimmed = mcp_server._trim(data, False)
    assert trimmed["scenarios"][0]["effect"] == result["effect"]


def test_region_capture_is_lossless_and_uses_viewport_coordinates_without_markers():
    page = object.__new__(Session)
    calls = []
    page.evaluate = lambda expression, timeout_ms=15000: {"width": 100, "height": 80, "scrollX": 10, "scrollY": 20}
    page.call = lambda method, **kw: calls.append((method, kw)) or {"data": base64.b64encode(png()).decode()}
    assert page.capture_region_png(effect()["region"]) == png()
    screenshot = next(params for name, params in calls if name == "Page.captureScreenshot")
    assert screenshot["format"] == "png" and "quality" not in screenshot
    assert screenshot["clip"] == {"x": 12, "y": 23, "width": 8, "height": 8, "scale": 1}
    assert screenshot["fromSurface"] is True and screenshot["captureBeyondViewport"] is False


def test_region_outside_the_viewport_is_rejected_before_any_capture():
    page = object.__new__(Session)
    page.evaluate = lambda expression, timeout_ms=15000: {"width": 4, "height": 4, "scrollX": 0, "scrollY": 0}
    page.call = lambda *a, **kw: pytest.fail("out-of-bounds region must never be captured")
    with pytest.raises(ValueError, match="outside"):
        page.capture_region_png(effect()["region"])
