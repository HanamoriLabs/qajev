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

from qajev import diagnostics, report, runner, suite


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


@pytest.mark.parametrize("first_level", ["log", "info"])
@pytest.mark.parametrize("later_level", ["log", "info"])
def test_first_fifty_routine_lines_stay_after_a_later_routine_line_in_a_second_read(first_level, later_level):
    first = [{"level": first_level, "text": f"original measurement {i}"} for i in range(50)]
    result = {}
    diagnostics.keep_console(result, {"page_console": first})
    diagnostics.keep_console(result, {"page_console": [{"level": later_level, "text": "later measurement 50"}]})
    assert result["page_console"] == first
    assert result["page_console_omitted"] == 1


@pytest.mark.parametrize("routine", ["log", "info"])
@pytest.mark.parametrize("important", ["warn", "error"])
def test_later_warning_or_error_replaces_a_routine_line_across_reads(routine, important):
    result = {}
    diagnostics.keep_console(result, {"page_console": [{"level": routine, "text": str(i)} for i in range(50)]})
    diagnostics.keep_console(result, {"page_console": [{"level": important, "text": "important diagnostic"}],
                                      "page_console_omitted": [2]})
    assert len(result["page_console"]) == 50 and result["page_console_omitted"] == 3
    assert result["page_console"][0]["text"] == "1"
    assert result["page_console"][-1] == {"level": important, "text": "important diagnostic"}


@pytest.mark.parametrize("routine", ["log", "info"])
@pytest.mark.parametrize("important", ["warn", "error"])
def test_routine_lines_cannot_evict_a_buffer_full_of_important_lines(routine, important):
    result = {}
    diagnostics.keep_console(result, {"page_console": [{"level": important, "text": str(i)} for i in range(50)]})
    diagnostics.keep_console(result, {"page_console": [{"level": routine, "text": "routine diagnostic"}]})
    assert result["page_console"] == [{"level": important, "text": str(i)} for i in range(50)]
    assert result["page_console_omitted"] == 1


def test_scenario_eviction_keeps_earlier_warnings_and_removes_only_a_routine_line():
    result = {}
    first = {"level": "warn", "text": "earlier warning"}
    diagnostics.keep_console(result, {"page_console": [first] + [{"level": "log", "text": str(i)} for i in range(49)]})
    diagnostics.keep_console(result, {"page_console": [{"level": "error", "text": "later error"}]})
    assert len(result["page_console"]) == 50 and result["page_console_omitted"] == 1
    assert result["page_console"][0] == first and result["page_console"][1]["text"] == "1"
    assert result["page_console"][-1] == {"level": "error", "text": "later error"}


@pytest.mark.skipif(not ENDPOINT or os.uname().sysname != "Linux", reason="requires approved CPU fixture browser")
@pytest.mark.parametrize("routine", ["log", "info"])
@pytest.mark.parametrize("important", ["warn", "error"])
def test_browser_keeps_later_warning_or_error_after_fifty_routine_lines(page, site, routine, important):
    session, _ = page
    session.hosts = {urlsplit(site).netloc}
    session.arm("readonly")
    session.navigate(site + "/")
    session.evaluate(f"for (let i = 0; i < 50; i++) console.{routine}(String(i)); "
                     f"console.{important}('important diagnostic'); true")
    observed = session.probe({})["probe"]
    lines = observed["page_console"]
    assert len(lines) == 50 and sum(observed["page_console_omitted"]) == 1
    assert lines[0]["text"] == "1" and lines[-1] == {"level": important, "text": "important diagnostic"}


@pytest.mark.skipif(not ENDPOINT or os.uname().sysname != "Linux", reason="requires approved CPU fixture browser")
def test_browser_eviction_keeps_earlier_warnings_and_removes_only_a_routine_line(page, site):
    session, _ = page
    session.hosts = {urlsplit(site).netloc}
    session.arm("readonly")
    session.navigate(site + "/")
    session.evaluate("console.warn('earlier warning'); for (let i = 0; i < 49; i++) console.log(String(i)); "
                     "console.error('later error'); true")
    observed = session.probe({})["probe"]
    lines = observed["page_console"]
    assert len(lines) == 50 and sum(observed["page_console_omitted"]) == 1
    assert lines[0] == {"level": "warn", "text": "earlier warning"} and lines[1]["text"] == "1"
    assert lines[-1] == {"level": "error", "text": "later error"}


@pytest.mark.skipif(not ENDPOINT or os.uname().sysname != "Linux", reason="requires approved CPU fixture browser")
@pytest.mark.parametrize("routine", ["log", "info"])
@pytest.mark.parametrize("important", ["warn", "error"])
def test_browser_routine_lines_cannot_evict_a_full_important_buffer(page, site, routine, important):
    session, _ = page
    session.hosts = {urlsplit(site).netloc}
    session.arm("readonly")
    session.navigate(site + "/")
    session.evaluate(f"for (let i = 0; i < 50; i++) console.{important}(String(i)); "
                     f"console.{routine}('routine diagnostic'); true")
    observed = session.probe({})["probe"]
    assert observed["page_console"] == [{"level": important, "text": str(i)} for i in range(50)]
    assert sum(observed["page_console_omitted"]) == 1


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
