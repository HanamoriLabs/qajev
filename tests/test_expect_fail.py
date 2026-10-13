"""Planted faults (expect_fail; the Orchestrator, 13 Oct): a test meant to fail on one named check. It is met only when
it fails there and every other check passes; a pass, or a failure anywhere else, turns the gate FAIL; a crash or a
time-out is never a catch. Its own outcome is kept, so nothing hides under the tag."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
import yaml

from qajev import cli, mcp_server, report, suite, verdict

URL = "http://127.0.0.1:8765/"
PRICE = "the price is 10"
LEDGER = {"usd": 0.0, "usd_typesafe_estimated": 0.0, "usd_text": 0.0, "calls": {"typesafe": 0, "text": 0},
          "tokens": {"typesafe": 0, "text": 0}, "errors": 0, "text_cost_reported": True, "cap_usd": 1.0}


def planted_suite(expect_fail=PRICE, **price):
    return suite.parse({"name": "planted", "device": "desktop", "scenarios": [
        {"name": "plain", "url": URL, "about": "the shop opens", "expect": {"text": ["Shop"]}},
        {"name": "price", "url": URL, "about": "plants a wrong price: the price check must catch it",
         "expect": {"text": ["Shop"], "js": "price() === 10", "says": {"js": PRICE}}, "expect_fail": expect_fail,
         **price},
    ]})


def ran(name, expect, *, shows=True, js=True, outcome=None, stop="checked"):
    """A result as the runner leaves it, its checks named and judged by verdict.page_checks."""
    observed = {"url": URL, "text": [shows] * len(expect.get("text", [])), "says": "Shop",
                "js": {"value": True} if js is True else js}
    checks = verdict.page_checks(expect, observed)
    failed = [c["check"] for c in checks if not c["ok"]]
    return {"name": name, "outcome": outcome or ("fail" if failed else "pass"), "stop": stop, "checks": checks,
            "reason": f"failed: {', '.join(failed)}" if failed else "every expectation held", "findings": [],
            "screens": []}


def build(tmp_path, s, results):
    return report.build(s, results, [LEDGER], browser={}, started_at=time.time(), strict=False, interrupted=False,
                        run_dir=tmp_path)


def run(tmp_path, s=None, **price):
    s = s or planted_suite()
    plain, planted = s.scenarios
    return build(tmp_path, s, [ran("plain", plain.expect), ran("price", planted.expect, **price)])


def price_of(data):
    return next(r for r in data["scenarios"] if r["name"] == "price")


def test_a_planted_fault_that_fails_on_its_check_is_met_and_the_gate_passes(tmp_path):
    data = run(tmp_path, js="the price is 12")
    r = price_of(data)
    assert data["gate"] == "PASS" and data["exit_code"] == 0
    assert r["outcome"] == "fail" and data["counts"]["fail"] == 1  # its own outcome is kept
    assert r["expected"] == "fail" and r["expected_check"] == PRICE and r["graded"] == "pass"
    assert r["graded_reason"].startswith("failed as expected on the planted check: ")
    assert data["expected_fail"] == 1


def test_a_planted_fault_that_passes_turns_the_gate_fail(tmp_path):
    data = run(tmp_path)
    r = price_of(data)
    assert data["gate"] == "FAIL" and data["exit_code"] == 1
    assert r["outcome"] == "pass" and r["graded"] == "fail" and r["graded_reason"] == verdict.PLANTED_MISSED


def test_a_planted_fault_that_fails_on_another_check_turns_the_gate_fail(tmp_path):
    data = run(tmp_path, shows=False)  # the price check passed; the shop's name is gone
    r = price_of(data)
    assert data["gate"] == "FAIL"
    assert r["graded"] == "fail" and r["graded_reason"].startswith("failed, but not on the planted check: ")


def test_failing_on_the_planted_check_and_another_one_is_no_catch(tmp_path):
    data = run(tmp_path, shows=False, js="the price is 12")
    assert data["gate"] == "FAIL" and price_of(data)["graded_reason"].startswith("failed, but not on the planted")


def test_a_page_that_did_not_load_is_no_catch(tmp_path):
    s = planted_suite()
    down = {"name": "price", "outcome": "fail", "stop": "checked", "checks": [], "findings": [], "screens": [],
            "reason": "the page answered HTTP 502"}
    data = build(tmp_path, s, [ran("plain", s.scenarios[0].expect), down])
    assert data["gate"] == "FAIL" and price_of(data)["graded_reason"] == ("failed, but not on the planted check: the "
                                                                          "page answered HTTP 502")


@pytest.mark.parametrize("stop", ["budget_seconds", "browser_error", "stale"])
def test_a_time_out_or_a_crash_is_never_a_catch(tmp_path, stop):
    data = run(tmp_path, js="the price is 12", stop=stop)  # failed on the right check, but the run broke off
    r = price_of(data)
    assert data["gate"] == "INCOMPLETE" and r["graded"] == "harness" and "never" in r["graded_reason"]


@pytest.mark.parametrize("outcome", ["stuck", "harness", "skipped"])
def test_stuck_harness_and_skipped_stay_as_they_are(tmp_path, outcome):
    data = run(tmp_path, js="the price is 12", outcome=outcome)
    assert data["gate"] == "INCOMPLETE" and price_of(data)["graded"] == outcome


def test_a_planted_check_whose_own_code_broke_is_no_catch():
    r = {"outcome": "fail", "stop": "checked", "reason": "x",
         "checks": [{"check": "js 'a'", "ok": False, "broke": True}]}
    assert verdict.planted(r, "js 'a'")[0] == "fail"


def test_a_run_without_planted_faults_is_unchanged(tmp_path):
    s = suite.parse({"name": "plain", "device": "desktop", "scenarios": [
        {"name": "plain", "url": URL, "about": "the shop opens", "expect": {"text": ["Shop"]}}]})
    data = build(tmp_path, s, [ran("plain", s.scenarios[0].expect, shows=False)])
    assert data["gate"] == "FAIL" and "expected_fail" not in data and "graded" not in data["scenarios"][0]


@pytest.mark.parametrize("named", [PRICE, "  the price  is 10 ", "js 'price() === 10'"])
def test_the_planted_check_is_named_by_its_says_or_its_name(named):
    assert planted_suite(named).scenarios[1].expect_fail == {"check": "js 'price() === 10'", "words": PRICE}


def test_true_names_a_tests_only_check():
    s = suite.parse({"name": "x", "device": "desktop", "scenarios": [
        {"name": "t", "url": URL, "about": "plants a missing title", "expect": {"text": ["Shop"]},
         "expect_fail": True}]})
    assert s.scenarios[0].expect_fail == {"check": "page shows 'Shop'", "words": "the page shows “Shop”"}


def test_device_copies_of_a_planted_test_are_planted_too():
    s = suite.parse({"name": "x", "devices": ["desktop", "phone"], "scenarios": [
        {"name": "t", "url": URL, "about": "plants a missing title", "expect": {"text": ["Shop"]},
         "expect_fail": True}]})
    assert len(s.scenarios) == 2 and all(x.expect_fail for x in s.scenarios)


@pytest.mark.parametrize("change, error", [
    ({"expect_fail": "the price is 11"}, "must name exactly one of the test's checks (it names 0)"),
    ({"expect_fail": True}, "true needs a test with exactly one check (this one has 2)"),
    ({"expect_fail": 3}, "must be the planted check's says text, or true"),
    ({"about": None}, "must say in about: what fault it plants"),
    ({"expect": {"text": ["Shop"], "js": "price() === 10", "says": {"js": "the page shows “Shop”"}},
      "expect_fail": "the page shows “Shop”"}, "it names 2"),
])
def test_an_unclear_planted_test_is_refused(change, error):
    item = {"name": "price", "url": URL, "about": "plants a wrong price",
            "expect": {"text": ["Shop"], "js": "price() === 10", "says": {"js": PRICE}}, "expect_fail": PRICE, **change}
    with pytest.raises(suite.SuiteError) as e:
        suite.parse({"name": "x", "device": "desktop", "scenarios": [item]})
    assert error in str(e.value)


def test_a_scenario_with_clients_cannot_be_planted():
    with pytest.raises(suite.SuiteError, match="clients cannot be expected to fail"):
        suite.parse({"name": "x", "device": "desktop", "scenarios": [
            {"name": "m", "url": URL, "about": "two players", "clients": [{"name": "a"}, {"name": "b"}],
             "steps": [{"all": {"js": "go()"}}], "expect": {"text": ["Go"]}, "expect_fail": True}]})


def test_qajev_plan_refuses_an_unclear_planted_test_with_the_config_exit(tmp_path, capsys):
    f = tmp_path / "s.yaml"
    f.write_text(yaml.safe_dump({"name": "x", "device": "desktop", "scenarios": [
        {"name": "t", "url": URL, "about": "plants a wrong price", "expect": {"text": ["Shop", "Cart"]},
         "expect_fail": True}]}))
    assert cli.main(["plan", str(f)]) == 3
    assert "exactly one check" in capsys.readouterr().err


def test_check_takes_expect_fail_with_or_without_the_check_it_names():
    parse = cli.build_parser().parse_args
    bare = parse(["check", URL, "--expect-text", "Shop", "--about", "plants a missing title", "--device", "desktop",
                  "--expect-fail"])
    assert bare.expect_fail is True
    assert cli.check_suite(bare).scenarios[0].expect_fail["check"] == "page shows 'Shop'"
    named = parse(["check", URL, "--expect-text", "Shop", "--expect-js", "price() === 10", "--says", PRICE,
                   "--about", "plants a wrong price", "--device", "desktop", "--expect-fail", PRICE])
    assert cli.check_suite(named).scenarios[0].expect_fail == {"check": "js 'price() === 10'", "words": PRICE}
    assert cli.check_suite(parse(["check", URL, "--expect-text", "Shop", "--device", "desktop"])).scenarios[0] \
        .expect_fail is None


def test_qa_check_passes_expect_fail_to_the_cli(monkeypatch):
    seen = []

    async def fake_report(args, ctx, background, title=None):
        seen.append(args)
        return {"gate": "PASS", "run_dir": "/runs/x", "scenarios": []}

    monkeypatch.setattr(mcp_server, "_run_report", fake_report)
    asyncio.run(mcp_server.qa_check(URL, None, expect_text=["Shop"], about="plants", expect_fail=True))
    asyncio.run(mcp_server.qa_check(URL, None, expect_js="price() === 10", says=PRICE, about="p", expect_fail=PRICE))
    asyncio.run(mcp_server.qa_check(URL, None, expect_text=["Shop"], about="plain"))
    parse = cli.build_parser().parse_args
    assert [parse(args).expect_fail for args in seen] == [True, PRICE, None]


def test_the_reports_tag_each_planted_test_and_count_them(tmp_path):
    data = run(tmp_path, js="the price is 12")
    report.write(tmp_path, data)
    md, html = (tmp_path / "report.md").read_text(), (tmp_path / "report.html").read_text()
    for text in (md, html):
        assert f"expected to fail on “{PRICE}”" in text
        assert "1 test(s) expected to fail (planted faults)" in text
        assert "failed as expected on the planted check" in text
    saved = json.loads((tmp_path / "report.json").read_text())
    assert saved["expected_fail"] == 1 and price_of(saved)["expected_check"] == PRICE


def test_an_agent_sees_how_the_gate_counted_a_planted_test(tmp_path):
    trimmed = mcp_server._trim(run(tmp_path), verbose=False)
    r = price_of(trimmed)
    assert (r["expected"], r["expected_check"], r["graded"]) == ("fail", PRICE, "fail")
    assert r["graded_reason"] == verdict.PLANTED_MISSED and trimmed["expected_fail"] == 1


def test_the_terminal_says_how_each_planted_test_counted(tmp_path, capsys):
    args = SimpleNamespace(json=False, quiet=False)
    assert cli._finish(args, run(tmp_path)) == 1
    assert f"expected to fail: price: {verdict.PLANTED_MISSED}" in capsys.readouterr().err
