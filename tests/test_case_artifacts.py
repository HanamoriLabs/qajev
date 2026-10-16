"""Named case exports use real encodings and whole attachments, never check previews."""
import base64
import hashlib
import json
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from qajev.session import Session
from qajev.suite import SuiteError, parse


def png_bytes():
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x12\x34\x56")) + chunk(b"IEND", b""))


class PngPage(Session):
    def __init__(self):
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        raw = png_bytes() if params.get("format") == "png" else b"\xff\xd8jpeg-lossy-fixture"
        return {"data": base64.b64encode(raw).decode()}

    def evaluate(self, expression, timeout_ms=15000):
        return 0

    def begin_test(self):
        pass

    def set_color_scheme(self, scheme):
        pass


def case(capture):
    return parse({"device": "desktop", "base_url": "http://127.0.0.1:8937", "scenarios": [
        {"name": "basket", "about": "Keep the final basket state.", "expect": {"text": "Basket"}, "capture": capture},
    ]}).scenarios[0]


def test_named_capture_is_opt_in_and_keeps_check_contracts():
    spec = case({"png": "basket-screen", "json": {"name": "basket-state", "js": "app.state"}})
    assert spec.capture == {"png": "basket-screen", "json": {"name": "basket-state", "js": "app.state"}}
    plain = parse({"device": "desktop", "scenarios": [{"url": "http://localhost", "expect": {"text": "Home"}}]})
    assert plain.scenarios[0].capture == {}


def test_png_kind_is_lossless_png_bytes_and_has_no_jpeg_quality(tmp_path):
    page = PngPage()
    path = page.screenshot(tmp_path / "named.png", format="png")
    assert path.read_bytes() == png_bytes()
    assert page.calls == [("Page.captureScreenshot", {"format": "png"})]


def test_legacy_screenshot_stays_jpeg_quality_70(tmp_path):
    page = PngPage()
    page.screenshot(tmp_path / "legacy.jpg")
    assert page.calls == [("Page.captureScreenshot", {"format": "jpeg", "quality": 70})]


@pytest.mark.parametrize("capture", [None, True, [], {}, {"jpg": "screen"}, {"png": "../escape"},
                                      {"png": "named.png"}, {"json": "app.state"},
                                      {"json": {"name": "state"}}, {"json": {"name": "state", "js": ""}},
                                      {"json": {"name": "state", "js": "state", "extra": True}}])
def test_invalid_capture_configuration_is_refused(capture):
    with pytest.raises(SuiteError):
        case(capture)


def test_capture_is_not_silently_accepted_for_multiplayer_or_real_devices():
    for extra in [{"clients": ["one"], "state": "({ok: true})"}, {}]:
        data = {"base_url": "http://localhost", "device": "desktop", "scenarios": [
            {"url": "/", "expect": {"text": "Home"}, "capture": {"png": "end"}, **extra}]}
        if not extra:
            data["real_devices"] = ["ios"]
        with pytest.raises(SuiteError, match="capture"):
            parse(data)


def test_png_capture_metadata_names_the_file_and_case_without_collisions(tmp_path):
    from qajev.artifacts import capture

    spec = case({"png": "basket-screen"})
    files, errors = capture(PngPage(), spec, tmp_path)
    assert errors == []
    assert len(files) == 1
    artifact = files[0]
    assert artifact["name"] == "basket-screen" and artifact["kind"] == "png"
    assert artifact["path"].startswith("artifacts/") and artifact["path"].endswith("/basket-screen.png")
    assert (tmp_path / artifact["path"]).read_bytes() == png_bytes()
    spec.name += " (phone)"
    other, errors = capture(PngPage(), spec, tmp_path)
    assert errors == [] and other[0]["path"] != artifact["path"]


def test_no_shots_cannot_silently_capture_or_omit_a_required_png(tmp_path):
    from qajev.artifacts import capture

    page = PngPage()
    files, errors = capture(page, case({"png": "screen"}), tmp_path, shots=False)
    assert files == [] and "no-shots" in errors[0]
    assert page.calls == []


def test_capture_folder_symlinks_are_refused(tmp_path):
    from qajev.artifacts import capture

    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "artifacts").symlink_to(outside, target_is_directory=True)
    files, errors = capture(PngPage(), case({"png": "screen"}), tmp_path)
    assert files == [] and errors
    assert list(outside.iterdir()) == []


def test_export_failure_makes_a_passing_case_harness_and_keeps_checks_short(tmp_path, monkeypatch):
    from qajev import runner

    class Page:
        def set_device(self, device): pass
        def arm(self, mode, speech=None): pass
        def check_host(self, url): pass
        def navigate(self, url): self.url = url
        def probe(self, expect): return {"url": self.url, "status": 200, "text": [True], "probe": {}}
        def screenshot(self, path, **kwargs): return None
        def why_failed(self, url): return None

    monkeypatch.setattr(runner, "wait_for_quiet", lambda opts: (True, 1.0))
    result = runner.run_scenario(Page(), case({"png": "screen"}), opts=runner.Options(),
                                 hosts={"127.0.0.1"}, run_dir=tmp_path)
    assert result["checks"][0]["ok"]
    assert result["outcome"] == "harness" and "capture" in result["reason"].lower()
    assert "artifacts" not in result or result["artifacts"] == []


