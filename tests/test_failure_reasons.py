"""José, 19 October: a failed check must explain which part failed."""

import json
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest

from qajev import clients, config, report, verdict
from qajev.ledger import Ledger
from qajev.session import checked


def test_named_js_parts_pass_only_when_every_value_is_literal_true():
    assert verdict.js_result({"value": {"total": True, "photo": True}})[:2] == (True, None)


@pytest.mark.parametrize("value", [False, None, 0, 1, "", "wrong total", [True], {"nested": True}])
def test_js_object_failure_names_the_non_true_part_and_its_value(value):
    ok, detail, _ = verdict.js_result({"value": {"passing": True, "broken": value}})
    assert ok is False
    assert '"broken"=' + json.dumps(value, ensure_ascii=False) in detail
    assert "passing" not in detail


def test_every_failed_js_part_is_named_even_beyond_the_value_preview_limit():
    parts = {f"part_{i:03}": False for i in range(30)}
    parts["quantity"] = 3
    ok, detail, _ = verdict.js_result({"value": parts})
    assert ok is False
    assert all('"' + key + '"=' in detail for key in parts)
    assert '"quantity"=3' in detail


def test_empty_js_object_fails_because_it_contains_no_checks():
    ok, detail, _ = verdict.js_result({"value": {}})
    assert ok is False and "no named checks" in detail


@pytest.mark.parametrize("value,ok,reason", [
    (True, True, None), (False, False, "returned false"),
    ("the basket total is 35, expected 30", False, "the basket total is 35, expected 30"),
    ("", False, "returned an empty string"),
])
def test_boolean_and_string_js_contract_is_preserved(value, ok, reason):
    assert verdict.js_result({"value": value})[:2] == (ok, reason)


def test_named_js_values_and_registered_secrets_are_redacted_before_emission(monkeypatch):
    secret = "synthetic-private-seed-for-failure-reason"
    monkeypatch.setattr(config, "_REMEMBERED", {secret})
    [check] = verdict.page_checks({"js": "parts"}, {"js": {"value": {
        "amount": 35, "password": "synthetic-password", "session": "synthetic-session", "note": secret,
    }}})
    text = json.dumps(check)
    assert check["ok"] is False and '"amount"=35' in check["detail"]
    assert all(value not in text for value in [secret, "synthetic-password", "synthetic-session"])
    assert "redacted" in check["detail"]


def test_command_failure_uses_the_last_400_stderr_characters_not_the_prefix():
    stderr = "startup noise\n" * 80 + "missing paid orders\n"
    result = verdict.command_check({"run": "gate"}, {"exit": 1, "stderr": stderr, "stdout": "unused"})
    assert result["ok"] is False
    assert result["detail"] == "exit 1: " + stderr.strip()[-400:]


@pytest.mark.parametrize("stderr", [None, "", " \n\t"])
def test_command_failure_falls_back_to_the_last_400_stdout_characters(stderr):
    stdout = "setup noise\n" * 80 + "FAIL: paid orders did not become completed\n"
    result = verdict.command_check({"run": "gate"}, {"exit": 1, "stderr": stderr, "stdout": stdout})
    assert result["ok"] is False
    assert result["detail"] == "exit 1: " + stdout.strip()[-400:]


def test_command_failure_redacts_before_selecting_the_tail(monkeypatch):
    secret = "synthetic-private-seed-for-command-failure"
    monkeypatch.setattr(config, "_REMEMBERED", {secret})
    stdout = secret + "x" * 390
    result = verdict.command_check({"run": "gate"}, {"exit": 1, "stderr": "", "stdout": stdout})
    assert result["detail"] == "exit 1: " + stdout.replace(secret, "[redacted]")[-400:]


def test_expected_nonzero_exit_and_stdout_success_still_pass():
    assert verdict.command_check({"run": "gate", "exit": 1}, {"exit": 1, "stderr": "expected"})["ok"]
    assert verdict.command_check({"run": "gate", "stdout": "ready"}, {"exit": 0, "stdout": "ready"})["ok"]


@pytest.mark.parametrize("value,ok,fragment", [
    ({"total": True, "photo": True}, True, None),
    ({"total": 35, "photo": False}, False, '"total"=35'),
    ("player 2 did not receive the update", False, "player 2 did not receive the update"),
])
def test_across_clients_judge_uses_the_same_named_failure_reasons(monkeypatch, value, ok, fragment):
    monkeypatch.setattr(clients, "_evaluate", lambda *_: {"result": {"value": {"value": value}}})
    [check] = clients.judge(None, [{"check": "room state", "js": "state", "says": "Every player updated."}], [], {})
    assert check["ok"] is ok and check["says"] == "Every player updated."
    assert check["detail"] is None if fragment is None else fragment in check["detail"]


def test_failed_js_parts_and_command_stdout_reach_all_report_formats(tmp_path):
    checks = verdict.page_checks({"js": "parts", "says": {"js": "The basket is ready."}},
                                 {"js": {"value": {"total": 35, "photo": False}}})
    checks.append(verdict.command_check({"run": "gate"}, {"exit": 1, "stdout": "paid orders missing", "stderr": ""}))
    outcome, reason = verdict.classify("checked", checks, has_checks=True)
    result = {"name": "basket", "about": "The basket is ready.", "url": "http://127.0.0.1/", "mode": "readonly",
              "goal": None, "outcome": outcome, "reason": reason, "checks": checks, "seconds": 0.1,
              "findings": [], "shot": None}
    data = report.build(SimpleNamespace(name="failure reasons"), [result], [Ledger(0.0).summary()], browser={},
                        started_at=time.time(), strict=False, interrupted=False, run_dir=tmp_path)
    report.write(tmp_path, data)
    assert data["gate"] == "FAIL"
    for name in ["report.json", "report.md", "report.html"]:
        text = (tmp_path / name).read_text()
        assert all(value in text for value in ["total", "35", "photo", "false", "paid orders missing"]), name


@pytest.mark.skipif(not shutil.which("node"), reason="needs Node for the page-side evaluator")
def test_accepted_object_does_not_run_the_optional_failure_explanation():
    explanation = '(() => { throw new Error("must not run") })()'
    source = "(" + checked("({total: true, photo: true})", explanation) + ")"
    completed = subprocess.run(["node", "-e", source + ".then(r => console.log(JSON.stringify(r)))"],
                               capture_output=True, text=True, timeout=10, check=True)
    assert json.loads(completed.stdout) == {"value": {"total": True, "photo": True}}


@pytest.mark.skipif(not shutil.which("node"), reason="needs Node for the page-side evaluator")
@pytest.mark.parametrize("expression,reason", [
    ("({passing: true, broken: undefined})", "undefined"),
    ("({passing: true, broken: () => true})", "function"),
    ("({passing: true, broken: 1n})", "bigint"),
    ("({passing: true, broken: {toJSON: () => true}})", "serialized as true"),
    ("({passing: true, get broken() { throw new Error('must not read'); }})", "accessor not read"),
])
def test_page_transport_cannot_drop_or_pass_a_non_true_named_part(expression, reason):
    source = "(" + checked(expression) + ").then(r => console.log(JSON.stringify(r)))"
    completed = subprocess.run(["node", "-e", source], capture_output=True, text=True, timeout=10, check=True)
    result = json.loads(completed.stdout)
    ok, detail, _ = verdict.js_result(result)
    assert ok is False and '"broken"=' in detail and reason in detail


def test_authentication_boolean_is_visible_as_a_failed_part():
    ok, detail, _ = verdict.js_result({"value": {"authenticated": False, "page_ready": True}})
    assert ok is False and detail == 'parts not true: "authenticated"=false'
