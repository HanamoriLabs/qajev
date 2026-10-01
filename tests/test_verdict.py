import pytest

from qajev import verdict as V

EXPECT = {"url": "/pricing", "text": ["Pro", "Team"], "absent": ["Error"], "js": "true"}


def observed(url="http://h/pricing", text=(True, True), absent=(False,), js=True):
    return {"url": url, "text": list(text), "absent": list(absent), "js": js, "says": "page text"}


def test_page_checks_pass_when_everything_holds():
    checks = V.page_checks(EXPECT, observed())
    assert len(checks) == 5 and V.all_ok(checks)


def test_page_checks_name_what_failed():
    checks = V.page_checks(EXPECT, observed(url="http://h/", text=(True, False), absent=(True,), js="error: x"))
    failed = [c["check"] for c in checks if not c["ok"]]
    assert failed == ["url contains '/pricing'", "page shows 'Team'", "page lacks 'Error'", "js 'true'"]


def test_js_must_be_exactly_true():
    assert not V.all_ok(V.page_checks({"js": "x"}, {"js": "error: boom"}))
    assert not V.all_ok(V.page_checks({"js": "x"}, {"js": False}))


def test_url_regex():
    assert V.all_ok(V.page_checks({"url_regex": r"/items/\d+$"}, {"url": "http://h/items/42"}))
    assert not V.all_ok(V.page_checks({"url_regex": r"/items/\d+$"}, {"url": "http://h/items/new"}))


OK = [{"check": "a", "ok": True}]
BAD = [{"check": "page shows 'Pro'", "ok": False, "detail": None}]


@pytest.mark.parametrize("stop, checks, has, outcome", [
    ("done", OK, True, "pass"),
    ("blocked", OK, True, "pass"),              # the page decides, not Jev's BLOCKED
    ("budget_actions", OK, True, "pass"),       # ...nor a harness stop
    ("done", BAD, True, "fail"),                # Jev thought it was done; the product disagrees
    ("reached", BAD, True, "fail"),             # page fine but a side effect is wrong
    ("checked", BAD, True, "fail"),
    ("blocked", BAD, True, "stuck"),
    ("budget_actions", BAD, True, "harness"),
    ("stale", BAD, True, "harness"),
    ("model_error", BAD, True, "harness"),
    ("guard_missing", BAD, True, "harness"),
    ("done", [], False, "unverified"),
    ("cost_cap", [], False, "harness"),
    ("unreachable", [], True, "fail"),
    ("skipped", [], True, "skipped"),
])
def test_classification(stop, checks, has, outcome):
    assert V.classify(stop, checks, has_checks=has)[0] == outcome


def test_fail_reason_quotes_the_failed_check():
    outcome, reason = V.classify("done", BAD, has_checks=True)
    assert reason == "Jev reported DONE but: page shows 'Pro'"


@pytest.mark.parametrize("outcomes, strict, gate", [
    (["pass", "pass"], False, "PASS"),
    (["pass", "fail"], False, "FAIL"),
    (["pass", "stuck"], False, "INCOMPLETE"),
    (["pass", "stuck"], True, "FAIL"),
    (["pass", "harness"], False, "INCOMPLETE"),
    (["unverified"], False, "INCOMPLETE"),
    ([], False, "INCOMPLETE"),
])
def test_gate(outcomes, strict, gate):
    assert V.gate(outcomes, strict=strict) == gate


def test_fetch_and_command_checks():
    assert V.fetch_check({"url": "/api", "status": 200}, {"status": 200})["ok"]
    assert not V.fetch_check({"url": "/api", "status": 200}, {"status": 500})["ok"]
    assert not V.fetch_check({"url": "/api", "contains": "ok"}, {"status": 200, "body": "nope"})["ok"]
    assert V.command_check({"run": "x", "stdout": "1"}, {"exit": 0, "stdout": "1\n"})["ok"]
    assert not V.command_check({"run": "x"}, {"exit": 2, "stderr": "boom"})["ok"]


def test_findings_skip_third_party_noise_and_our_own_write_blocks():
    probe = {"errors": [
        {"kind": "exception", "detail": "TypeError: x is undefined"},
        {"kind": "rejection", "detail": "TypeError: QAJev read-only: blocked POST"},
        {"kind": "http", "status": 500, "detail": "http://h/api/data"},
        {"kind": "http", "status": 404, "detail": "https://cdn.other.com/pixel.gif"},
        {"kind": "resource", "detail": "http://h/missing.png"},
        {"kind": "console", "detail": "warning-ish"},
    ], "blank_ms": 12000}
    found = V.findings_from_probe(probe, scenario="s", url="http://h/", first_party_hosts={"h"})
    kinds = [(f["severity"], f["kind"]) for f in found]
    assert kinds == [("S2", "page error"), ("S2", "HTTP 500"), ("S3", "failed to load"), ("S3", "console error")]


def test_a_blank_spell_is_one_finding_with_its_longest_duration():
    assert V.blank_finding(9_999, scenario="s", url="u") == []
    (f,) = V.blank_finding(15_037, scenario="s", url="u")
    assert (f["severity"], f["kind"], f["detail"]) == ("S2", "blank or spinner over 10 s", "longest 15.0 s")


def test_visible_checks_are_named_and_explain_a_miss():
    ok, miss = V.page_checks({"visible": ["€17.50", "FAQ"]}, {"visible": [True, False]})
    assert ok == {"check": "on screen: '€17.50'", "ok": True, "detail": None}
    assert not miss["ok"] and "not visible in the viewport" in miss["detail"]


def test_screens_report_the_runner_up_label_from_the_request():
    decision = {
        "operation": "CLICK", "target": "2", "confidence": 0.9,
        "target_probabilities": {"1": 0.3, "2": 0.6, "3": 0.1},
        "request": {"questions": {"click_target": {"criteria": {
            "1": {"element": "[1] Overview"}, "2": {"element": "[2] Pricing"}, "3": {"element": "[3] Docs"}}}}},
    }
    done = {"operation": "DONE", "target": None, "operation_probabilities": {"DONE": 0.95, "WAIT": 0.05}}
    s1, s2 = V.screens([decision, done])
    assert (s1["next_step"], s1["p"], s1["runner_up"], s1["runner_up_p"]) == ("[2] Pricing", 0.6, "[1] Overview", 0.3)
    assert (s2["next_step"], s2["p"], s2["runner_up"]) == ("DONE", 0.95, "WAIT")


def test_ignore_case_is_named_in_the_check():
    (check,) = V.page_checks({"text": ["Solo"], "ignore_case": True}, {"text": [True]})
    assert check == {"check": "page shows 'Solo' (any case)", "ok": True, "detail": None}
