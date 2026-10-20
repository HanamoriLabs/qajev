import base64
import io

import pytest
from PIL import Image

from qajev import suite, vision


def png(color="white", size=(8, 4)):
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def region(name="subject", **extra):
    return {"name": name, "x": 0, "y": 0, "width": 4, "height": 4, **extra}


def claim(text="The left hand holds the grip.", **extra):
    return {"claim": text, "crop": region(), **extra}


def response(body, probability=0.9):
    return {"answers": {name: {"noul": probability} for name in body["questions"]}}


def test_legacy_looks_ask_one_claim_per_request_and_keep_each_probability():
    calls = []

    def post(url, key, body):
        calls.append(body)
        return response(body, 0.9 if len(calls) == 1 else 0.1)

    checks = vision.look(post, "data:image/jpeg;base64,AAAA", ["The hand holds.", "The halo is wide."], {})
    assert len(calls) == 2
    assert all(len(call["questions"]) == 1 for call in calls)
    instructions = [next(iter(call["questions"].values()))["instructions"] for call in calls]
    assert "The hand holds." in instructions[0] and "The halo is wide." not in instructions[0]
    assert "The halo is wide." in instructions[1] and "The hand holds." not in instructions[1]
    assert [check["ok"] for check in checks] == [True, False]
    assert [check["detail"] for check in checks] == ["p 0.90", "p 0.10"]


def test_structured_claims_are_preserved_and_each_plan_names_its_crop():
    spec = suite.parse({"devices": ["desktop"], "scenarios": [{"name": "hands", "about": "Both hands hold.",
                       "url": "http://127.0.0.1:8765/", "expect": {"looks": [claim()]}}]})
    item = spec.scenarios[0].expect["looks"][0]
    assert isinstance(item, dict) and item["claim"] == claim()["claim"]
    from qajev import verdict

    assert any("left hand" in check["check"] and "subject" in check["check"]
               for check in verdict.planned(spec.scenarios[0].expect))


@pytest.mark.parametrize("item", [claim(crop=region(width=True)), claim(crop=region(x=-1)),
                                 claim(crop=region(width=1.5)), claim(crop=region(name="")),
                                 claim(reference={"path": "../private.png", "role": "expected"}),
                                 claim(reference={"path": "/private.png", "role": "expected"}),
                                 claim(reference={"path": ".private/frame.png", "role": "expected"}),
                                 claim(reference={"path": "frame.png", "role": "other"}),
                                 claim(different_from={"crop": region("other"), "min_changed": 0}),
                                 {"claim": "A hand is visible."}])
def test_invalid_claim_is_refused_before_the_browser(item):
    with pytest.raises(suite.SuiteError, match="looks"):
        suite.parse({"scenarios": [{"name": "claim", "about": "The scene draws.",
                                    "url": "http://127.0.0.1/", "expect": {"looks": [item]}}]})


def test_each_claim_receives_only_its_own_lossless_crop_and_keeps_the_evidence(tmp_path):
    frame = Image.new("RGB", (8, 4), "white")
    frame.paste((0, 0, 0), (4, 0, 8, 4))
    output = io.BytesIO()
    frame.save(output, format="PNG")
    calls = []

    def post(url, key, body):
        calls.append(body)
        return response(body)

    entries = [claim("The left region is white."),
               claim("The right region is black.", crop=region("right", x=4))]
    checks = vision.look(post, vision.data_url(output.getvalue()), entries, {},
                         folder=tmp_path, evidence_dir=tmp_path / "report")
    assert len(calls) == len(checks) == 2
    for index, (call, check) in enumerate(zip(calls, checks)):
        assert len(call["questions"]) == len(call["images"]) == 1
        raw = base64.b64decode(call["images"][0].split(",", 1)[1], validate=True)
        with Image.open(io.BytesIO(raw)) as image:
            assert image.format == "PNG" and image.size == (4, 4)
            assert image.getpixel((0, 0)) == ((255, 255, 255) if index == 0 else (0, 0, 0))
        assert (tmp_path / "report" / check["evidence"]["crop"]).read_bytes() == raw


