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


def test_console_keep_lines_stay_past_the_fifty_line_cap_on_a_cap_of_their_own():
    result = {}
    kept = [{"level": "log", "text": f"Verse boot {i}"} for i in range(60)]
    diagnostics.keep_console(result, {"page_console": [{"level": "log", "text": str(i)} for i in range(70)],
                                      "page_console_kept": kept})
    assert result["page_console_kept"] == kept and "page_console_kept_omitted" not in result
    assert len(result["page_console"]) == 50 and result["page_console_omitted"] == 20
    diagnostics.keep_console(result, {"page_console_kept": [{"level": "warn", "text": f"Verse late {i}"}
                                                            for i in range(150)], "page_console_kept_omitted": [3]})
    assert len(result["page_console_kept"]) == diagnostics.CONSOLE_KEPT_LINES == 200
    assert result["page_console_kept"][:60] == kept  # the first matching lines stay
    assert result["page_console_kept_omitted"] == 10 + 3


def test_console_keep_lines_are_cleaned_and_cut_as_the_other_lines():
    result = {}
    diagnostics.keep_console(result, {"page_console_kept": [{"level": "error", "text": "Verse " + "x" * 900},
                                                            {"level": "debug", "text": "not a level QAJev keeps"}]})
    assert result["page_console_kept"] == [{"level": "error", "text": ("Verse " + "x" * 900)[:500], "truncated": True}]


def test_console_keep_is_read_from_the_suite_and_the_test_and_refuses_what_a_page_cannot_read():
    parsed = suite.parse({"console_keep": "^boot", "scenarios": [
        {"name": "a", "url": "http://127.0.0.1/", "about": "x", "console_keep": ["Verse \\d+", "^boot"],
         "expect": {"text": "a"}},
        {"name": "b", "url": "http://127.0.0.1/", "about": "x", "expect": {"text": "a"}}]})
    assert [s.console_keep for s in parsed.scenarios] == [["^boot", "Verse \\d+"], ["^boot"]]
    for bad, words in [("(?P<n>x)", "Python-only"), ("(?i)verse", "Python-only"), ("(", "not a regular expression"),
                       (["a"] * 21, "at most 20"), ([""], "a list of patterns"), (7, "a list of patterns")]:
        with pytest.raises(suite.SuiteError, match=words):
            suite.parse({"scenarios": [{"name": "a", "url": "http://127.0.0.1/", "about": "x", "console_keep": bad,
                                        "expect": {"text": "a"}}]})


def test_console_keep_goes_to_the_guard_and_changes_its_version():
    from qajev import guard

    plain, keeping = guard.build_config(), guard.build_config(console_keep=["^Verse"])
    assert keeping["console_keep"] == ["^Verse"] and plain["console_keep"] == []
    assert plain["v"] != keeping["v"]  # a test with other patterns re-arms the guard
    assert '"console_keep": ["^Verse"]' in guard.script(keeping)


def test_the_runner_gives_the_guard_the_run_and_the_test_patterns(monkeypatch):
    from test_check_evidence import Page

    monkeypatch.setattr(runner, "wait_for_quiet", lambda _: (True, 1))
    armed = []

    class Keeping(Page):
        def arm(self, *_):
            armed.append(self.console_keep)

    scenario = suite.parse({"console_keep": "^boot", "scenarios": [{
        "name": "one", "url": "http://127.0.0.1/", "settle": 0, "console_keep": ["Verse"],
        "expect": {"js": "false"}}]}).scenarios[0]
    runner.run_scenario(Keeping({}), scenario, opts=runner.Options(shots=False, ux="off", console_keep=("^run",)),
                        hosts={"127.0.0.1"}, run_dir=None)
    assert armed == [["^run", "^boot", "Verse"]]


def test_kept_lines_reach_both_reports(tmp_path):
    parsed = suite.parse({"scenarios": [{"name": "kept", "url": "http://127.0.0.1/", "about": "x",
                                         "console_keep": ["Verse"], "expect": {"text": "a"}}]})
    result = {"name": "kept", "url": "http://127.0.0.1/", "goal": None, "mode": "readonly", "outcome": "fail",
              "reason": "the page crashed at http://127.0.0.1/; last step: reading the page", "checks": [],
              "findings": [], "screens": [], "stop": "page_crashed", "cache": "cold",
              "page_crashed": {"url": "http://127.0.0.1/", "last_step": "reading the page"},
              "page_console_kept": [{"level": "error", "text": "Verse <boot> failed"}],
              "page_console_kept_omitted": 4, "console_keep_broken": ["(?>x)"]}
    data = report.build(parsed, [result], [], browser={}, started_at=time.time(), strict=False, interrupted=False,
                        run_dir=tmp_path)
    report.write(tmp_path, data)
    html = (tmp_path / "report.html").read_text()
    assert "lines kept by console_keep" in html and "Verse &lt;boot&gt; failed" in html
    assert "4 matching lines cut (over 200)" in html and "(?&gt;x)" in html
    assert "every load skipped Chrome&#x27;s cache" in html or "every load skipped Chrome's cache" in html
    assert "Page crashed" in html
    markdown = (tmp_path / "report.md").read_text()
    assert "Page console, lines kept by console_keep:" in markdown and "error: Verse <boot> failed" in markdown
    assert "Cache: cold" in markdown
    assert json.loads((tmp_path / "report.json").read_text())["scenarios"][0]["page_console_kept"] == [
        {"level": "error", "text": "Verse <boot> failed"}]


@pytest.mark.skipif(not ENDPOINT or os.uname().sysname != "Linux", reason="requires approved CPU fixture browser")
def test_browser_keeps_matching_lines_past_the_cap_and_names_a_pattern_it_cannot_read(page, site):
    session, _ = page
    session.hosts = {urlsplit(site).netloc}
    session.console_keep = ["^Verse boot", "(?>x)"]
    session.arm("readonly")
    session.navigate(site + "/")
    session.evaluate("console.log('Verse boot 1'); for (let i = 0; i < 60; i++) console.log(String(i)); "
                     "console.error('Verse boot 2 failed'); console.warn('not Verse boot'); true")
    result = {}
    diagnostics.keep_console(result, session.probe({})["probe"])
    assert result["page_console_kept"] == [{"level": "log", "text": "Verse boot 1"},
                                           {"level": "error", "text": "Verse boot 2 failed"}]
    assert len(result["page_console"]) == 50 and result["page_console_omitted"] == 11
    assert result["page_console"][-1] == {"level": "warn", "text": "not Verse boot"}
    assert result["console_keep_broken"] == ["(?>x)"]
