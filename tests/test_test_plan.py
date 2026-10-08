"""No QAJev run without a test plan the Orchestrator approved (José, 8 Oct): the plan is a Markdown file in the project
(tools/qa/plans/<date>-<name>.md), and its last approval line carries the sha256 of the plan text above it, so an edit
after the approval counts as not approved. A suite names its plan (`plan:`); a check or a play takes `--plan`. A run
without an approved plan warns now; with QAJEV_REQUIRE_PLAN=1 it is refused before Chrome starts."""

import hashlib
import json

import pytest

from qajev import cli, planfile, report_html

PLAN = """# Test plan: checkout

Why: paying adds the order.

| Test | Pass means |
|---|---|
| pay | the order is saved |
"""


def approve(text, when="8 Oct 2026 10:34"):
    """The approver's line: the sha256 of the plan text above it."""
    return f"{text}\nApproved by the Orchestrator {when} sha256:{planfile.digest(text)}\n"


def test_the_digest_is_the_sha256_of_the_plan_text_whatever_its_line_ends():
    want = hashlib.sha256((PLAN.rstrip() + "\n").encode()).hexdigest()
    assert planfile.digest(PLAN) == want
    assert planfile.digest(PLAN.replace("\n", "\r\n") + "\n\n") == want  # CRLF and trailing blank lines: the same plan


def test_a_plan_approved_with_its_hash_is_approved(tmp_path):
    p = tmp_path / "plan.md"
    p.write_text(approve(PLAN))
    got = planfile.check(p)
    assert got["approved"] is True and got["problem"] is None
    assert got["approval"].startswith("Approved by the Orchestrator 8 Oct 2026 10:34 sha256:")
    assert got["sha256"] == planfile.digest(PLAN) and got["path"] == str(p)


@pytest.mark.parametrize("text, problem", [
    (PLAN, "has no line that starts “Approved by the Orchestrator”"),
    (PLAN + "\nApproved by the Orchestrator 8 Oct 2026 10:34\n", "its approval line has no sha256 of the plan"),
    (approve(PLAN).replace("the order is saved", "the order is paid"), "the plan changed after its approval"),
    (approve(PLAN) + "\n- also: refund the order\n", "text after its last approval line is not approved"),
])
def test_a_plan_without_a_valid_approval_is_not_approved(tmp_path, text, problem):
    p = tmp_path / "plan.md"
    p.write_text(text)
    got = planfile.check(p)
    assert got["approved"] is False and problem in got["problem"]


def test_the_last_approval_line_covers_the_whole_plan_and_its_additions(tmp_path):
    first = approve(PLAN)
    added = first + "\n## Addition\n\n- rerun all tests on the fix\n"
    p = tmp_path / "plan.md"
    p.write_text(f"{added}\nApproved by the Orchestrator 8 Oct 2026 10:45 sha256:{planfile.digest(added)}\n")
    assert planfile.check(p)["approved"] is True


def test_a_missing_plan_file_or_no_plan_is_a_problem(tmp_path):
    assert planfile.check(tmp_path / "nope.md")["problem"] == "no such file"
    assert planfile.check(None) == {"path": None, "approved": False, "approval": None, "approved_at": None,
                                    "sha256": None, "problem": "no test plan named (plan: in the suite, or --plan)"}


def test_a_suite_names_its_plan_from_its_folder_or_the_project_root(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)  # the project root
    (root / "tools/qa/plans").mkdir(parents=True)
    (root / "tools/qa/plans/2026-10-08-checkout.md").write_text(approve(PLAN))
    (root / "qa").mkdir()
    suite = root / "qa" / "checkout.qajev.yaml"
    suite.write_text("name: checkout\nplan: tools/qa/plans/2026-10-08-checkout.md\nbase_url: http://127.0.0.1:9\n"
                     "scenarios:\n  - name: pay\n    about: Paying adds the order\n    url: /\n"
                     "    expect: {text: [Paid]}\n")
    from qajev.suite import load

    s = load(suite)
    assert s.plan == "tools/qa/plans/2026-10-08-checkout.md"
    assert planfile.resolve(s.plan, suite.parent) == root / "tools/qa/plans/2026-10-08-checkout.md"


def _suite(tmp_path, plan_line=""):
    suite = tmp_path / "shop.qajev.yaml"
    suite.write_text(f"name: shop\n{plan_line}base_url: http://127.0.0.1:9\nscenarios:\n"
                     "  - name: pay\n    about: Paying adds the order\n    url: /\n    expect: {text: [Paid]}\n")
    return suite


