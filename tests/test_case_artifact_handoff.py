"""Opt-in offline handoff with an explicitly selected Cloud checkout. No browser or provider request."""
import json
import os
import time
from pathlib import Path

import pytest
from test_case_artifacts import PngPage, case
from test_case_artifacts import javascript as javascript


def test_capture_report_upload_redaction_and_download_keep_the_same_complete_files(tmp_path, monkeypatch, javascript):
    source = os.environ.get("QAJEV_ARTIFACT_CLOUD_SOURCE")
    if not source:
        pytest.skip("set QAJEV_ARTIFACT_CLOUD_SOURCE for the offline cross-repo contract proof")
    source = Path(source).resolve(strict=True)
    monkeypatch.syspath_prepend(str(source))
    from qcloud import jobmap
    from qcloud import runner as cloud_runner

    from qajev import cloud, report, runner
    from qajev.ledger import Ledger

    assert Path(jobmap.__file__).resolve().is_relative_to(source)

    class Page(PngPage):
        def set_device(self, device): pass
        def arm(self, mode, speech=None): pass
        def check_host(self, url): pass
        def navigate(self, url): self.url = url
        def probe(self, expect): return {"url": self.url, "status": 200, "text": [True], "probe": {}}
        def why_failed(self, url): return None

        def evaluate(self, expression, timeout_ms=15000):
            return 0 if expression.startswith("window.__qajev") else javascript.evaluate(expression)

    monkeypatch.setattr(runner, "wait_for_quiet", lambda opts: (True, 1.0))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    scenario = case({"png": "end-screen", "json": {"name": "end-state",
        "js": "({payload:'x'.repeat(90000), tail:'LAST_VALUE', seed:'handoff_seed_canary_123'})"}})
    result = runner.run_scenario(Page(), scenario, opts=runner.Options(), hosts={"127.0.0.1"}, run_dir=run_dir)
    assert result["outcome"] == "pass" and len(result["artifacts"]) == 2
    assert all("value" not in check for check in result["checks"])
    suite = type("OfflineSuite", (), {"name": "Offline export contract; no browser run",
                                      "scenarios": [scenario]})()
    built = report.build(suite, [result], [Ledger().summary()], browser={"cdp_url": "offline fixture"},
                         started_at=time.time(), strict=False, interrupted=False, run_dir=run_dir)
    report.write(run_dir, built)
    job = tmp_path / "job"
    job.mkdir()
    (job / "result.json").write_text(json.dumps(built))
    files = jobmap.files_to_upload(job)
    cloud_runner.hide_in_files(["handoff_seed_canary_123"], files)
    jobmap.refresh_artifact_manifests(job, files)
    files = jobmap.files_to_upload(job)
    assert files[-1][0] == "report.json"
    uploaded = {name: path.read_bytes() for name, path in files}
    monkeypatch.setattr(cloud, "get_file", lambda client, run, path: uploaded[path])
    downloaded, images = cloud.fetch_report(None, "r_offlinefixture", tmp_path / "download")
    assert images == 1
    for entry in downloaded["scenarios"][0]["artifacts"]:
        assert (tmp_path / "download" / entry["path"]).read_bytes() == uploaded[entry["path"]]
        assert entry["path"] in (tmp_path / "download/report.html").read_text()
        assert entry["path"] in (tmp_path / "download/report.md").read_text()
        if entry["kind"] == "json":
            state = json.loads(uploaded[entry["path"]])
            assert state == {"payload": "x" * 90000, "tail": "LAST_VALUE", "seed": "[hidden]"}
