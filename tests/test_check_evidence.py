"""Failed checks retain the measurements and full diagnostics needed to explain them."""

import json
import time
from html import escape
from types import SimpleNamespace

import pytest

from qajev import config, diagnostics, report, runner, suite, verdict
from qajev.report_files import image_paths

LONG = 'CreateTexture: ' + 'diagnostic ' * 80 + 'upscale-ann-large: 3840x2160, requested 256 MiB'


@pytest.mark.parametrize("result,reason", [({"value": LONG}, LONG), ({"error": LONG}, "error: " + LONG)])
def test_failed_js_keeps_the_complete_reason_and_value(result, reason):
    [check] = verdict.page_checks({"js": "fixture"}, {"js": result})
    assert check["ok"] is False and check["detail"] == reason
    if "value" in result:
        assert check["value"] == LONG


def test_unexpected_object_keeps_its_full_value_without_becoming_truthy():
    value = {"text": LONG, "numbers": list(range(100))}
    [check] = verdict.page_checks({"js": "fixture"}, {"js": {"value": value}})
    assert check["ok"] is False
    assert check["value"] == json.dumps(value)
    assert json.loads(check["value"]) == value


@pytest.mark.parametrize("ok", [False, True])
def test_explicit_js_result_keeps_measured_evidence_and_the_authors_plan_words(ok):
    value = {"ok": ok, "says": "panel difference 0.29 missed 0.3", "evidence": {
        "wall_pixels": 7, "minimum_pixels": 8, "ratio": 0.29, "minimum_ratio": 0.3, "details": LONG}}
    [check] = verdict.page_checks({"js": "fixture", "says": {"js": "At least eight wall pixels"}},
                                 {"js": {"value": value}})
    assert check["ok"] is ok
    assert check["detail"] == (None if ok else value["says"])
    assert check.get("evidence") == value["evidence"] and check.get("evidence_type") == "js"
    assert check["value"] == json.dumps(value)
    assert json.loads(check["value"]) == value
    assert check["says"] == "At least eight wall pixels"


@pytest.mark.parametrize("value", [
    {"ok": True}, {"ok": "true", "says": "why", "evidence": {}},
    {"ok": 1, "says": "why", "evidence": {}}, {"ok": True, "says": "why", "evidence": []},
    {"ok": True, "says": None, "evidence": {}},
])
def test_malformed_structured_js_results_still_fail(value):
    [check] = verdict.page_checks({"js": "fixture"}, {"js": {"value": value}})
    assert check["ok"] is False and "evidence" not in check


def test_error_envelope_wins_over_an_apparent_structured_success():
    raw = {"error": LONG, "value": {"ok": True, "says": "passed", "evidence": {"pixels": 8}}}
    [check] = verdict.page_checks({"js": "fixture"}, {"js": raw})
    assert check["ok"] is False and check["detail"] == "error: " + LONG
    assert "evidence" not in check


def test_console_error_findings_keep_the_complete_diagnostic():
    found = verdict.findings_from_probe({"errors": [{"kind": "console", "detail": LONG}]},
                                        scenario="fixture", url="http://127.0.0.1/", first_party_hosts={"127.0.0.1"})
    assert len(found) == 1 and found[0]["detail"] == LONG


@pytest.mark.parametrize("length", [65535, 65536, 65537, 2_000_000])
def test_js_reason_and_value_have_a_visible_per_item_boundary(length):
    text = "x" * length
    expected = text if length <= 65536 else "x" * 65536 + f"\n[truncated {length - 65536} characters]"
    ok, detail, value = verdict.js_result({"value": text})
    assert ok is False and bool(detail == expected) and bool(value == expected)


@pytest.mark.parametrize("kind", ["console", "exception", "rejection", "worker"])
def test_page_error_findings_bound_each_oversized_item(kind):
    text = "x" * 2_000_000
    [finding] = verdict.findings_from_probe({"errors": [{"kind": kind, "detail": text}]},
                                            scenario="fixture", url="http://127.0.0.1/",
                                            first_party_hosts={"127.0.0.1"})
    assert bool(finding["detail"] == "x" * 65536 + "\n[truncated 1934464 characters]")