def test_artifact_links_are_in_markdown_and_html_without_inlining_state():
    from qajev import report, report_html

    files = [{"name": "basket-state", "kind": "json", "path": "artifacts/basket-123456789abc/basket-state.json",
              "bytes": 1000, "sha256": "a" * 64}]
    scenario = {"name": "basket", "outcome": "pass", "reason": "All checks passed", "seconds": 1,
                "artifacts": files, "checks": [], "findings": []}
    data = {"scenarios": [scenario], "findings": [], "gate": "PASS", "counts": {}, "cost": {}, "suite": "fixture"}
    page = report_html._scenario(0, scenario)
    assert 'href="artifacts/basket-123456789abc/basket-state.json"' in page
    assert "basket-state" in page and "JSON" in page
    # Reuse the same complete report fixture as existing report tests.
    data = json.loads((Path(__file__).parent / "fixtures/six-game-report.json").read_text())
    data["browser"] = {}
    data["qajev"] = "test"
    data["scenarios"][0]["artifacts"] = files
    assert "[basket-state](artifacts/basket-123456789abc/basket-state.json)" in report.markdown(data)


def test_unsafe_artifact_paths_never_become_report_links():
    from qajev import report_html

    scenario = {"name": "basket", "outcome": "pass", "reason": "All checks passed", "checks": [],
                "artifacts": [{"name": "bad", "kind": "json", "path": "javascript:alert(1)",
                               "bytes": 2, "sha256": "a" * 64}]}
    assert "javascript:" not in report_html._scenario(0, scenario)


@pytest.fixture
def javascript():
    # A JS engine only: no browser, DOM, network, model or daemon. It executes the actual export expressions.
    program = """const readline = require('node:readline');
    (async () => { for await (const line of readline.createInterface({input: process.stdin})) {
      try { const value = await (0,eval)(JSON.parse(line)); process.stdout.write(JSON.stringify({value})+'\\n'); }
      catch { process.stdout.write(JSON.stringify({error: true})+'\\n'); }
    } })();"""
    assert shutil.which("node"), "Node is required for the offline JSON serialization proof"
    process = subprocess.Popen(["node", "-e", program], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)

    class Engine:
        messages = []

        def evaluate(self, expression):
            message = json.dumps(expression) + "\n"
            assert len(message.encode()) < 64 * 1024
            self.messages.append(message)
            process.stdin.write(message)
            process.stdin.flush()
            reply = json.loads(process.stdout.readline())
            if reply.get("error"):
                raise RuntimeError("fixture script failed")
            return reply.get("value")

    try:
        yield Engine()
    finally:
        process.stdin.close()
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


def test_full_json_is_chunked_without_200_character_or_daemon_line_truncation(tmp_path, javascript):
    from qajev.artifacts import capture

    spec = case({"json": {"name": "scene-state", "js": "({label:'雪', data:'🌱'.repeat(40000), tail:'LAST_VALUE'})"}})
    files, errors = capture(javascript, spec, tmp_path)
    assert errors == [] and len(files) == 1
    payload = (tmp_path / files[0]["path"]).read_bytes()
    assert json.loads(payload) == {"label": "雪", "data": "🌱" * 40000, "tail": "LAST_VALUE"}
    assert len(payload) > 64 * 1024 and len(javascript.messages) > 10
    assert files[0]["bytes"] == len(payload) and files[0]["sha256"] == hashlib.sha256(payload).hexdigest()
    remains = javascript.evaluate("Object.getOwnPropertyNames(globalThis).some(k => k.startsWith('__qajev_export_'))")
    assert remains is False


def test_oversize_json_is_a_plain_error_not_a_cut_attachment(tmp_path, javascript, monkeypatch):
    from qajev import artifacts

    monkeypatch.setattr(artifacts, "MAX_BYTES", 64)
    files, errors = artifacts.capture(javascript, case({"json": {"name": "state", "js": "({data:'x'.repeat(500)})"}}),
                                      tmp_path)
    assert files == []
    assert errors == ["JSON capture exceeds the 64-byte file limit; no snapshot was written"]
    assert list(tmp_path.rglob("*.json")) == []


@pytest.mark.parametrize("expression", ["undefined", "1n", "(()=>{const x={};x.self=x;return x})()",
                                         "Promise.reject(new Error('private-canary'))"])
def test_unserializable_json_is_a_plain_error_without_raw_values(tmp_path, javascript, expression):
    from qajev.artifacts import capture

    files, errors = capture(javascript, case({"json": {"name": "state", "js": expression}}), tmp_path)
    assert files == [] and errors == ["JSON capture must return a serializable JSON value"]
    assert list(tmp_path.rglob("*.json")) == []


