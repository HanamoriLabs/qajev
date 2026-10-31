import json
import re
from html import escape
from pathlib import Path

import pytest

from qajev import plan, report_html

SIX_GAME = Path(__file__).parent / "fixtures" / "six-game-report.json"


def test_the_six_game_report_opens_with_its_test_plan_and_says_what_each_test_proved():
    # José, 5 Oct: test 7 ("shells: three in a row") said only "The page's own script check should come out true",
    # though its suite says what it proves. A report opens with the plan: one checkbox per test, in plain words.
    data = json.loads(SIX_GAME.read_text())
    page = report_html.render(data)
    block = page[page.index('id="plan"'):]
    block = block[:block.index("</ol>")]
    items = re.findall(r'<li class="plan-item (\w+)"><span class="box (\w+)">', block)  # each test opens with its box
    assert len(items) == 8 and all(item == box for item, box in items), items
    # the fixture keeps the run as graded before js checks had to return exactly true; this test is about the plan
    shells = data["scenarios"][6]
    assert shells["name"] == "shells: three in a row"
    test7 = page[page.index('id="s6"'):page.index('id="s7"')]  # sections count from 0
    proved = test7[test7.index("What it proved"):]
    assert escape(shells["about"]) in proved
    assert "script check should come out true" not in page


def test_a_check_says_what_it_proves_in_plain_words_or_is_flagged():
    assert plan.words({"check": "page shows 'Pro plan'"}) == "the page shows “Pro plan”"
    assert plan.words({"check": "url contains '/pricing'"}) == "the address contains “/pricing”"
    assert plan.words({"check": "js 'window.ok === true'", "says": "the cart saved"}) == "the cart saved"
    assert plan.words({"check": "js 'window.ok === true'"}) is None  # only its author can say
    assert plan.words({"check": "[p2] page shows 'Hi'"}) == "[p2] the page shows “Hi”"
    it = plan.item(1, "pay", "Paying works", [{"check": "js 'x'", "ok": True}], "pass")
    assert not it["described"] and plan.not_described([it]) == ["pay"]
    assert not plan.item(1, "pay", None, [], "pass")["described"]  # no about: nothing stated


def test_qajev_plan_lists_a_suites_plan_and_its_not_described_tests_without_running(tmp_path, capsys):
    # SideGame1, 6 Oct: a lint before the PR. `qajev plan FILE` prints the plan, opens no browser, spends nothing,
    # and exits 2 (as INCOMPLETE would) while a test does not say what it proves.
    from qajev import cli

    site = tmp_path / "site.qajev.yaml"
    site.write_text(
        "name: shop\nbase_url: http://127.0.0.1:9\nscenarios:\n"
        "  - name: pay\n    about: Paying adds the order\n    url: /\n"
        "    expect: {text: [Paid], js: 'window.paid === true', says: {js: the order is saved}}\n"
        "  - name: look\n    url: /\n    expect: {js: 'window.ok === true'}\n")
    assert cli.main(["plan", str(site)]) == 2
    out = capsys.readouterr().out
    assert "1. pay: Paying adds the order" in out and "the page shows “Paid”" in out and "the order is saved" in out
    assert "2. look: NOT DESCRIBED" in out and "1 of 2 tests say what they prove" in out
    game = tmp_path / "game.suite.yaml"
    game.write_text(
        "name: boss\nabout: the first boss can be beaten\nsteps:\n"
        "  - name: title\n    about: the game opens on its title\n    expect: {screen: TITLE, min_fps: 30}\n"
        "  - name: fight\n    play: {seconds: 60, until: {boss_hp: '<= 0'}}\n")
    assert cli.main(["plan", str(game), "--json"]) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["not_described"] == ["fight"]
    title, fight = data["plan"]
    assert [c["words"] for c in title["checks"]] == [
        "the screen is TITLE", "the game runs at 30 frames a second or more", "no script errors"]
    assert fight["checks"][0]["words"] == "the game reaches boss_hp <= 0 in time"
    site.write_text(site.read_text().replace("expect: {js: 'window.ok === true'}",
                                             "about: the home page opens\n    expect: {text: [Welcome]}"))
    assert cli.main(["plan", str(site)]) == 0