@pytest.mark.parametrize("color", ["white", (251, 251, 251)])
def test_similar_panels_fail_even_when_a_model_would_pass_them(tmp_path, color):
    frame = Image.new("RGB", (8, 4), "white")
    frame.paste(color, (4, 0, 8, 4))
    output = io.BytesIO()
    frame.save(output, format="PNG")
    calls = []

    def post(url, key, body):
        calls.append(body)
        return response(body, 1.0)

    entry = claim("The panels visibly differ.", different_from={"crop": region("control", x=4),
                                                              "min_changed": 0.05, "tolerance": 8})
    checks = vision.look(post, vision.data_url(output.getvalue()), [entry], {},
                         folder=tmp_path, evidence_dir=tmp_path / "report")
    assert checks[0]["ok"] is False and not calls
    metrics = checks[0]["evidence"]["comparison"]
    assert metrics["changed"] == 0 and metrics["total"] == 16 and metrics["min_changed"] == 0.05
    for name in ("crop", "panel", "difference"):
        assert (tmp_path / "report" / checks[0]["evidence"][name]).is_file()


def test_a_scene_claim_receives_the_labeled_blockout_reference_and_retains_it(tmp_path):
    reference = tmp_path / "references/blockout.png"
    reference.parent.mkdir()
    reference.write_bytes(png("gray", (4, 4)))
    calls = []

    def post(url, key, body):
        calls.append(body)
        return response(body, 0.2)

    entry = claim("The scene has finished materials.", reference={"path": "references/blockout.png",
                                                               "role": "blockout"})
    check = vision.look(post, vision.data_url(png()), [entry], {}, folder=tmp_path,
                        evidence_dir=tmp_path / "report")[0]
    assert check["ok"] is False and len(calls[0]["images"]) == 2
    assert calls[0]["state"]["reference_role"] == "blockout"
    assert (tmp_path / "report" / check["evidence"]["reference"]).read_bytes() == reference.read_bytes()
    assert str(tmp_path) not in str(check)


def test_identical_blockout_cannot_pass_even_if_the_model_would_pass_it(tmp_path):
    reference = tmp_path / "blockout.png"
    reference.write_bytes(png("white", (4, 4)))
    entry = claim("The scene has finished materials.", reference={"path": "blockout.png", "role": "blockout"})
    check = vision.look(lambda *args: pytest.fail("identical block-out must fail without a model call"),
                        vision.data_url(png()), [entry], {}, folder=tmp_path,
                        evidence_dir=tmp_path / "report")[0]
    assert check["ok"] is False and "identical" in check["detail"]
    assert check["evidence"]["crop"] == check["evidence"]["reference"]


def test_a_reference_symlink_cannot_read_or_upload_an_outside_image(tmp_path):
    base = tmp_path / "suite"
    base.mkdir()
    private = tmp_path / "outside.png"
    private.write_bytes(png())
    (base / "reference.png").symlink_to(private)
    entry = claim(reference={"path": "reference.png", "role": "expected"})
    with pytest.raises(vision.VisionError, match="reference"):
        vision.look(lambda *args: pytest.fail("unsafe reference must not reach a model"), vision.data_url(png()),
                    [entry], {}, folder=base, evidence_dir=base / "report")


@pytest.mark.parametrize("probability", [True, float("nan"), float("inf"), -0.1, 1.1, "0.9"])
def test_invalid_model_probabilities_never_pass(probability):
    checks = vision.look(lambda url, key, body: response(body, probability), "data:image/jpeg;base64,AAAA",
                         ["The subject is visible."], {})
    assert checks[0]["ok"] is False