def test_qajev_plan_shows_the_test_plan_and_fails_only_when_one_is_required(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("QAJEV_REQUIRE_PLAN", raising=False)
    suite = _suite(tmp_path)
    assert cli.main(["plan", str(suite), "--json"]) == 0  # warns now: listed, not failed
    data = json.loads(capsys.readouterr().out)
    assert data["test_plan"]["approved"] is False and "no test plan named" in data["test_plan"]["problem"]
    monkeypatch.setenv("QAJEV_REQUIRE_PLAN", "1")
    assert cli.main(["plan", str(suite)]) == 2
    assert "Plan file: NOT APPROVED (no test plan named" in capsys.readouterr().out
    (tmp_path / "plan.md").write_text(approve(PLAN))
    assert cli.main(["plan", str(_suite(tmp_path, "plan: plan.md\n"))]) == 0
    out = capsys.readouterr().out
    assert "Plan file: " in out and "approved: Approved by the Orchestrator" in out


def test_qajev_plan_hash_prints_the_line_to_approve_a_plan(tmp_path, capsys):
    p = tmp_path / "plan.md"
    p.write_text(PLAN)
    assert cli.main(["plan-hash", str(p)]) == 0
    assert capsys.readouterr().out.strip() == f"sha256:{planfile.digest(PLAN)}"


def test_a_run_without_an_approved_plan_warns_and_with_the_rule_on_is_refused_before_chrome(tmp_path, capsys,
                                                                                           monkeypatch):
    ran = []

    def fake_run(suite, opts):
        ran.append(suite.test_plan)
        return {"gate": "PASS", "exit_code": 0, "suite": suite.name, "scenarios": [], "run_dir": str(tmp_path)}

    monkeypatch.setattr("qajev.runner.run", fake_run)
    monkeypatch.setattr(cli, "_finish", lambda args, report: report["exit_code"])
    monkeypatch.delenv("QAJEV_REQUIRE_PLAN", raising=False)
    suite = _suite(tmp_path)
    assert cli.main(["run", str(suite), "--headless"]) == 0
    assert ran[0]["approved"] is False
    assert "qajev: warning: no approved test plan: no test plan named" in capsys.readouterr().err
    monkeypatch.setenv("QAJEV_REQUIRE_PLAN", "1")
    assert cli.main(["run", str(suite), "--headless", "--json"]) == 3 and len(ran) == 1  # Chrome never started
    assert "needs an approved test plan" in json.loads(capsys.readouterr().out)["error"]
    (tmp_path / "plan.md").write_text(approve(PLAN))
    assert cli.main(["run", str(suite), "--headless", "--plan", str(tmp_path / "plan.md")]) == 0
    assert ran[-1]["approved"] is True


def test_the_report_shows_the_plan_its_approval_and_hash_or_a_banner(tmp_path):
    from pathlib import Path

    from qajev import report

    p = tmp_path / "plan.md"
    p.write_text(approve(PLAN))
    suite = type("S", (), {"name": "shop", "about": None, "scenarios": [], "guard": {},
                           "test_plan": planfile.check(p)})()
    built = report.build(suite, [], [], browser={}, started_at=0, strict=False, interrupted=False, run_dir=tmp_path)
    assert built["test_plan"]["approved"] is True
    saved = json.loads((Path(__file__).parent / "fixtures" / "six-game-report.json").read_text())
    page = report_html.render({**saved, "test_plan": built["test_plan"]})
    assert str(p) in page and f"sha256:{planfile.digest(PLAN)}" in page and "Approved by the Orchestrator" in page
    assert "No approved test plan" not in page
    assert f"Test plan: {p}, approved: Approved by the Orchestrator" in report.markdown({**saved, **built,
                                                                                          "cost": saved["cost"]})
    page = report_html.render({**saved, "test_plan": planfile.check(None)})
    assert "No approved test plan" in page and "no test plan named" in page


def test_a_plan_is_looked_for_up_to_the_project_root_never_above_it(tmp_path):
    # Review of #73: a decoy plan in a folder above the project must never be found.
    (tmp_path / "tools/qa/plans").mkdir(parents=True)
    (tmp_path / "tools/qa/plans/decoy.md").write_text(approve(PLAN))
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "qa").mkdir()
    found = planfile.resolve("tools/qa/plans/decoy.md", root / "qa")
    assert found == root / "qa" / "tools/qa/plans/decoy.md" and not found.is_file()
    assert planfile.check(found)["problem"] == "no such file"
    (root / "tools/qa/plans").mkdir(parents=True)
    (root / "tools/qa/plans/decoy.md").write_text(approve(PLAN))
    assert planfile.resolve("tools/qa/plans/decoy.md", root / "qa") == root / "tools/qa/plans/decoy.md"


def test_an_unreadable_plan_is_not_approved_and_never_crashes(tmp_path, capsys):
    bad = tmp_path / "plan.md"
    bad.write_bytes(b"# Plan\n\xff\xfe not utf-8\n")
    got = planfile.check(bad)
    assert got["approved"] is False and got["problem"].startswith("cannot read the plan")
    assert cli.main(["plan-hash", str(bad)]) == 3
    assert "cannot read the plan" in capsys.readouterr().err


def test_the_report_header_says_when_the_plan_was_approved(tmp_path):
    from pathlib import Path

    p = tmp_path / "plan.md"
    p.write_text(approve(PLAN, when="8 Oct 2026 10:34"))
    status = planfile.check(p)
    assert status["approved_at"] == "8 Oct 2026 10:34"
    saved = json.loads((Path(__file__).parent / "fixtures" / "six-game-report.json").read_text())
    page = report_html.render({**saved, "test_plan": status})
    header = page[page.index("<h1"):page.index('<div class="gate')]
    assert "plan approved 8 Oct 2026 10:34" in header
