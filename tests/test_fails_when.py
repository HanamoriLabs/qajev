"""How do we know a test tests something? (José, 8 Oct.) Each test says the broken state it catches (`fails_when`), and
the lint flags a test whose claim is about motion or the network while every check reads one still moment at the end.
Both are lint: `qajev plan` / `qa_plan` list them; a run never refuses for them."""

import json

import pytest

from qajev import cli, plan, report_html
from qajev.suite import SuiteError, load

SITE = """name: shop
base_url: http://127.0.0.1:9
devices: [desktop]
scenarios:
  - name: pay
    about: Paying adds the order
    fails_when: the order is not saved after paying
    url: /
    expect: {text: [Paid]}
  - name: look
    about: The home page opens
    url: /
    expect: {text: [Welcome]}
"""


def test_a_scenario_says_the_broken_state_it_catches(tmp_path):
    site = tmp_path / "site.qajev.yaml"
    site.write_text(SITE)
    pay, look = load(site).scenarios
    assert pay.fails_when == "the order is not saved after paying" and look.fails_when is None
    site.write_text(SITE.replace("fails_when: the order is not saved after paying", "fails_when: [1, 2]"))
    with pytest.raises(SuiteError, match="fails_when must be text"):
        load(site)


def test_qajev_plan_lists_the_tests_that_do_not_say_what_they_catch(tmp_path, capsys):
    site = tmp_path / "site.qajev.yaml"
    site.write_text(SITE)
    assert cli.main(["plan", str(site), "--json"]) == 2  # required in the lint, as `about` is
    data = json.loads(capsys.readouterr().out)
    assert [it["fails_when"] for it in data["plan"]][:2] == ["the order is not saved after paying", None]
    assert data["fails_when_missing"] == ["look"]
    assert cli.main(["plan", str(site)]) == 2
    out = capsys.readouterr().out
    assert "fails when: the order is not saved after paying" in out and "fails when: NOT STATED" in out
    assert "FAILS WHEN NOT STATED: look (say" in out
    site.write_text(SITE.replace("    about: The home page opens\n",
                                 "    about: The home page opens\n    fails_when: the home page does not load\n"))
    assert cli.main(["plan", str(site)]) == 0


def test_a_game_step_says_what_it_catches_too(tmp_path, capsys):
    game = tmp_path / "game.suite.yaml"
    game.write_text("name: boss\nsteps:\n"
                    "  - name: title\n    about: the game opens on its title\n"
                    "    fails_when: the game opens on a black screen\n    expect: {screen: TITLE}\n"
                    "  - name: fight\n    about: the boss can be beaten\n    play: {seconds: 60, until: {boss_hp: '<= 0'}}\n")
    assert cli.main(["plan", str(game), "--json"]) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["plan"][0]["fails_when"] == "the game opens on a black screen" and data["fails_when_missing"] == ["fight"]


@pytest.mark.parametrize("about, hook, flagged", [
    ("the server feed stays in sync while Jev acts", None, True),
    ("the cube draws at 60 fps", None, True),
    ("the page keeps moving and Jev still acts", None, True),
    ("the cube draws at 60 fps", {"react": {"js": "[]", "until": "true", "for_s": 5}}, False),  # watched over time
    ("the pricing page shows the Pro price", None, False),
])
def test_a_claim_about_motion_read_from_one_still_moment_is_flagged(tmp_path, capsys, about, hook, flagged):
    site = tmp_path / "s.qajev.yaml"
    before = f"    before: [{json.dumps(hook)}]\n" if hook else ""
    site.write_text(f"name: s\nbase_url: http://127.0.0.1:9\ndevices: [desktop]\nscenarios:\n  - name: t\n"
                    f"    about: {about}\n    fails_when: it does not\n    url: /\n{before}    expect: {{text: [Ok]}}\n")
    assert cli.main(["plan", str(site), "--json"]) == 0  # a flag, never a lint failure
    data = json.loads(capsys.readouterr().out)
    assert (data.get("still_state") == ["t"]) is flagged
    if flagged:
        assert plan.STILL_STATE in data["plan"][0]["flags"]


def test_a_game_step_that_plays_or_idles_is_watched_over_time(tmp_path, capsys):
    game = tmp_path / "game.suite.yaml"
    game.write_text("name: g\nsteps:\n"
                    "  - name: run\n    about: the frame rate holds while the level moves\n    fails_when: it stutters\n"
                    "    play: {seconds: 20}\n    expect: {min_fps: 30}\n"
                    "  - name: look\n    about: the HUD animates in\n    fails_when: it does not\n"
                    "    expect: {screen: HUD}\n")
    cli.main(["plan", str(game), "--json"])
    assert json.loads(capsys.readouterr().out)["still_state"] == ["look"]


def test_the_report_plan_shows_what_each_test_catches():
    it = plan.item(1, "pay", "Paying adds the order", [{"check": "page shows 'Paid'", "ok": True}], "pass",
                   fails_when="the order is not saved")
    assert it["fails_when"] == "the order is not saved"
    page = report_html._plan([it], [{"checks": []}])
    assert "Fails when: the order is not saved" in page
