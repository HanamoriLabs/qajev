"""The fixture site in read-only mode: the guard hides its Save button, so a goal whose only check is
`absent: ["Save"]` can never pass there. QAJev stops before Jev's first move (redteam L1 on #256)."""

import os

import pytest

from test_live import browser, session, site  # noqa: F401  (the fixture server and its Chrome)

pytestmark = pytest.mark.skipif(os.environ.get("QAJEV_LIVE") != "1", reason="set QAJEV_LIVE=1 (starts a local Chrome)")


def test_a_real_goal_whose_absent_check_the_guard_blinds_asks_jev_nothing(session, site, monkeypatch):  # noqa: F811
    from qajev import runner, suite

    asked = []

    def tick():  # stands in for one of Jev's paid decisions: counted, and it says DONE so the run ends
        asked.append("predict")
        session.agent.state["status"] = "done"

    monkeypatch.setattr(session, "tick", tick)
    scenario = suite.parse({"scenarios": [{
        "name": "home", "url": site + "/", "settle": 0, "mode": "readonly",
        "goal": "Read the home page. Stop when you have read it.", "expect": {"absent": ["Save"]}}]}).scenarios[0]
    result = runner.run_scenario(session, scenario, opts=runner.Options(shots=False, ux="off", load_high=None),
                                 hosts={site.split("//")[1]}, run_dir=None)
    assert (result["stop"], asked) == ("guard_blind", []), result["reason"]
    assert result["outcome"] == "harness", result["reason"]
    assert "hidden by QAJev's guard (" in result["reason"], result["reason"]
