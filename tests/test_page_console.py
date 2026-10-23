"""Page log/info measurements use the same bounded scenario diagnostics as warn/error."""

import json
import http.server
import os
import threading
import time
from urllib.parse import urlsplit

import pytest
from test_check_evidence import run_page
from test_ux_dom_live import ENDPOINT
from test_ux_dom_live import no_model_initialization as no_model_initialization
from test_ux_dom_live import page as page

from qajev import report, runner, suite


@pytest.fixture
def site():
    body = b'<!doctype html><link rel="icon" href="data:,"><h1>Page console fixture with visible explanatory text</h1>'

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("level", ["log", "info"])
def test_runner_keeps_page_log_and_info_measurements(monkeypatch, level):
    text = 'measured {"difference":0.29,"minimum":0.3}'
    result = run_page(monkeypatch, {"page_console": [{"level": level, "text": text}]})
    assert result.get("page_console") == [{"level": level, "text": text}]


@pytest.mark.skipif(not ENDPOINT or os.uname().sysname != "Linux", reason="requires approved CPU fixture browser")
@pytest.mark.parametrize("level", ["log", "info"])
def test_page_log_and_info_reach_the_report_without_changing_the_verdict(page, site, level):
    session, folder = page
    session.hosts = {urlsplit(site).netloc}
    session.call("Page.addScriptToEvaluateOnNewDocument", source=f"""
      window.forwarded = [];
      const original = console.{level};
      console.{level} = function(...args) {{
        window.forwarded.push(this === console); original.apply(this, args); return 17;
      }};
    """)
    parsed = suite.parse({"scenarios": [{
        "name": "page " + level, "url": site + "/ux-clean.html", "about": "logging measurements leaves a true check passing",
        "before": [{"js": f"window.consoleReturn = console.{level}('measured', {{difference:0.29,minimum:0.3}})"}],
        "settle": 0, "expect": {"js": "true", "says": {"js": "The local fixture check returns true"}},
    }]})
    scenario = parsed.scenarios[0]
    result = runner.run_scenario(session, scenario, opts=runner.Options(shots=False, ux="off", load_high=None),
                                 hosts=session.hosts, run_dir=folder)
    assert result["outcome"] == "pass" and not result["findings"]
    assert session.evaluate("window.consoleReturn") == 17 and session.evaluate("window.forwarded") == [True]
    lines = result.get("page_console", [])
    assert len(lines) == 1 and lines[0]["level"] == level
    assert json.loads(lines[0]["text"].removeprefix("measured ")) == {"difference": 0.29, "minimum": 0.3}
    data = report.build(parsed, [result], [], browser={}, started_at=time.time(),
                        strict=False, interrupted=False, run_dir=folder)
    report.write(folder, data)
    saved = json.loads((folder / "report.json").read_text())
    assert saved["gate"] == "PASS" and saved["scenarios"][0]["page_console"] == lines
    assert "0.29" in (folder / "report.html").read_text() and "Page console" in (folder / "report.html").read_text()