def test_a_test_whose_only_checks_say_a_text_is_absent_is_flagged(tmp_path, capsys):
    # Foley claim 13, 13 Oct: "B accepts A's invite" only checked that the page lacks "Sign in or create account". A
    # blank page passes that: the step passed in 0.89 s with 0 actions, the next step closed the page before its accept
    # went out, and we reported a Foley bug that was not there (r_mv68tohf521ef3f1). A test with "lacks" checks and
    # nothing that must be there proves nothing, so the plan flags it and exits 2.
    from qajev import cli

    site = tmp_path / "site.qajev.yaml"
    site.write_text(
        "name: invite\nbase_url: http://127.0.0.1:9\nscenarios:\n"
        "  - name: accept\n    about: B accepts the invite\n    url: /accept\n"
        "    expect: {absent: [Sign in or create account]}\n"
        "  - name: list\n    about: B sees the project\n    url: /\n"
        "    expect: {text: [Shared test], absent: [That did not work]}\n")
    assert cli.main(["plan", str(site), "--json"]) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["absent_only"] == ["accept"] and data["not_described"] == []
    assert cli.main(["plan", str(site)]) == 2
    out = capsys.readouterr().out
    assert "ABSENT ONLY: accept" in out and "a blank page passes" in out
    site.write_text(site.read_text().replace("expect: {absent: [Sign in or create account]}",
                                             "expect: {text: [You are in], absent: [Sign in or create account]}"))
    assert cli.main(["plan", str(site), "--json"]) == 0  # with a text that must be there, it proves something
    assert json.loads(capsys.readouterr().out)["absent_only"] == []


