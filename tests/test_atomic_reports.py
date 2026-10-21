"""Complete report documents and referenced images become visible before the final JSON marker."""

import json
import os
import time
import urllib.parse
from datetime import datetime, timezone
from html import escape
from types import SimpleNamespace

import pytest

from qajev import cloud, report


def document(folder, name="current", *, shot=None, detail="a complete reason"):
    scenario = {
        "name": "capture",
        "about": "every required image and reason is available",
        "outcome": "pass",
        "reason": "the capture completed",
        "seconds": 1,
        "checks": [{"check": "capture is complete", "ok": True, "detail": detail}],
        "findings": [],
        "screens": [],
    }
    if shot:
        scenario["shot"] = shot
    return report.build(
        SimpleNamespace(name=name),
        [scenario],
        [],
        browser={},
        started_at=time.time(),
        strict=False,
        interrupted=False,
        run_dir=folder,
    )


def test_render_failure_preserves_the_previous_complete_report(tmp_path, monkeypatch):
    report.write(tmp_path, document(tmp_path, "previous"))
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}

    def broken(_):
        raise RuntimeError("synthetic render failure")

    monkeypatch.setattr(report.report_html, "render", broken)
    with pytest.raises(RuntimeError, match="render failure"):
        report.write(tmp_path, document(tmp_path, "next"))
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()} == before


def test_final_json_is_published_after_both_complete_human_reports(tmp_path, monkeypatch):
    report.write(tmp_path, document(tmp_path, "previous"))
    replace = os.replace
    observed = []

    def watch(source, destination):
        if str(destination).endswith("report.json"):
            observed.append(all("next" in (tmp_path / name).read_text() for name in ("report.md", "report.html")))
        return replace(source, destination)

    monkeypatch.setattr(os, "replace", watch)
    report.write(tmp_path, document(tmp_path, "next"))
    assert observed == [True]


def test_atomic_writes_use_independent_staging_paths(tmp_path, monkeypatch):
    replace = os.replace
    sources = []

    def watch(source, destination):
        sources.append(str(source))
        return replace(source, destination)

    monkeypatch.setattr(os, "replace", watch)
    report._atomic(tmp_path / "report.json", '{"generation":1}')
    report._atomic(tmp_path / "report.json", '{"generation":2}')
    assert len(sources) == len(set(sources)) == 2
    assert json.loads((tmp_path / "report.json").read_text()) == {"generation": 2}


def test_late_partial_progress_cannot_replace_a_finished_report(tmp_path):
    data = document(tmp_path)
    report.write(tmp_path, data)
    before = (tmp_path / "report.json").read_bytes()
    partial = report.Partial(tmp_path, SimpleNamespace(name="running"), {}, time.time(), None)
    partial.add({"name": "late", "outcome": "pass"})
    assert (tmp_path / "report.json").read_bytes() == before


def test_a_missing_required_image_makes_the_report_incomplete_with_its_reason(tmp_path):
    data = document(tmp_path, shot="shots/client.jpg")
    report.write(tmp_path, data)
    saved = json.loads((tmp_path / "report.json").read_text())
    assert saved["gate"] == "INCOMPLETE" and saved["exit_code"] != 0
    assert "report_incomplete" in saved
    assert "shots/client.jpg" in json.dumps(saved["report_incomplete"])
    for name in ("report.md", "report.html"):
        assert "Report incomplete" in (tmp_path / name).read_text()
        assert "shots/client.jpg" in (tmp_path / name).read_text()


def test_the_detailed_markdown_keeps_the_complete_failure_reason(tmp_path):
    detail = "reason " * 80 + "last measured value: 0.036; expected 0.05-0.3"
    data = document(tmp_path, detail=detail)
    report.write(tmp_path, data)
    assert detail in (tmp_path / "report.md").read_text()


def test_human_reports_show_myt_even_when_the_runner_uses_another_zone(tmp_path, monkeypatch):
    stamp = datetime(2026, 10, 21, tzinfo=timezone.utc).timestamp()
    data = document(tmp_path)
    data["started_at"] = stamp
    with monkeypatch.context() as patch:
        patch.setenv("TZ", "America/Los_Angeles")
        time.tzset()
        try:
            report.write(tmp_path, data)
        finally:
            patch.undo()
            time.tzset()
    assert data["started_at"] == stamp
    for name in ("report.md", "report.html"):
        assert "2026-10-21 08:00 MYT" in (tmp_path / name).read_text()


def test_human_reports_keep_every_complete_native_diagnostic(tmp_path):
    data = document(tmp_path)
    errors = ["ERROR " + "full text " * 70 + f"final error number {i}" for i in range(12)]
    errors.append('ERROR <img src=x onerror="alert(1)">')
    data["scenarios"][0]["diagnostics"] = {"complete": True, "errors": errors, "reason": None}
    data["gate"], data["exit_code"] = "FAIL", 1
    report.write(tmp_path, data)
    for name in ("report.md", "report.html"):
        text = (tmp_path / name).read_text()
        assert "Native output" in text
        assert all((escape(error) if name.endswith("html") else error) in text for error in errors)
    assert errors[-1] not in (tmp_path / "report.html").read_text()
    assert "    " + errors[-1] in (tmp_path / "report.md").read_text()


