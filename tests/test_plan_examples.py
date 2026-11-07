"""The test plan examples in examples/plans/ are what agents copy (docs/test-plans.md). They must stay complete and
valid: each plan has every section of the template, none is approved by itself, the example suite lints clean and
names its plan, and an approval line made with `qajev plan-hash` turns a copy into an approved plan, which one edit
makes not approved again."""

import json
import re
import shutil
from pathlib import Path

import pytest

from qajev import cli, planfile

ROOT = Path(__file__).resolve().parent.parent
PLANS = ROOT / "examples" / "plans"
EXAMPLES = sorted(p for p in PLANS.glob("2026-*.md"))
SECTIONS = ["Why", "Target", "When", "The runs", "What each", "Expected result", "Cost and load",
            "Data and accounts", "The stop rule", "The result"]


def suite_plan(path):
    from qajev import suite
    return suite.load(path).plan


def _headings(text):
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


def test_there_are_three_examples_and_a_template():
    assert len(EXAMPLES) == 3
    assert (PLANS / "TEMPLATE.md").is_file() and (PLANS / "waitlist.yaml").is_file()


@pytest.mark.parametrize("path", [PLANS / "TEMPLATE.md", *EXAMPLES], ids=lambda p: p.name)
def test_each_plan_has_every_section_in_order(path):
    heads = _headings(path.read_text())
    missing = [s for s in SECTIONS if not any(h.startswith(s) for h in heads)]
    assert not missing, f"{path.name} lacks {missing}"
    found = [next(i for i, h in enumerate(heads) if h.startswith(s)) for s in SECTIONS]
    assert found == sorted(found), f"{path.name}: sections out of order"


@pytest.mark.parametrize("path", [PLANS / "TEMPLATE.md", *EXAMPLES], ids=lambda p: p.name)
def test_no_example_is_approved_by_itself(path):
    got = planfile.check(path)
    assert got["approved"] is False and "has no line that starts" in got["problem"]
    assert not re.search(r"(?m)^Approved by ", path.read_text())


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_each_example_run_names_its_plan(path):
    text = path.read_text()
    runs = text.split("## The runs", 1)[1].split("\n## ", 1)[0]
    commands = re.findall(r"`(qajev [^`]+)`", runs)
    assert commands, f"{path.name}: no command in The runs"
    for command in commands:
        suite = re.search(r"qajev run (\S+\.yaml)", command)
        if suite:  # the suite names the plan with plan:
            assert suite_plan(ROOT / suite.group(1)) == path.name, f"{path.name}: {command!r} names another plan"
        else:
            assert f"--plan tools/qa/plans/{path.name}" in command, f"{path.name}: {command!r} does not name the plan"


def test_the_example_suite_lints_clean_and_names_its_plan(capsys, monkeypatch):
    monkeypatch.delenv("QAJEV_REQUIRE_PLAN", raising=False)
    assert cli.main(["plan", str(PLANS / "waitlist.yaml"), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert not data["not_described"] and not data["absent_only"] and not data["key_goals"]
    assert data["test_plan"]["path"] == str(PLANS / "2026-11-07-waitlist-local.md")
    assert data["test_plan"]["approved"] is False


def test_an_approval_line_from_plan_hash_approves_a_copy_and_an_edit_undoes_it(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    plan = tmp_path / "2026-11-07-waitlist-local.md"
    shutil.copy(PLANS / plan.name, plan)
    shutil.copy(PLANS / "waitlist.yaml", tmp_path / "waitlist.yaml")
    assert cli.main(["plan-hash", str(plan)]) == 0
    sha = capsys.readouterr().out.strip()
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", sha)
    plan.write_text(plan.read_text() + f"\nApproved by the Orchestrator 7 Nov 2026 18:14 {sha}\n")
    assert planfile.check(plan)["approved"] is True
    assert cli.main(["plan", str(tmp_path / "waitlist.yaml"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["test_plan"]["approved"] is True
    plan.write_text(plan.read_text().replace("$0.60", "$6.00"))
    got = planfile.check(plan)
    assert got["approved"] is False and "the plan changed after its approval" in got["problem"]


def test_the_guide_links_only_files_that_exist():
    guide = ROOT / "docs" / "test-plans.md"
    for link in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", guide.read_text()):
        if not link.startswith("http"):
            assert (guide.parent / link).resolve().exists(), link