def test_web_runner_reports_mcp_and_download_keep_the_exact_claim_images(tmp_path, monkeypatch):
    import json
    import time
    from types import SimpleNamespace

    from qajev import cloud, mcp_server, report, runner

    reference = tmp_path / "blockout.png"
    reference.write_bytes(png("gray", (4, 4)))
    frame = Image.new("RGB", (8, 4), "white")
    frame.paste("black", (4, 0, 8, 4))
    buffer = io.BytesIO()
    frame.save(buffer, format="PNG")

    class Page:
        cwd = tmp_path
        jev = SimpleNamespace(model=SimpleNamespace(post_json=lambda url, key, body: response(body)))

        def set_device(self, device): pass
        def arm(self, mode, speech=None): pass
        def check_host(self, url): pass
        def navigate(self, url): self.url = url
        def probe(self, expect): return {"url": self.url, "status": 200, "probe": {}}
        def screenshot(self, path, marked=True): return None
        def screen_image_png(self): return vision.data_url(buffer.getvalue())
        def screen_image(self): pytest.fail("structured claims must use PNG, not JPEG")

    entry = claim("The panels visibly differ.", reference={"path": "blockout.png", "role": "blockout"},
                  different_from={"crop": region("control", x=4), "min_changed": 0.05})
    parsed = suite.parse({"devices": ["desktop"], "scenarios": [{"name": "panels", "about": "The panels differ.",
                        "url": "http://127.0.0.1:8765/", "expect": {"looks": [entry]}}]})
    monkeypatch.setattr(runner, "wait_for_quiet", lambda opts: (True, 0))
    folder = tmp_path / "report"
    result = runner.run_scenario(Page(), parsed.scenarios[0], opts=runner.Options(shots=False),
                                 hosts={"127.0.0.1:8765"}, run_dir=folder)
    assert result["outcome"] == "pass"
    evidence = result["checks"][0]["evidence"]
    data = report.build(parsed, [result], [], browser={}, started_at=time.time(), strict=False,
                        interrupted=False, run_dir=folder)
    report.write(folder, data)
    for filename in ("report.md", "report.html"):
        text = (folder / filename).read_text()
        assert all(evidence[key] in text for key in ("crop", "reference", "panel", "difference"))
        assert "min_changed" in text and "0.05" in text
    assert mcp_server._trim(data, False)["scenarios"][0]["checks"][0]["evidence"] == evidence

    def get_file(client, run_id, path):
        return json.dumps(data).encode() if path == "report.json" else (folder / path).read_bytes()

    monkeypatch.setattr(cloud, "get_file", get_file)
    destination = tmp_path / "download"
    _, kept = cloud.fetch_report(object(), "fixture", destination)
    assert kept == 4
    for key in ("crop", "reference", "panel", "difference"):
        assert (destination / evidence[key]).read_bytes() == (folder / evidence[key]).read_bytes()


def test_native_steps_validate_and_plan_the_same_named_claim(tmp_path):
    from qajev import native, plan

    steps = [{"name": "scene", "about": "The scene draws.", "expect": {"looks": [claim()]}}]
    native.check_looks(steps, tmp_path)
    assert plan.from_steps(steps)[0]["checks"][-1]["check"] == vision.planned(claim())["check"]
    steps[0]["expect"]["looks"] = [claim(crop=region(width=False))]
    with pytest.raises(native.NativeError, match="looks"):
        native.check_looks(steps, tmp_path)


@pytest.mark.parametrize("action", [{"js": "true"}, {"handoff": {}}, {"open_url": {}}, {"crash_renderer": True}])
def test_native_actions_that_do_not_judge_looks_refuse_the_claim(action, tmp_path):
    from qajev import native

    with pytest.raises(native.NativeError, match="cannot judge"):
        native.check_looks([{**action, "expect": {"looks": [claim()]}}], tmp_path)