@pytest.mark.parametrize("wire", ["error", "shown", "object", "measured"])
def test_all_js_diagnostic_wires_bound_oversized_text(wire):
    text = "x" * 2_000_000
    raw = {"error": text} if wire == "error" else {"type": "function", "shown": text} if wire == "shown" else {
        "value": {"message": text} if wire == "object" else {"ok": False, "says": text, "evidence": {"ratio": 0.29}}}
    [check] = verdict.page_checks({"js": "fixture"}, {"js": raw})
    assert check["ok"] is False
    outputs = ([check["detail"]] if wire == "error" else [check["value"]] if wire in {"shown", "object"}
               else [check["detail"], check["value"]])
    assert all(len(item.encode()) < 65600 and "[truncated " in item for item in outputs)
    if wire == "measured":
        assert check["evidence"] == {"ratio": 0.29}


@pytest.mark.parametrize("text,expected", [
    ("🧪" * 16384, "🧪" * 16384),
    ("x" * 65535 + "🧪", "x" * 65535 + "\n[truncated 1 characters]"),
])
def test_item_bound_counts_utf8_bytes_without_splitting_a_character(text, expected):
    _, detail, value = verdict.js_result({"value": text})
    assert bool(detail == expected) and bool(value == expected)


def test_item_bound_cleans_the_whole_secret_and_path_before_cutting(monkeypatch):
    secret = "SYNTHETIC-BOUNDARY-SECRET-ABCDEFGHIJK"
    monkeypatch.setattr(config, "_REMEMBERED", {secret})
    text = "x" * 65530 + secret + " /Users/DiagnosticPerson/repo/check.js " + "z" * 500
    _, detail, value = verdict.js_result({"value": text})
    assert isinstance(value, str)
    assert detail == value and len(value.encode()) < 65600 and "[truncated " in value
    assert value.startswith("x" * 65530 + "[redac")
    assert "SYNTHETIC" not in value and "DiagnosticPerson" not in value


@pytest.mark.parametrize("length,kept", [(65527, True), (65528, False), (2_000_000, False)])
def test_an_object_value_over_one_item_stays_valid_json(length, kept):
    # 31 Oct: every calib5e click run's value was cut as text, with "[truncated N characters]" after it, so it did not
    # parse. {"m": "x" * 65527} is exactly 65536 characters of JSON.
    obj = {"m": "x" * length}
    ok, detail, value = verdict.js_result({"value": obj})
    assert ok is False and detail == "check must return true, got object"
    if kept:
        assert json.loads(value) == obj
    else:
        assert json.loads(value) == {"_qajev_truncated": f"[truncated {length + 9} characters]"}


@pytest.mark.parametrize("ok", [True, False])
def test_an_oversized_measured_value_stays_valid_json_and_keeps_its_says(ok):
    # A passing check has its says only in its value: big evidence must not take the says with it.
    says = "the 5 clicks each moved the camera by under 2 px"
    measured = {"ok": ok, "says": says, "evidence": {"frames": ["x" * 100] * 2000}}
    [check] = verdict.page_checks({"js": "fixture"}, {"js": {"value": measured}})
    value = json.loads(check["value"])
    assert set(value) == {"ok", "says", "_qajev_truncated"}
    assert value["ok"] is ok and value["says"] == says and value["_qajev_truncated"].startswith("[truncated ")
    assert len(check["value"].encode()) <= 65536
    assert check["evidence"]["_qajev_truncated"].startswith("[truncated ")


@pytest.mark.parametrize("says", ["x" * 2_000_000, "🧪" * 30_000, 'a "quote" and a \\ ' * 8000])
def test_a_measured_value_with_a_huge_says_keeps_its_start_in_one_item(says):
    _, _, text = verdict.js_result({"value": {"ok": True, "says": says, "evidence": {}}})
    value = json.loads(text)
    assert len(text.encode()) <= 65536 and len(value["says"]) > 4000 and says.startswith(value["says"])
    assert value["ok"] is True and value["_qajev_truncated"].startswith("[truncated ")


@pytest.mark.parametrize("length,kept", [(65520, True), (65521, True), (65522, False), (2_000_000, False)])
def test_structured_evidence_has_its_own_item_boundary(length, kept):
    evidence = {"message": "x" * length}
    [check] = verdict.page_checks({"js": "fixture"}, {"js": {"value": {
        "ok": False, "says": "measured", "evidence": evidence}}})
    assert check["detail"] == "measured" and check["evidence_type"] == "js"
    if kept:
        assert bool(check["evidence"] == evidence)
    else:
        assert bool(check["evidence"] == {"_qajev_truncated": f"[truncated {length + 15} characters]"})