class Download:
    def __init__(self, folder, *, missing=None):
        self.folder, self.missing, self.observed = folder, missing, []
        self.data: dict[str, object] = {
            "scenarios": [
                {
                    "name": "capture",
                    "shot": "shots/main.jpg",
                    "clients": [{"name": "second", "shot": "shots/client.jpg"}],
                    "react": [{"path": "shots/react.jpg"}],
                    "effect": {"off": "shots/off.png", "on": "shots/on.png", "difference": "shots/diff.png"},
                    "checks": [
                        {
                            "evidence": {
                                "crop": "shots/crop.png",
                                "reference": "shots/ref.png",
                                "panel": "shots/panel.png",
                                "difference": "shots/claim-diff.png",
                            }
                        }
                    ],
                }
            ]
        }
        self.paths = [
            "shots/" + name
            for name in (
                "main.jpg",
                "client.jpg",
                "react.jpg",
                "off.png",
                "on.png",
                "diff.png",
                "crop.png",
                "ref.png",
                "panel.png",
                "claim-diff.png",
            )
        ]
        self.files = {
            "report.json": json.dumps(self.data).encode(),
            "report.md": b"new markdown",
            "report.html": b"new html",
            **{name: name.encode() for name in self.paths},
        }

    def _call(self, method, path, **kwargs):
        name = urllib.parse.unquote(path.split("/files/", 1)[1])
        if name.startswith("shots/"):
            self.observed.append((self.folder / "report.json").read_text() == '{"generation":"previous"}')
        if name == self.missing or name not in self.files:
            raise cloud.ApiError(404, "synthetic missing artifact")
        return self.files[name]


def previous_download(folder):
    folder.mkdir()
    for name, data in {
        "report.json": b'{"generation":"previous"}',
        "report.md": b"old markdown",
        "report.html": b"old html",
    }.items():
        (folder / name).write_bytes(data)


def test_download_keeps_every_client_reaction_effect_and_claim_frame_before_json(tmp_path):
    folder = tmp_path / "kept"
    previous_download(folder)
    client = Download(folder)
    data, count = cloud.fetch_report(client, "r_fixture", folder)
    assert data == client.data and count == 10
    assert client.observed and all(client.observed)
    for name in client.paths:
        assert (folder / name).read_bytes() == client.files[name]


def test_failed_frame_download_keeps_the_previous_complete_artifacts(tmp_path):
    folder = tmp_path / "kept"
    previous_download(folder)
    client = Download(folder, missing="shots/react.jpg")
    before = {path.name: path.read_bytes() for path in folder.iterdir()}
    with pytest.raises(cloud.CloudError, match="required.*frame|frame.*missing|incomplete"):
        cloud.fetch_report(client, "r_fixture", folder)
    assert {path.name: path.read_bytes() for path in folder.iterdir()} == before


def test_a_missing_frame_cannot_be_hidden_on_a_passing_report(tmp_path):
    folder = tmp_path / "kept"
    previous_download(folder)
    client = Download(folder, missing="shots/react.jpg")
    client.data.update(gate="PASS", exit_code=0, report_incomplete={"missing": ["shots/react.jpg"]})
    client.files["report.json"] = json.dumps(client.data).encode()
    with pytest.raises(cloud.CloudError, match="missing|incomplete"):
        cloud.fetch_report(client, "r_fixture", folder)
    assert (folder / "report.json").read_text() == '{"generation":"previous"}'


def test_an_explicit_incomplete_report_keeps_available_evidence_and_its_reason(tmp_path):
    folder = tmp_path / "kept"
    previous_download(folder)
    client = Download(folder, missing="shots/react.jpg")
    client.data.update(
        gate="INCOMPLETE",
        exit_code=2,
        report_incomplete={"reason": "a required frame is missing", "missing": ["shots/react.jpg"]},
    )
    client.files["report.json"] = json.dumps(client.data).encode()
    data, count = cloud.fetch_report(client, "r_fixture", folder)
    assert data["gate"] == "INCOMPLETE" and data["exit_code"] == 2 and count == 9
    assert "required frame is missing" in (folder / "report.json").read_text()


def test_a_contained_reference_never_downloads_through_an_outside_symlink(tmp_path):
    folder = tmp_path / "kept"
    previous_download(folder)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "client.jpg").write_bytes(b"private control")
    (folder / "shots").symlink_to(outside, target_is_directory=True)
    client = Download(folder)
    before = (folder / "report.json").read_bytes()
    try:
        cloud.fetch_report(client, "r_fixture", folder)
    except cloud.CloudError:
        pass
    assert (outside / "client.jpg").read_bytes() == b"private control"
    assert (folder / "report.json").read_bytes() == before
