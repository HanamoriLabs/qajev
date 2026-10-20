"""The report CLI writes one complete JSON document, including on failure."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

RUN = "r_reportjson"
KEY = "qjk_report_json_synthetic_key_12345678"
PRIVATE = "synthetic-private-report-content"


def run_report(tmp_path, target, *flags, dashboard=None, extra_env=None):
    """Use the installed module entry point with only synthetic settings and a temporary home."""
    env = {key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ}
    env.update(
        HOME=str(tmp_path),
        QAJEV_HOME=str(tmp_path / "qajev-home"),
        QAJEV_CLOUD_KEY=KEY,
        QAJEV_CLOUD_URL=dashboard.url if dashboard else "http://127.0.0.1:1",
    )
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-B", "-m", "qajev", "report", str(target), *flags],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def assert_json_error(result, code):
    assert result.returncode == code
    data = json.loads(result.stdout)  # complete stdout: extra text or a second document must fail
    assert data["exit_code"] == code and isinstance(data["error"], str) and data["error"]
    assert "qajev:" in result.stderr and "Traceback" not in result.stderr
    return data


class ReportDashboard:
    """Only read-only report downloads exist; an unexpected endpoint fails the test."""

    def __init__(self):
        self.files = {}
        self.errors = {}
        self.calls = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.calls.append((self.command, self.path, self.headers.get("Authorization")))
                name = self.path.removeprefix(f"/api/v1/runs/{RUN}/files/")
                if name in outer.errors:
                    code, message = outer.errors[name]
                    body = json.dumps({"error": message}).encode()
                elif name in outer.files:
                    code, body = 200, outer.files[name]
                else:
                    code, body = 404, b'{"error":"no report file"}'
                self.send_response(code)
                self.send_header("Content-Type", "application/json" if code != 200 else "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def dashboard():
    dash = ReportDashboard()
    yield dash
    dash.close()


def test_local_json_is_one_document_and_preserves_the_payload(tmp_path):
    data = {"gate": "PASS", "nested": {"text": "a } brace and café", "values": [None, True, 1, 0.5]}}
    (tmp_path / "report.json").write_text("\n " + json.dumps(data, ensure_ascii=False) + "\n\t")
    result = run_report(tmp_path, tmp_path, "--json")
    assert result.returncode == 0 and json.loads(result.stdout) == data
    assert result.stderr == ""


@pytest.mark.parametrize(
    "raw", [b'{"gate":"PASS"}\n{"gate":"FAIL"}', b'{"private_source":"' + PRIVATE.encode() + b'",\n broken}', b"\xff"]
)
def test_local_invalid_json_is_one_safe_error_not_a_repaired_report(tmp_path, raw):
    (tmp_path / "report.json").write_bytes(raw)
    result = run_report(tmp_path, tmp_path, "--json")
    data = assert_json_error(result, 3)
    assert "report.json" in data["error"] and "JSON" in data["error"]
    assert PRIVATE not in result.stdout + result.stderr


def test_local_missing_json_is_one_error_document(tmp_path):
    result = run_report(tmp_path, tmp_path, "--json")
    assert "report.json" in assert_json_error(result, 3)["error"]


def test_local_unreadable_json_is_one_error_document(tmp_path):
    (tmp_path / "report.json").mkdir()
    result = run_report(tmp_path, tmp_path, "--json")
    assert "report.json" in assert_json_error(result, 3)["error"]


def test_cloud_json_has_no_diagnostics_and_keeps_all_companion_files(tmp_path, dashboard):
    data = {
        "gate": "PASS",
        "scenarios": [{"shot": "shots/end.jpg", "react": [{"path": "shots/frame.jpg"}]}],
        "details": {"text": "a } brace", "values": [None, True, 3]},
    }
    dashboard.files = {
        "report.json": json.dumps(data).encode(),
        "report.md": b"# report\n",
        "report.html": b"<!doctype html><title>report</title>",
        "shots/end.jpg": b"end",
        "shots/frame.jpg": b"frame",
    }
    result = run_report(tmp_path, RUN, "--json", dashboard=dashboard)
    assert result.returncode == 0 and json.loads(result.stdout) == data
    assert result.stderr == ""
    folder = tmp_path / "qajev-home/cloud-reports" / RUN
    for name, raw in dashboard.files.items():
        assert (folder / name).read_bytes() == raw
    assert {path for method, path, auth in dashboard.calls} == {
        f"/api/v1/runs/{RUN}/files/{name}" for name in dashboard.files
    }
    assert all(method == "GET" and auth == f"Bearer {KEY}" for method, path, auth in dashboard.calls)


def test_cloud_missing_report_is_one_error_document(tmp_path, dashboard):
    result = run_report(tmp_path, RUN, "--json", dashboard=dashboard)
    data = assert_json_error(result, 4)
    assert "no report" in data["error"] and RUN in data["error"]


@pytest.mark.parametrize(
    "raw", [b'{"scenarios":[]}\n{"extra":true}', b'{"private_source":"' + PRIVATE.encode() + b'",\n broken}', b"\xff"]
)
def test_cloud_malformed_report_is_one_safe_error_document(tmp_path, dashboard, raw):
    dashboard.files["report.json"] = raw
    result = run_report(tmp_path, RUN, "--json", dashboard=dashboard)
    data = assert_json_error(result, 4)
    assert "report.json" in data["error"] and "JSON" in data["error"]
    assert PRIVATE not in result.stdout + result.stderr


def test_cloud_transport_error_is_redacted_on_both_streams(tmp_path, dashboard):
    secret = "synthetic-diagnostic-secret-12345"
    dashboard.errors["report.json"] = (503, f"temporary failure {KEY} {secret}")
    result = run_report(tmp_path, RUN, "--json", dashboard=dashboard, extra_env={"TYPESAFE_API_KEY": secret})
    assert "503" in assert_json_error(result, 4)["error"]
    assert "[redacted]" in result.stdout and "[redacted]" in result.stderr
    assert KEY not in result.stdout + result.stderr and secret not in result.stdout + result.stderr


def test_cloud_connection_error_is_one_error_document(tmp_path):
    # This reserved loopback port has no fake dashboard; no external service is used.
    result = run_report(tmp_path, RUN, "--json")
    assert "does not answer" in assert_json_error(result, 4)["error"]


@pytest.mark.parametrize("cloud_target", [False, True])
def test_html_and_json_is_one_error_without_rebuilding_or_fetching(tmp_path, dashboard, cloud_target):
    (tmp_path / "report.json").write_text('{"scenarios":[]}')
    result = run_report(tmp_path, RUN if cloud_target else tmp_path, "--html", "--json", dashboard=dashboard)
    data = assert_json_error(result, 3)
    assert "--html" in data["error"] and "--json" in data["error"]
    assert not (tmp_path / "report.html").exists() and dashboard.calls == []


def test_markdown_output_and_cloud_confirmation_stay_separate(tmp_path, dashboard):
    raw = "# report\nA complete result.\n"
    (tmp_path / "report.md").write_text(raw)
    local = run_report(tmp_path, tmp_path)
    assert local.returncode == 0 and local.stdout == raw + "\n" and local.stderr == ""
    dashboard.files = {"report.json": b'{"scenarios":[]}', "report.md": raw.encode()}
    result = run_report(tmp_path, RUN, dashboard=dashboard)
    assert result.returncode == 0 and result.stdout == raw + "\n"
    assert "kept in" in result.stderr and "0 screenshots" in result.stderr