def test_report_download_keeps_json_and_png_and_verifies_their_bytes(tmp_path, monkeypatch):
    from qajev import cloud

    exports, bodies = [], {}
    for kind, body in [("png", png_bytes()), ("json", json.dumps({"tail": "whole", "state": "X" * 1000}).encode())]:
        path = f"artifacts/case-123456789abc/named.{kind}"
        bodies[path] = body
        exports.append({"name": "named", "kind": kind, "path": path, "bytes": len(body),
                        "sha256": hashlib.sha256(body).hexdigest()})
    bodies.update({"report.json": json.dumps({"scenarios": [{"artifacts": exports}]}).encode(),
                   "report.html": b"report", "report.md": b"report"})
    monkeypatch.setattr(cloud, "get_file", lambda client, run, path: bodies[path])
    data, images = cloud.fetch_report(None, "r_fixture", tmp_path)
    assert images == 1
    for path in [e["path"] for e in exports]:
        assert (tmp_path / path).read_bytes() == bodies[path]


def test_report_download_rejects_tampered_artifact_bytes(tmp_path, monkeypatch):
    from qajev import cloud

    path = "artifacts/case-123456789abc/state.json"
    data = {"scenarios": [{"artifacts": [{"name": "state", "kind": "json", "path": path,
              "bytes": 2, "sha256": hashlib.sha256(b"{}").hexdigest()}]}]}
    monkeypatch.setattr(cloud, "get_file", lambda client, run, name: json.dumps(data).encode() if name == "report.json"
                        else b"X" if name == path else b"report")
    with pytest.raises(cloud.CloudError, match="manifest"):
        cloud.fetch_report(None, "r_fixture", tmp_path)
    assert not (tmp_path / path).exists()


def test_png_capture_does_not_publish_an_oversized_image(tmp_path, monkeypatch):
    from qajev import artifacts

    monkeypatch.setattr(artifacts, "MAX_BYTES", 16)
    files, errors = artifacts.capture(PngPage(), case({"png": "screen"}), tmp_path)
    assert files == [] and errors == ["PNG capture exceeds the 16-byte file limit"]
    assert not list(tmp_path.rglob("*.png")) and not list(tmp_path.rglob(".capture-*"))


def test_existing_capture_target_is_never_followed_or_overwritten(tmp_path):
    from qajev.artifacts import capture

    spec = case({"png": "screen"})
    files, errors = capture(PngPage(), spec, tmp_path)
    assert errors == []
    target = tmp_path / files[0]["path"]
    before = target.read_bytes()
    files, errors = capture(PngPage(), spec, tmp_path)
    assert files == [] and errors and target.read_bytes() == before
    assert not list(tmp_path.rglob(".capture-*"))


def test_json_attachment_preserves_known_secret_redaction_and_all_other_state(tmp_path, javascript, monkeypatch):
    from qajev import artifacts

    monkeypatch.setattr(artifacts, "secret_values", lambda: ["fake_secret_canary"])
    files, errors = artifacts.capture(javascript, case({"json": {"name": "state",
        "js": "({credential: 'fake_secret_canary', payload:'x'.repeat(1000), tail:'kept'})"}}), tmp_path)
    assert errors == []
    body = (tmp_path / files[0]["path"]).read_bytes()
    assert json.loads(body) == {"credential": "[redacted]", "payload": "x" * 1000, "tail": "kept"}
    assert files[0]["sha256"] == hashlib.sha256(body).hexdigest()


def test_check_previews_stay_short_and_never_become_json_exports():
    from qajev.verdict import js_result

    ok, _, value = js_result({"value": "x" * 1000})
    assert not ok and len(value) == 200


@pytest.mark.parametrize("name", ["_leading", "___", "雪", "!!!"])
def test_case_display_names_always_produce_a_valid_artifact_manifest(tmp_path, name):
    from qajev.artifacts import capture, manifest_path

    spec = case({"png": "screen"})
    spec.name = name
    files, errors = capture(PngPage(), spec, tmp_path)
    assert errors == [] and len(files) == 1
    assert manifest_path(files[0]) == files[0]["path"]


def test_named_png_is_the_cases_main_shot_without_a_second_jpeg(tmp_path, monkeypatch):
    from qajev import runner

    class Page(PngPage):
        def set_device(self, device): pass
        def arm(self, mode, speech=None): pass
        def check_host(self, url): pass
        def navigate(self, url): self.url = url
        def probe(self, expect): return {"url": self.url, "status": 200, "text": [True], "probe": {}}
        def why_failed(self, url): return None

    monkeypatch.setattr(runner, "wait_for_quiet", lambda opts: (True, 1.0))
    page = Page()
    result = runner.run_scenario(page, case({"png": "screen"}), opts=runner.Options(),
                                 hosts={"127.0.0.1"}, run_dir=tmp_path)
    assert result["outcome"] == "pass"
    assert result["shot"] == result["artifacts"][0]["path"] and result["shot"].endswith("/screen.png")
    assert page.calls == [("Page.captureScreenshot", {"format": "png"})]
