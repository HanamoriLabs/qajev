import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace
from typing import cast

import pytest

from qajev import cli, cloud, config, jobs, report, runner


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    # This tests rejection before a browser/model is needed, without reading any operator settings.
    monkeypatch.setattr(config, "load_env", lambda *a, **kw: [])
    monkeypatch.setattr(config, "reload_env", lambda: [])
    monkeypatch.setattr(jobs, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(cli, "_lower_priority", lambda: None)

    def unexpected(*a, **kw):
        pytest.fail("a rejected suite must not open a browser or submit a cloud run")

    monkeypatch.setattr(runner.chrome, "start", unexpected)
    monkeypatch.setattr(cloud, "connect", unexpected)


def suite_file(tmp_path, text=None, suffix=".yaml"):
    path = tmp_path / ("suite" + suffix)
    path.write_text(text or """name: Rejected fixture
base_url: http://127.0.0.1:8877
scenarios:
  - name: planted fault
    url: /
    about: Confirms a planted fault is detected.
    expect_fail: missing check
    expect:
      text: Welcome
""")
    return path


def read_report(out):
    reports = list(out.rglob("report.json"))
    assert len(reports) == 1, "a rejected suite must leave exactly one report"
    data = json.loads(reports[0].read_text())
    folder = reports[0].parent
    md = (folder / "report.md").read_text()
    html = (folder / "report.html").read_text()
    assert data["gate"] == "INCOMPLETE"
    assert data["exit_code"] != 0
    assert data["scenarios"] == [] and data["findings"] == []
    assert data["cost"]["usd"] == 0 and not any(data["cost"]["calls"].values())
    assert data["error"] in md
    assert "Gate: INCOMPLETE" in md and "Gate: INCOMPLETE" in html
    return data, md, html


def test_invalid_expect_fail_writes_error_report_and_failed_job(tmp_path, capsys):
    path = suite_file(tmp_path)
    out = tmp_path / "reports"
    assert cli.main(["run", str(path), "--runner", "local", "--out", str(out), "--quiet"]) == 3
    data, md, html = read_report(out)
    assert data["outcome"] == "suite_error"
    assert "must name exactly one" in md and "it names 0" in md
    assert path.name in md and "must name exactly one" in html
    job = jobs.listing()[0]
    assert job["state"] == "failed" and job["exit_code"] == 3 and job["gate"] == "INCOMPLETE"
    assert job["run_dir"] == data["run_dir"]
    assert job["error"] == data["error"]
    assert "report.md" in capsys.readouterr().err


@pytest.mark.parametrize(("text", "suffix", "reason"), [
    ("scenarios:\n  - expect: [\n", ".yaml", "line 3"),
    ("base_url: http://127.0.0.1:8877\nscenarios: *missing\n", ".yaml", "alias"),
    ('{"scenarios": [}', ".json", "line 1"),
])
def test_malformed_suite_writes_one_line_reason_without_traceback(tmp_path, capsys, text, suffix, reason):
    path = suite_file(tmp_path, text, suffix)
    out = tmp_path / "reports"
    assert cli.main(["run", str(path), "--runner", "local", "--out", str(out)]) == 3
    data, md, _ = read_report(out)
    assert path.name in md and reason in data["error"]
    assert "\n" not in data["error"]
    assert "Traceback" not in capsys.readouterr().err


def test_malformed_yaml_never_quotes_an_unknown_plain_secret_in_any_output(tmp_path, monkeypatch, capsys):
    import yaml

    secret = "violet marble canoe"
    path = suite_file(tmp_path, f"scenarios: [{secret}\n")
    # No configured secret or token pattern can hide a raw parser snippet for this test.
    monkeypatch.setattr(config, "secret_values", lambda: [])
    with pytest.raises(yaml.YAMLError) as error:
        yaml.safe_load(path.read_text())
    assert secret in str(error.value), "the malformed line must appear in the parser's unsafe diagnostic"

    out = tmp_path / "reports"
    assert cli.main(["run", str(path), "--runner", "local", "--out", str(out), "--json"]) == 3
    printed = capsys.readouterr()
    data, md, html = read_report(out)
    raw_json = next(out.rglob("report.json")).read_text()
    assert "expected" in data["error"] and "line" in data["error"] and "column" in data["error"]
    assert json.loads(printed.out) == data
    for channel, text in (("stdout", printed.out), ("stderr", printed.err), ("report.md", md),
                          ("report.json", raw_json), ("report.html", html)):
        assert secret not in text, f"{channel} quoted the malformed suite's private content"


def test_cli_failure_redacts_console_json_and_job_metadata_without_the_report_layer(monkeypatch, capsys):
    secret = "fixture-cli-private-value-7183"
    monkeypatch.setenv("QAJEV_FAKE_TOKEN", secret)
    recorded = []
    monkeypatch.setattr(cli, "_record", recorded.append)

    def no_report(*args, **kwargs):
        pytest.fail("the CLI redaction boundary must be tested without the report layer")

    monkeypatch.setattr(report, "suite_error", no_report)
    args = SimpleNamespace(command="run", json=True)
    assert cli._failure(args, f"setup refused {secret}", 3,
                        extra={"diagnostic": {"value": secret}}, report=False) == 3
    printed = capsys.readouterr()
    for text in (printed.out, printed.err, json.dumps(recorded)):
        assert secret not in text
        assert "[redacted]" in text
    assert json.loads(printed.out) == recorded[0]


def test_missing_suite_writes_report(tmp_path):
    out = tmp_path / "reports"
    assert cli.main(["run", str(tmp_path / "absent.yaml"), "--runner", "local", "--out", str(out)]) == 3
    assert "suite not found" in read_report(out)[1]


def test_preflight_error_is_one_json_document_and_redacted_before_all_outputs(tmp_path, monkeypatch, capsys):
    secret = "fixture-rejection-secret-0123456789"
    monkeypatch.setenv("QAJEV_FAKE_TOKEN", secret)
    path = suite_file(tmp_path)
    path.write_text(path.read_text().replace("missing check", secret))
    path = path.rename(tmp_path / f"{secret}.yaml")
    out = tmp_path / "reports" / secret
    assert cli.main(["run", str(path), "--runner", "local", "--out", str(out), "--json"]) == 3
    printed = capsys.readouterr()
    data, md, html = read_report(out)
    assert json.loads(printed.out) == data
    texts = [printed.out, printed.err, md, html]
    texts.extend(p.read_text() for p in (tmp_path / "jobs").rglob("result.json"))
    for text in texts:
        assert secret not in text


def test_safety_refusal_still_exits_five_and_leaves_report(tmp_path, capsys):
    path = suite_file(tmp_path, "scenarios:\n  - url: https://example.com/\n    goal: x\n    mode: mutate\n")
    out = tmp_path / "reports"
    assert cli.main(["run", str(path), "--runner", "local", "--out", str(out), "--json"]) == 5
    data, md, _ = read_report(out)
    assert data["outcome"] == "refused" and data["exit_code"] == 5
    assert "not a local dev host" in md
    assert json.loads(capsys.readouterr().out) == data


def test_setup_error_writes_report_without_inventing_product_failures(tmp_path, monkeypatch):
    path = suite_file(tmp_path, "scenarios:\n  - url: http://127.0.0.1:8877/\n    expect: {text: Ready}\n")
    out = tmp_path / "reports"

    def not_ready(*a):
        raise runner.ConfigError("the required test provider is unavailable")

    monkeypatch.setattr(runner, "run", not_ready)
    assert cli.main(["run", str(path), "--runner", "local", "--out", str(out)]) == 3
    data, md, _ = read_report(out)
    assert "test provider is unavailable" in md
    assert not any(data["counts"].values())


def test_cloud_preflight_rejection_writes_report_without_submitting(tmp_path):
    path = suite_file(tmp_path)
    out = tmp_path / "reports"
    assert cli.main(["run", str(path), "--runner", "cloud", "--out", str(out)]) == 3
    data, md, _ = read_report(out)
    assert data["runner"] == "cloud"
    assert "must name exactly one" in md


def test_error_report_does_not_overwrite_a_previous_attempt(tmp_path):
    path = suite_file(tmp_path)
    out = tmp_path / "reports"
    for _ in range(2):
        assert cli.main(["run", str(path), "--runner", "local", "--out", str(out)]) == 3
    assert len(list(out.rglob("report.md"))) == 2


def test_real_module_entry_point_preserves_rejection_exit(tmp_path):
    # Exercise __main__ rather than only a Python return value. No browser, key or env-file read.
    path = suite_file(tmp_path)
    out = tmp_path / "reports"
    script = ("import runpy; from qajev import config; "
              "config.load_env=lambda *a,**k: []; runpy.run_module('qajev',run_name='__main__')")
    proc = subprocess.run([sys.executable, "-c", script, "run", str(path), "--runner", "local",
                           "--out", str(out), "--quiet"], capture_output=True, text=True, timeout=20)
    assert proc.returncode == 3
    assert "must name exactly one" in proc.stderr
    assert "must name exactly one" in read_report(out)[1]


def test_mcp_keeps_rejected_suite_reason_and_report_paths(tmp_path, monkeypatch):
    from qajev import mcp_server

    data = {"outcome": "suite_error", "error": "invalid planted check", "reason": "invalid planted check",
            "gate": "INCOMPLETE", "exit_code": 3, "run_dir": str(tmp_path), "scenarios": []}
    monkeypatch.setattr(jobs, "start", lambda *a, **kw: {"id": "fixture-job"})
    monkeypatch.setattr(jobs, "events_since", lambda *a: ([], 0))
    monkeypatch.setattr(jobs, "result", lambda *a: data)
    monkeypatch.setattr(jobs, "status", lambda *a: {"state": "failed", "error": data["error"]})
    out = mcp_server._trim(asyncio.run(mcp_server._run_report([], None)), False)
    assert out["gate"] == "INCOMPLETE" and out["exit_code"] == 3
    assert out["error"] == "invalid planted check"
    assert out["report_md"] == str(tmp_path / "report.md")


def test_mcp_cloud_validation_writes_report_without_submission(tmp_path):
    from qajev import mcp_server

    path = suite_file(tmp_path)
    out = tmp_path / "reports"
    ctx = cast(mcp_server.Context, None)
    result = asyncio.run(mcp_server.qa_run_suite(ctx, suite_path=str(path), out_dir=str(out), runner="cloud"))
    data, _, _ = read_report(out)
    assert result["outcome"] == "suite_error" and result["exit_code"] == 3
    assert result["report_md"] == str(out / data["run_dir"].split("/")[-1] / "report.md")


def test_unwritable_report_keeps_original_failure_and_exit(tmp_path, monkeypatch, capsys):
    path = suite_file(tmp_path)
    out = tmp_path / "not-a-directory"
    out.write_text("unrelated existing file")
    assert cli.main(["run", str(path), "--runner", "local", "--out", str(out)]) == 3
    err = capsys.readouterr().err
    assert "must name exactly one" in err and "could not write" in err
    assert out.read_text() == "unrelated existing file"


def test_invalid_utf8_suite_writes_clear_error_report(tmp_path):
    path = tmp_path / "invalid.yaml"
    path.write_bytes(b"scenarios: \xff\n")
    out = tmp_path / "reports"
    assert cli.main(["run", str(path), "--runner", "local", "--out", str(out)]) == 3
    assert "UTF-8" in read_report(out)[1]


def test_cloud_preflight_does_not_replace_the_previous_jobs_result(tmp_path):
    path = suite_file(tmp_path)
    out = tmp_path / "reports"
    assert cli.main(["run", str(path), "--runner", "local", "--out", str(out)]) == 3
    result_file = next((tmp_path / "jobs").rglob("result.json"))
    previous = result_file.read_bytes()
    assert cli.main(["run", str(path), "--runner", "cloud", "--out", str(out)]) == 3
    assert result_file.read_bytes() == previous


def test_mcp_unwritable_report_keeps_plain_original_cause(tmp_path):
    from qajev import mcp_server

    path = suite_file(tmp_path)
    out = tmp_path / "not-a-directory"
    out.write_text("existing file")
    ctx = cast(mcp_server.Context, None)
    with pytest.raises(mcp_server.ToolError, match="must name exactly one.*could not write"):
        asyncio.run(mcp_server.qa_run_suite(ctx, suite_path=str(path), out_dir=str(out), runner="cloud"))


def test_closed_json_output_cannot_turn_suite_rejection_into_exit_zero(tmp_path, monkeypatch):
    path = suite_file(tmp_path)

    class ClosedOutput:
        def write(self, text):
            raise BrokenPipeError("reader closed stdout")

        def flush(self):
            pass

    monkeypatch.setattr(cli.sys, "stdout", ClosedOutput())
    assert cli.main(["run", str(path), "--runner", "local", "--out", str(tmp_path / "reports"), "--json"]) == 3


def test_error_report_redacts_before_normalizing_or_shortening(tmp_path, monkeypatch):
    secret = "fixture-rejection-line-one\nline-two.json"
    monkeypatch.setenv("QAJEV_FAKE_TOKEN", secret)
    out = tmp_path / "reports"
    report.suite_error(out, tmp_path / secret, f"invalid setting: {secret}", 3)
    for path in out.rglob("report.*"):
        text = path.read_text()
        assert "fixture-rejection-line-one" not in text
        assert "line-two" not in text
        assert "[redacted]" in text