def test_oversized_diagnostics_write_visible_markers_to_all_reports(tmp_path):
    [check] = verdict.page_checks({"js": "fixture"}, {"js": {"value": "x" * 2_000_000}})
    report.write(tmp_path, document(tmp_path, check))
    for name in ("report.json", "report.html", "report.md"):
        text = (tmp_path / name).read_text()
        assert "[truncated 1934464 characters]" in text and "x" * 65537 not in text


def document(folder, check, **extra):
    scenario = {"name": "measured", "about": "measured values explain the failure", "outcome": "fail",
                "reason": "one check failed", "seconds": 1, "checks": [check], "findings": [], "screens": [], **extra}
    return report.build(SimpleNamespace(name="fixture"), [scenario], [], browser={}, started_at=time.time(),
                        strict=False, interrupted=False, run_dir=folder)


def test_json_and_html_keep_full_failed_values_and_escape_measurements(tmp_path):
    dangerous = '<img src=x onerror="alert(1)">'
    evidence = {"numbers": [7, 8, 0.29, 0.3], "text": LONG + dangerous,
                "crop": "javascript:alert(2)", "reference": "shots/not-an-image.png"}
    check = {"check": "js 'fixture'", "says": "Measured pixels", "ok": False, "detail": LONG,
             "value": LONG + dangerous, "evidence": evidence, "evidence_type": "js"}
    data = document(tmp_path, check)
    assert image_paths(data) == [], "JS measurement keys must never become report artifacts"
    report.write(tmp_path, data)
    saved = json.loads((tmp_path / "report.json").read_text())
    assert saved["scenarios"][0]["checks"][0] == check
    assert "report_incomplete" not in saved
    html = (tmp_path / "report.html").read_text()
    assert LONG in html and escape(LONG + dangerous) in html
    assert "0.29" in html and "0.3" in html
    assert dangerous not in html and 'src="javascript:' not in html
    assert escape(json.dumps(evidence, ensure_ascii=False, sort_keys=True)) in html
    md = (tmp_path / "report.md").read_text()
    assert "    " + LONG + dangerous in md
    assert "](javascript:" not in md and "](shots/not-an-image.png)" not in md


class Page:
    def __init__(self, events):
        self.events = events

    def set_device(self, *_):
        pass

    def arm(self, *_):
        pass

    def check_host(self, *_):
        pass

    def navigate(self, *_):
        pass

    def probe(self, *_):
        return {"url": "http://127.0.0.1/", "js": {"value": False}, "probe": self.events}


def run_page(monkeypatch, events):
    monkeypatch.setattr(runner, "wait_for_quiet", lambda _: (True, 1))
    page = Page(events)
    scenario = suite.parse({"scenarios": [{"name": "one", "url": "http://127.0.0.1/", "settle": 0,
                                          "expect": {"js": "false"}}]}).scenarios[0]
    return runner.run_scenario(page, scenario, opts=runner.Options(shots=False, ux="off"),
                               hosts={"127.0.0.1"}, run_dir=None)


def test_scenario_keeps_bounded_console_after_cleaning_complete_text(monkeypatch):
    secret = "SYNTHETIC-SCENARIO-TOKEN-AABBCCDDEEFF"
    monkeypatch.setattr(config, "_REMEMBERED", {secret})
    lines = [{"level": "warn", "text": "x" * 490 + secret + " /Users/HiddenName/repo/file.py"}]
    lines += [{"level": "warn", "text": "/Users/ConsolePerson/repo/file.py " + secret}]
    lines += [{"level": "error", "text": f"measured difference {i}: 0.29 < 0.3"} for i in range(55)]
    result = run_page(monkeypatch, {"page_console": lines, "page_console_omitted": [3]})
    assert len(result.get("page_console", [])) == 50
    first = result["page_console"][0]
    assert len(first["text"]) == 500 and first["truncated"] is True
    assert "SYNTHETIC-" not in json.dumps(result) and "HiddenName" not in json.dumps(result)
    assert "ConsolePerson" not in json.dumps(result)
    assert result["page_console_omitted"] == 10


def test_console_limit_applies_across_multiple_reads():
    result = {}
    diagnostics.keep_console(result, {"page_console": [{"level": "warn", "text": f"first {i}"} for i in range(30)]})
    diagnostics.keep_console(result, {"page_console": [{"level": "error", "text": f"second {i}"} for i in range(30)],
                                      "page_console_omitted": [3]})
    assert len(result["page_console"]) == 50 and result["page_console_omitted"] == 13
    assert result["page_console"][0]["text"] == "first 0" and result["page_console"][-1]["text"] == "second 19"


