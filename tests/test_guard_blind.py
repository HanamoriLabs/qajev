"""A goal stops before Jev's next move when its only unmet checks are absent checks that QAJev's guard blinds.

redteam L1 on #256: in read-only mode the guard hides a Save button, so `absent: ["Save"]` can never hold on that
page. drive() stopped only when every check held, so such a goal ran Jev to its step budget, with paid calls, and the
result was still harness.
"""

import time
from types import SimpleNamespace

import pytest

from qajev import runner, suite, verdict

HID = "hidden by QAJev's guard (read-only: Save), so QAJev cannot tell if the page has it"


def check(name, ok, detail=None):
    return {"check": name, "ok": ok, "detail": detail}


@pytest.mark.parametrize("checks, named", [
    ([check("page shows 'Account'", True), check("page lacks 'Save'", False, HID)], ["page lacks 'Save'"]),
    ([check("page lacks 'Save'", False, HID), check("page lacks 'Approve'", False, HID.replace("Save", "Approve"))],
     ["page lacks 'Save'", "page lacks 'Approve'"]),
    # another check fails too: Jev's moves can still change it
    ([check("page shows 'Account'", False), check("page lacks 'Save'", False, HID)], []),
    ([check("page lacks 'Save'", False, "still shown")], []),  # the page shows it: a plain fail
    # a shown-text check the guard hid: its words can still show in the page's own text after a move
    ([check("page shows 'Save'", False, "hidden by QAJev's guard (read-only: Save)")], []),
    ([check("page shows 'Account'", True)], []),
    ([], []),
])
def test_only_absent_checks_the_guard_blinds_are_named(checks, named):
    assert [c["check"] for c in verdict.guard_blind(checks)] == named


def drive(blind_after=None, actions=5):
    """Jev's loop on a page whose absent check the guard blinds once Jev has taken `blind_after` actions (None: never).
    Each of Jev's moves is one decision and one action. -> (stop, detail, decisions)."""
    state = {"status": "ready", "history": [], "decisions": [], "page": {"fingerprint": "start"}}

    def tick():
        n = len(state["history"]) + 1
        state["decisions"].append({"operation": "CLICK"})
        state["history"].append({"step": n, "action": f"Next {n}", "kind": "click", "page_changed": True})
        state["page"] = {"fingerprint": f"page {n}"}

    def blind():
        return "page lacks 'Save'" if blind_after is not None and len(state["history"]) >= blind_after else None

    session = SimpleNamespace(agent=SimpleNamespace(state=state), tick=tick, observe=lambda: None,
                              scroll_further=lambda: False)
    stop, detail = runner.drive(session, SimpleNamespace(budget={"actions": actions, "seconds": 60}),
                                read=lambda: None, page_ok=lambda: False, started=time.monotonic(), blind=blind)
    return stop, detail, len(state["decisions"])


def test_a_goal_blind_from_the_first_read_makes_no_jev_call():
    assert drive(blind_after=0) == ("guard_blind", "page lacks 'Save'", 0)


def test_a_goal_that_moves_to_a_blind_page_stops_there():
    assert drive(blind_after=2) == ("guard_blind", "page lacks 'Save'", 2)


def test_a_goal_the_guard_never_blinds_runs_as_before():
    assert drive() == ("budget_actions", "5 actions", 5)


class ReadOnlyPage:
    """A read-only page whose Save button QAJev's guard hid. probe answers as session.probe does; `account` says if
    the page's other check holds. Each Jev move is counted, and it says DONE after its first. hooks: the hooks run."""
    assists = []

    def __init__(self, account=True):
        self.account = account
        self.hooks = []
        self.agent = SimpleNamespace(state={"status": "ready", "history": [], "decisions": [], "text_calls": [],
                                            "elapsed_ms": 0})
        self.ledger = SimpleNamespace(spent=lambda: 0.0)

    def set_device(self, device): pass
    def arm(self, mode, speech=None): pass
    def check_host(self, url): pass
    def navigate(self, url): self.url = url
    def reset_agent(self, task): pass
    def why_failed(self, url): return None
    def observe(self): pass
    def run_hook(self, hook, url): self.hooks.append(hook)

    def probe(self, expect):
        return {"url": self.url, "status": 200, "text": [self.account], "absent": [False],
                "absent_guarded": ["read-only: Save"], "probe": {}}

    def tick(self):
        self.agent.state["decisions"].append({"operation": "DONE"})
        self.agent.state["status"] = "done"


def run(page, after=(), **expect):
    scenario = suite.parse({"scenarios": [{
        "name": "account", "url": "http://127.0.0.1:8765/", "settle": 0, "mode": "readonly",
        "goal": "Open the account page. Stop when you see Account.", "after": list(after),
        "expect": {"text": "Account", "absent": ["Save"], **expect}}]}).scenarios[0]
    return runner.run_scenario(page, scenario, opts=runner.Options(), hosts={"127.0.0.1"}, run_dir=None)


def test_a_goal_scenario_whose_absent_check_the_guard_blinds_makes_0_jev_calls(monkeypatch):
    # redteam's plant for L1 on #256
    monkeypatch.setattr(runner, "wait_for_quiet", lambda opts: (True, 1.0))
    result = run(ReadOnlyPage())
    assert (result["stop"], result["jev"]["decisions"]) == ("guard_blind", 0), result["reason"]
    assert result["outcome"] == "harness", result["reason"]
    assert "read-only: Save" in result["reason"], result["reason"]


def test_a_guard_blind_stop_skips_the_paid_looks_judge_and_the_after_hooks(monkeypatch):
    # redteam B1 on #257: guard_blind is a harness stop, so nothing after Jev's loop spends more. The looks judge is a
    # paid Clef call, and the after hooks judge nothing on a page no check can judge.
    monkeypatch.setattr(runner, "wait_for_quiet", lambda opts: (True, 1.0))
    judged = []
    monkeypatch.setattr(runner, "judge_looks", lambda *a, **k: judged.append("looks") or ([], None))
    page = ReadOnlyPage()
    result = run(page, after=[{"js": "account.close()"}], looks=["The account page shows the plan."])
    assert (result["stop"], judged, page.hooks) == ("guard_blind", [], []), result["reason"]
    assert result["outcome"] == "harness", result["reason"]


def test_a_goal_with_another_unmet_check_still_asks_jev(monkeypatch):
    monkeypatch.setattr(runner, "wait_for_quiet", lambda opts: (True, 1.0))
    result = run(ReadOnlyPage(account=False))
    assert (result["stop"], result["jev"]["decisions"]) == ("done", 1), result["reason"]