def test_real_device_conversion_marks_unavailable_looks_as_skipped():
    from qajev import mobile

    parsed = suite.parse({"devices": ["desktop"], "scenarios": [{"name": "scene", "about": "The scene draws.",
                        "url": "http://127.0.0.1/", "expect": {"looks": [claim()]}}]})
    step = mobile.web_steps(parsed, "ios")[0]
    assert "looks" in step.get("skip", "")


def test_structured_web_capture_uses_unmarked_lossless_png():
    from qajev.session import Session

    page = object.__new__(Session)
    calls, evaluations = [], []
    page.evaluate = lambda expression, timeout_ms=15000: evaluations.append(expression)
    page.call = lambda method, **kwargs: calls.append((method, kwargs)) or {"data": "fixture"}
    assert page.screen_image_png() == "data:image/png;base64,fixture"
    assert calls == [("Page.captureScreenshot", {"format": "png", "fromSurface": True,
                                               "captureBeyondViewport": False})]
    assert len(evaluations) == 1 and "mark(false)" in evaluations[0]


def test_three_model_images_respect_the_combined_input_limit_before_calling_clef(tmp_path, monkeypatch):
    reference = tmp_path / "reference.png"
    reference.write_bytes(png("gray", (4, 4)))
    frame = Image.new("RGB", (8, 4), "white")
    frame.paste("black", (4, 0, 8, 4))
    output = io.BytesIO()
    frame.save(output, format="PNG")
    entry = claim(reference={"path": "reference.png", "role": "expected"},
                  different_from={"crop": region("control", x=4), "min_changed": 0.05})
    monkeypatch.setattr(vision, "MAX_TOTAL_BYTES", len(png("white", (4, 4))) + len(reference.read_bytes()))
    with pytest.raises(vision.VisionError, match="combined"):
        vision.look(lambda *args: pytest.fail("oversized image set must not reach Clef"),
                    vision.data_url(output.getvalue()), [entry], {}, folder=tmp_path,
                    evidence_dir=tmp_path / "report")


def test_native_session_resolves_reference_at_suite_folder_and_keeps_the_exact_crop(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from qajev import native
    from qajev import session as session_mod

    (tmp_path / "reference.png").write_bytes(png("gray", (4, 4)))
    monkeypatch.setattr(session_mod, "load", lambda ledger: SimpleNamespace(
        model=SimpleNamespace(post_json=lambda url, key, body: response(body))))

    class Window:
        errors = []

        def observe(self): return {"screen": "scene", "texts": [], "state": {}}
        def call(self, **kwargs): return {"ok": True}
        def shot(self, path):
            path.write_bytes(png())
            return path

    steps = [{"name": "scene", "about": "The scene is finished.", "expect": {"looks": [
        claim(reference={"path": "reference.png", "role": "expected"})]}}]
    native.check_looks(steps, tmp_path)
    folder = tmp_path / "report"
    results = native.run_session(Window(), steps, ledger=SimpleNamespace(spent=lambda: 0),
                                 run_dir=folder, shots=False, looks_folder=tmp_path)
    assert results[0]["outcome"] == "pass", results[0]["reason"]
    check = next(check for check in results[0]["checks"] if check["check"].startswith("looks:"))
    assert (folder / check["evidence"]["crop"]).is_file()
    assert (folder / check["evidence"]["reference"]).read_bytes() == (tmp_path / "reference.png").read_bytes()


def test_plan_command_refuses_invalid_native_claim_with_a_plain_reason(tmp_path, monkeypatch, capsys):
    from qajev import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QAJEV_HOME", str(tmp_path / "settings"))
    path = tmp_path / "native.yaml"
    path.write_text("""about: The scene is visible.
steps:
  - name: scene
    about: The scene is visible.
    expect:
      looks:
        - claim: The scene is visible.
          crop: {name: scene, x: 0, y: 0, width: true, height: 4}
""")
    assert cli.main(["plan", str(path), "--json"]) == cli.EXIT_CONFIG
    output = capsys.readouterr()
    assert "looks" in output.out + output.err and "Traceback" not in output.out + output.err