@pytest.mark.parametrize("length", [499, 500, 501])
def test_console_character_limit_marks_only_lines_that_were_shortened(length):
    result = {}
    diagnostics.keep_console(result, {"page_console": [{"level": "warn", "text": "x" * length}]})
    [line] = result["page_console"]
    assert len(line["text"]) == min(length, 500)
    assert bool(line.get("truncated")) is (length > 500)


def test_a_new_scenario_clears_carried_console_but_preserves_blocked_writes():
    from qajev.session import Session

    session = Session.__new__(Session)
    session.carried = {"page_console": [{"level": "warn", "text": "old"}], "page_console_omitted": [3],
                       "blocked": [{"method": "POST", "url": "/write"}]}
    session._document = lambda: "loader"
    session.begin_test()
    assert "page_console" not in session.carried and "page_console_omitted" not in session.carried
    assert session.carried["blocked"] == [{"method": "POST", "url": "/write"}]


def test_non_data_js_shown_values_are_not_shortened():
    ok, _, value = verdict.js_result({"type": "function", "shown": LONG})
    assert ok is False and value == LONG


def test_invalid_console_metadata_is_ignored_without_growing_reports(monkeypatch):
    result = run_page(monkeypatch, {"page_console": [None, {"level": "debug", "text": "ignore"},
                                                    {"level": "warn", "text": 42}],
                                     "page_console_omitted": [-1, True, "4", 0]})
    assert not result.get("page_console") and not result.get("page_console_omitted")


def test_structured_diagnostics_clean_secrets_and_paths_without_losing_numbers(monkeypatch):
    secret = "SYNTHETIC-MEASUREMENT-PRIVATE-TOKEN"
    monkeypatch.setattr(config, "_REMEMBERED", {secret})
    value = {"ok": False, "says": f"{secret} /Users/MeasuredPerson/repo/check.js", "evidence": {
        "ratio": 0.29, "minimum": 0.3, "message": LONG + secret + " /Users/MeasuredPerson/repo/check.js"}}
    [check] = verdict.page_checks({"js": "fixture"}, {"js": {"value": value}})
    text = json.dumps(check)
    assert "MeasuredPerson" not in text and secret not in text
    assert "[redacted]" in text and "3840x2160" in text
    assert check.get("evidence", {}).get("ratio") == 0.29
    assert check.get("evidence", {}).get("minimum") == 0.3


def test_diagnostic_cleaning_preserves_json_numbers_and_cleans_nested_keys(monkeypatch):
    secret = "SYNTHETIC-NESTED-SECRET-KEY"
    monkeypatch.setattr(config, "_REMEMBERED", {secret, "12345678"})
    source = {"number": 12345678, "flag": False, "none": None, secret: "12345678",
              "nested": [{secret: secret, "ratio": 0.29}]}
    cleaned = diagnostics.clean_data(source)
    assert cleaned["number"] == 12345678 and cleaned["flag"] is False and cleaned["none"] is None
    assert cleaned["[redacted]"] == "[redacted]"
    assert cleaned["nested"] == [{"[redacted]": "[redacted]", "ratio": 0.29}]
    assert secret not in json.dumps(cleaned)


def test_diagnostic_cleaning_refuses_colliding_redacted_keys(monkeypatch):
    monkeypatch.setattr(config, "_REMEMBERED", {"SYNTHETIC-FIRST-KEY", "SYNTHETIC-SECOND-KEY"})
    with pytest.raises(ValueError, match="collide"):
        diagnostics.clean_data({"SYNTHETIC-FIRST-KEY": 7, "SYNTHETIC-SECOND-KEY": 8})


def test_console_is_visible_and_escaped_in_failed_and_harness_reports(tmp_path):
    text = 'panel diff 0.29 < 0.3 <script>alert(3)</script>'
    check = {"check": "js 'fixture'", "ok": False, "detail": "returned false"}
    for outcome in ("fail", "harness"):
        data = document(tmp_path, check, outcome=outcome, page_console=[{"level": "warn", "text": text}],
                        page_console_omitted=2)
        report.write(tmp_path, data)
        html = (tmp_path / "report.html").read_text()
        assert "Page console" in html and escape(text) in html and text not in html
        assert "2 console lines omitted" in html
        assert "    warn: " + text in (tmp_path / "report.md").read_text()
        assert "2 console lines omitted" in (tmp_path / "report.md").read_text()
