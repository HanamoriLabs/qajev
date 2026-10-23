"""Opt-in synthetic evidence fixtures on the approved headless CPU browser."""

import json
import os
from urllib.parse import urlsplit

import pytest
from test_ux_dom_live import ENDPOINT
from test_ux_dom_live import no_model_initialization as no_model_initialization
from test_ux_dom_live import page as page
from test_ux_dom_live import site as site
from test_check_evidence import LONG, document

from qajev import config, diagnostics, report, runner, suite, verdict

pytestmark = pytest.mark.skipif(not ENDPOINT or os.uname().sysname != "Linux",
                               reason="requires the approved Linux fixture browser endpoint")


def open_fixture(page, site):
    session, folder = page
    session.hosts = {urlsplit(site).netloc}
    session.arm("readonly")
    session.navigate(site + "/ux-clean.html")
    return session, folder


def test_measured_js_executes_once_and_keeps_object_console_numbers(page, site):
    session, _ = open_fixture(page, site)
    observed = session.probe({"js": "(() => { window.runs = (window.runs || 0) + 1; "
                              "console.warn('panel', {ratio: 0.29, minimum: 0.3}); "
                              "return {ok:false, says:'seven wall pixels, need eight', "
                              "evidence:{pixels:7, minimum:8, ratio:0.29, threshold:0.3}}; })()"})
    [check] = verdict.page_checks({"js": "fixture"}, observed)
    assert check["ok"] is False and check.get("evidence") == {
        "pixels": 7, "minimum": 8, "ratio": 0.29, "threshold": 0.3}
    assert session.evaluate("window.runs") == 1
    console = observed["probe"].get("page_console", [])
    assert len(console) == 1 and console[0]["level"] == "warn"
    assert console[0]["text"] == 'panel {"ratio":0.29,"minimum":0.3}'
    assert json.loads(console[0]["text"].removeprefix("panel ")) == {"ratio": 0.29, "minimum": 0.3}


def test_browser_keeps_long_check_values_and_errors(page, site):
    session, _ = open_fixture(page, site)
    text = 'WebGPU memory diagnostic ' * 60 + 'upscale-ann-large 3840x2160 256 MiB'
    session.evaluate("console.error(" + json.dumps(text) + "); true")
    for expression, prefix in [(json.dumps(text), ""),
                                ("(() => { throw new Error(" + json.dumps(text) + "); })()", "error: ")]:
        observed = session.probe({"js": expression})
        [check] = verdict.page_checks({"js": expression}, observed)
        assert check["detail"] == prefix + text
        if not prefix:
            assert check["value"] == text
            assert any(error.get("kind") == "console" and error.get("detail") == text
                       for error in observed["probe"]["errors"])


def test_browser_keeps_the_complete_shown_value_of_non_data_results(page, site):
    session, _ = open_fixture(page, site)
    text = 'function diagnostic ' * 60 + 'last measured number 0.29'
    expression = "(() => {const f = function() {}; f.toString = () => " + json.dumps(text) + "; return f;})()"
    observed = session.probe({"js": expression})
    [check] = verdict.page_checks({"js": expression}, observed)
    assert check["ok"] is False and check["value"] == text


def test_console_buffer_counts_omissions_and_keeps_text_until_privacy_cleaning(page, site, monkeypatch):
    session, _ = open_fixture(page, site)
    secret = "SYNTHETIC-CONSOLE-CROSSING-BOUNDARY-TOKEN"
    monkeypatch.setattr(config, "_REMEMBERED", {secret})
    text = "x" * 490 + secret
    session.evaluate("console.warn(" + json.dumps(text) + "); true")
    session.evaluate("for(let i=0;i<59;i++) console.warn('line', i); true")
    observed = session.probe({"js": "(console.error('from check'), false)"})
    probe = observed["probe"]
    assert len(probe.get("page_console", [])) == 51
    assert probe["page_console"][0]["text"] == text
    assert sum(probe.get("page_console_omitted", [])) == 10
    result = {}
    diagnostics.keep_console(result, probe)
    assert len(result["page_console"]) == 50 and result["page_console_omitted"] == 11
    assert "SYNTHETIC-" not in result["page_console"][0]["text"]
    drained = session.probe({})["probe"]
    assert drained.get("page_console") == [] and drained.get("page_console_omitted") == []


