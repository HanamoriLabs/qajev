"""Failure observations are explicit, bounded previews; they never change the JS verdict."""

import asyncio
import json
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest

from qajev import config, verdict
from qajev.suite import SuiteError, parse


def test_failed_js_shows_declared_actual_and_expected_values():
    [check] = verdict.page_checks(
        {"js": "total === 20", "explain": "({actual: total, expected: 20})"},
        {"js": {"value": False, "diagnostic": {"actual": 17, "expected": 20}}},
    )
    assert check["ok"] is False and check["value"] == "false"
    assert '"actual": 17' in check["detail"] and '"expected": 20' in check["detail"]


def test_diagnostic_values_are_redacted_before_they_enter_a_check(monkeypatch):
    monkeypatch.setattr(config, "_REMEMBERED", {"synthetic-hidden-value"})
    [check] = verdict.page_checks(
        {"js": "false", "explain": "state"},
        {"js": {"value": False, "diagnostic": {
            "actual": "synthetic-hidden-value", "password": "not-a-real-password", "count": 3,
        }}},
    )
    shown = json.dumps(check)
    assert "synthetic-hidden-value" not in shown and "not-a-real-password" not in shown
    assert "redacted" in shown and '"count": 3' in check["detail"]


def test_diagnostic_cannot_turn_false_into_a_pass_and_large_data_stays_a_preview():
    [check] = verdict.page_checks(
        {"js": "false", "explain": "state"},
        {"js": {"value": False, "diagnostic": {"ok": True, "blob": "x" * 100_000}}},
    )
    assert check["ok"] is False and check["value"] == "false"
    assert len(check["detail"]) <= 700
    assert len(json.dumps(check)) < 1500


def test_suite_accepts_an_explicit_failure_explanation():
    scenario = parse({"scenarios": [{
        "name": "total", "url": "http://127.0.0.1:8765/", "about": "The cart adds its prices.",
        "expect": {"js": "total === 20", "explain": "({actual: total, expected: 20})", "says": "total is 20"},
    }]}).scenarios[0]
    assert scenario.expect["explain"] == "({actual: total, expected: 20})"


@pytest.mark.parametrize("expect", [
    {"explain": "state"}, {"js": "false", "explain": 4}, {"js": "false", "explain": " "},
])
def test_explanation_needs_a_js_check_and_a_nonempty_expression(expect):
    with pytest.raises(SuiteError, match="explain"):
        parse({"scenarios": [{"name": "bad", "url": "http://127.0.0.1/", "expect": expect}]})


def settle(expression, explain):
    from qajev.session import checked

    if not shutil.which("node"):
        pytest.skip("needs Node for the page-side evaluator")
    source = f"({checked(expression, explain, timeout_ms=100)}).then(r => console.log(JSON.stringify(r)))"
    result = subprocess.run(["node", "-e", source], capture_output=True, text=True, timeout=10, check=True)
    return json.loads(result.stdout)


def test_the_assertion_runs_once_and_its_explanation_runs_only_after_failure():
    result = settle("(globalThis.checkCalls = (globalThis.checkCalls || 0) + 1, false)",
                    "({actual: globalThis.checkCalls, expected: 2})")
    assert result == {"value": False, "diagnostic": {"actual": 1, "expected": 2}}
    assert settle("true", "(() => { throw new Error('must not execute') })()") == {"value": True}


def test_a_broken_diagnostic_does_not_replace_the_original_false():
    result = settle("false", "(() => { throw new Error('synthetic-private-message') })()")
    assert result["value"] is False and result["diagnostic"] == {"error": "explanation failed"}
    assert "synthetic-private-message" not in json.dumps(result)


def test_page_side_diagnostics_bound_data_and_never_invoke_accessors():
    result = settle("false", "({long: 'x'.repeat(100000), get value() { throw new Error('not read'); }})")
    assert result["value"] is False
    assert result["diagnostic"]["long"] == "[string too long]"
    assert result["diagnostic"]["value"] == "[accessor not read]"
    assert len(json.dumps(result)) < 1500


def test_common_page_global_names_are_not_shadowed_by_diagnostic_locals():
    result = settle("(globalThis.result = false, globalThis.extra = {actual: 7}, result)", "extra")
    assert result == {"value": False, "diagnostic": {"actual": 7}}


def test_cli_preserves_the_assertion_and_explicit_explanation():
    from qajev.cli import build_parser, check_suite

    args = build_parser().parse_args([
        "check", "http://127.0.0.1/", "--expect-js", "total === 20",
        "--explain-js", "({actual: total, expected: 20})", "--says", "The total is 20.",
    ])
    scenario = check_suite(args).scenarios[0]
    assert scenario.expect["js"] == "total === 20"
    assert scenario.expect["explain"] == "({actual: total, expected: 20})"


def test_mcp_passes_failure_diagnostics_without_changing_the_check(monkeypatch):
    from qajev import mcp_server

    seen = []

    async def fake_report(args, ctx, background, title=None):
        seen.extend(args)
        return {"gate": "FAIL", "scenarios": [], "run_dir": "/synthetic/run"}

    monkeypatch.setattr(mcp_server, "_run_report", fake_report)
    asyncio.run(mcp_server.qa_check("http://127.0.0.1/", None, expect_js="total === 20",
                                   explain_js="({actual: total, expected: 20})", runner="local"))
    assert seen[seen.index("--expect-js") + 1] == "total === 20"
    assert seen[seen.index("--explain-js") + 1] == "({actual: total, expected: 20})"


def test_an_assertion_error_does_not_run_failure_diagnostics():
    result = settle("(() => { throw new Error('assertion failed to run') })()", "({unexpected: true})")
    assert result == {"error": "assertion failed to run"}


def test_failure_values_reach_json_markdown_and_html_reports_without_secrets(tmp_path, monkeypatch):
    from qajev import report
    from qajev.ledger import Ledger

    monkeypatch.setattr(config, "_REMEMBERED", {"synthetic-report-secret"})
    checks = verdict.page_checks(
        {"js": "total === 20", "explain": "state", "says": {"js": "The cart total is 20."}},
        {"js": {"value": False, "diagnostic": {
            "actual": 17, "expected": 20, "token": "not-a-real-token", "probe": "synthetic-report-secret",
        }}},
    )
    result = {"name": "total", "about": "The cart adds its prices.", "url": "http://127.0.0.1/",
              "mode": "readonly", "goal": None, "outcome": "fail", "reason": checks[0]["detail"],
              "checks": checks, "seconds": 0.1, "findings": [], "shot": None}
    data = report.build(SimpleNamespace(name="failure values"), [result], [Ledger(0.0).summary()], browser={},
                        started_at=time.time(), strict=False, interrupted=False, run_dir=tmp_path)
    report.write(tmp_path, data)
    assert data["gate"] == "FAIL" and data["counts"]["fail"] == 1
    for name in ("report.json", "report.md", "report.html"):
        text = (tmp_path / name).read_text()
        assert all(word in text for word in ("actual", "expected", "17", "20")), name
        assert "synthetic-report-secret" not in text and "not-a-real-token" not in text, name
