import json
import re
from html import escape
from pathlib import Path

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
    assert cli.main(["plan", str(site)]) == 2  # every test also says the broken state it catches (fails_when)
    site.write_text(site.read_text().replace("    url: /\n", "    fails_when: it does not\n    url: /\n"))
    assert cli.main(["plan", str(site)]) == 0


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