def test_a_goal_that_asks_jev_to_press_a_key_is_flagged(tmp_path, capsys):
    # verse #1000, 31 Oct: the goal said "press the Right arrow key 3 times". Jev cannot press keys, so it clicked the
    # step "Adjust" 8 times. Key presses go in a key: hook; the plan says so before a run, and exits 2.
    from qajev import cli

    site = tmp_path / "site.qajev.yaml"
    site.write_text(
        "name: fit\nbase_url: http://127.0.0.1:9\nscenarios:\n"
        "  - name: refit\n    about: a size release refits\n    url: /\n"
        "    goal: Click the tab Fit (Alt+4). Press the Right arrow key 3 times. Stop when it says Fitted again.\n"
        "    expect: {text: [Fitted again]}\n"
        "  - name: open\n    about: the Fit tab opens\n    url: /\n"
        "    goal: Click the tab Fit (Alt+4), then the Press kit link. Stop when it says 4 poses.\n"
        "    expect: {text: [4 poses]}\n")
    assert cli.main(["plan", str(site), "--json"]) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["key_goals"] == [{"name": "refit", "says": "Press the Right arrow key"}]
    assert cli.main(["plan", str(site)]) == 2
    out = capsys.readouterr().out
    assert "PRESSES KEYS: refit (“Press the Right arrow key”)" in out and "key:" in out
    site.write_text(site.read_text().replace("Press the Right arrow key 3 times. ", ""))
    assert cli.main(["plan", str(site), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["key_goals"] == []


@pytest.mark.parametrize("goal, says", [
    ("Type your name, then press Enter.", "press Enter"),
    ("Hit the Escape key to close it.", "Hit the Escape key"),
    ("Save it with Ctrl+S.", "Ctrl+S"),
    ("Press Space to jump.", "Press Space"),
    ("Hold down the Shift key and click two rows.", "Hold down the Shift key"),
    ("Click Save. Stop when it says Saved.", None),
    ("Press the Save button, then return to the list.", None),
    ("Open the menu Releases (Alt+9).", None),
    ("Press the Tab named Width.", None),
    # redteam on #222: a chord of 3 keys in brackets is a label too (L1); a key word in a control's name is no key
    # (L2); and the misses it found (N1)
    ("Click Redo (Ctrl+Shift+Z).", None),
    ("Open the inspector [Cmd+Option+I].", None),
    ("Save it ( Ctrl+S ) now.", None),
    ("Undo with Ctrl+Shift+Z.", "Ctrl+Shift+Z"),
    ("Press the API keys tab.", None),
    ("Hit the Next arrow on the carousel.", None),
    ("Press Enter site on the age gate.", None),
    ("Pick the Option + Extras bundle.", None),
    ("Hitting Enter sends it.", "Hitting Enter"),
    ("Use the arrow keys to move.", "Use the arrow keys"),
    ("Press ArrowRight twice.", "Press ArrowRight"),
    ("Hold Shift and click two rows.", "Hold Shift"),
    # redteam on #223: a modifier by itself is a key only when held (L1); a chord after "/" is in its brackets (L2);
    # and the misses it found (N3, N4)
    ("Press Alt text to add a description.", None),
    ("Press Control Center in the top bar.", None),
    ("Click Save (Ctrl/Cmd+S).", None),
    ("Hold down Alt and drag the box.", "Hold down Alt"),
    ("Press Cmd+K to search.", "Cmd+K"),
    ("Using the arrow keys, move it.", "Using the arrow keys"),
    ("Press Page Down twice.", "Press Page Down"),
    ("Press PageDown twice.", "Press PageDown"),
])
def test_the_key_lint_reads_key_presses_and_not_buttons_or_labels(goal, says):
    from types import SimpleNamespace

    from qajev import plan

    found = plan.key_goals([SimpleNamespace(name="t", goal=goal)])
    assert found == ([{"name": "t", "says": says}] if says else [])


def test_every_expect_key_is_decided_for_the_absent_only_lint():
    # redteam, 13 Oct (#129): each check kind either proves something is there or does not. A new expect key fails here
    # until someone decides which list it belongs to. An address or a status alone lets a blank page pass.
    from types import SimpleNamespace

    from qajev import plan
    from qajev import suite as suite_mod

    meta = {"absent", "ignore_case", "says"}
    assert set(plan.POSITIVE) | set(plan.WHERE_ONLY) | meta == suite_mod.EXPECT_KEYS
    assert not set(plan.POSITIVE) & set(plan.WHERE_ONLY)

    def flagged(kind, value):
        return plan.absent_only([SimpleNamespace(name=kind, expect={"absent": ["Sign in"], kind: value})]) == [kind]

    for kind in plan.POSITIVE:
        assert not flagged(kind, ["something"]), kind
    for kind, value in (("url", "/accept"), ("url_regex", ".*"), ("status", 200)):
        assert flagged(kind, value), kind


def test_the_plan_before_a_run_names_the_checks_as_the_run_will(tmp_path):
    # The dashboard shows the plan before the run and ticks it live: a planned check must match the one that runs.
    from qajev import suite as suite_mod
    from qajev import verdict

    path = tmp_path / "s.qajev.yaml"
    path.write_text(
        "name: plan\nbase_url: http://127.0.0.1:9\nscenarios:\n"
        "  - name: pay\n    about: Paying adds the order\n    url: /\n    goal: pay. Stop when paid.\n"
        "    expect: {url: /done, text: [Paid], js: 'window.paid === true', says: {js: the order is saved}}\n"
        "  - name: look\n    url: /\n    goal: look. Stop when seen.\n    expect: {js: 'window.ok === true'}\n")
    scenarios = suite_mod.load(path).scenarios
    before = plan.from_suite(scenarios)
    assert [it["state"] for it in before] == ["todo", "todo"]
    assert [line["words"] for line in before[0]["checks"]] == [
        "the address contains “/done”", "the page shows “Paid”", "the order is saved"]
    assert plan.not_described(before) == ["look"]  # no about, and a js check without says
    observed = {"url": "http://127.0.0.1:9/done", "text": [True], "js": True}
    ran = verdict.page_checks(scenarios[0].expect, observed)
    assert [c["check"] for c in ran] == [line["check"] for line in before[0]["checks"]]
    during = plan.merge(before, [{"name": "pay", "outcome": "pass", "checks": ran}])
    assert [it["state"] for it in during] == ["pass", "todo"]
    assert all(line["ok"] for line in during[0]["checks"])