def scenario(name, site, before):
    return suite.parse({"scenarios": [{"name": name, "url": site + "/ux-clean.html", "before": before,
                                      "settle": 0, "expect": {"js": "false"}}]}).scenarios[0]


def run(session, spec, folder):
    return runner.run_scenario(session, spec, opts=runner.Options(shots=False, ux="off", load_high=None),
                               hosts=session.hosts, run_dir=folder)


def test_console_survives_navigation_inside_a_scenario_and_does_not_leak_to_the_next(page, site):
    session, folder = open_fixture(page, site)
    first = run(session, scenario("first", site, [
        {"js": "console.warn('before navigation')"}, {"navigate": site + "/ux-clean.html?next"},
        {"js": "console.warn('after navigation')"}]), folder)
    assert [line["text"] for line in first.get("page_console", [])] == ["before navigation", "after navigation"]
    session.evaluate("console.warn('late first scenario'); true")
    second = run(session, scenario("second", site, [{"js": "console.warn('second scenario')"}]), folder)
    assert [line["text"] for line in second.get("page_console", [])] == ["second scenario"]


def test_guard_console_wrapper_preserves_original_calls_and_handles_circular_arguments(page, site):
    session, _ = page
    session.call("Page.addScriptToEvaluateOnNewDocument", source="""
      window.forwarded = [];
      for (const level of ['warn','error']) {
        const original = console[level];
        console[level] = function(...args) {
          window.forwarded.push([level, this === console]); original.apply(this, args); return 17;
        };
      }
    """)
    session, _ = open_fixture(page, site)
    assert session.evaluate("const circular = {}; circular.self = circular; console.warn('circular', circular)") == 17
    observed = session.probe({"js": "(console.error('fixture error'), false)"})
    lines = observed["probe"].get("page_console", [])
    assert [row["level"] for row in lines] == ["warn", "error"]
    assert "[unserializable console argument]" in lines[0]["text"]
    assert any(error.get("kind") == "console" and error["detail"] == "fixture error"
               for error in session.probe({})["probe"]["errors"])
    assert session.evaluate("window.forwarded") == [["warn", True], ["error", True]]


def test_rendered_report_opens_complete_values_without_executing_measurement_text(page, site):
    session, folder = open_fixture(page, site)
    unsafe = '<img src=x onerror="window.injected=1">'
    check = {"check": "js 'fixture'", "says": "Measured wall pixels", "ok": False, "detail": LONG,
             "value": LONG + unsafe, "evidence_type": "js",
             "evidence": {"ratio": 0.29, "minimum": 0.3, "crop": "javascript:window.injected=2", "text": unsafe}}
    data = document(folder, check, page_console=[{"level": "warn", "text": "panel 0.29 < 0.3 " + unsafe}])
    report.write(folder, data)
    frame = session.call("Page.getFrameTree")["frameTree"]["frame"]["id"]
    session.call("Page.setDocumentContent", frameId=frame, html=(folder / "report.html").read_text())
    shown = session.evaluate("""(() => {
      const details = [...document.querySelectorAll('details')].find(d => d.querySelector('summary').textContent === 'Returned value');
      details.querySelector('summary').click();
      return {open:details.open, text:details.querySelector('pre').textContent,
              injected:window.injected || null, images:document.querySelectorAll('img[src^="javascript:"]').length,
              page:document.body.textContent};
    })()""")
    assert shown["open"] and shown["text"] == LONG + unsafe
    assert shown["injected"] is None and shown["images"] == 0
    assert "0.29" in shown["page"] and "0.3" in shown["page"] and "Page console" in shown["page"]
