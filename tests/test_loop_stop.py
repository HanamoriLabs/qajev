"""A step stops as a loop when Jev clicks one target again and again and the page does not move on.

#802, 30 Oct: run 9 clicked one box 9 times and run 8 clicked it 11 times. Each spent the 25-action budget and said
nothing about why.
"""

import time
from types import SimpleNamespace

import pytest

from qajev import runner, verdict


def drive(clicks, prints, then="done"):
    """Jev clicks `clicks` (labels) in turn, and the page's fingerprint after each is the matching one in `prints`."""
    state = {"status": "ready", "history": [], "decisions": [], "page": {"fingerprint": "start"}}
    moves = iter(zip(clicks, prints))

    def tick():
        move = next(moves, None)
        if move is None:
            state["status"] = then
            return
        label, print_ = move
        kind = "fill" if label.startswith("type ") else "click"
        state["decisions"].append({"operation": "CLICK"})
        state["history"].append({"step": len(state["history"]) + 1, "action": label.removeprefix("type "),
                                 "kind": kind, "text": "x" if kind == "fill" else None})
        state["page"] = {"fingerprint": print_}

    session = SimpleNamespace(agent=SimpleNamespace(state=state), tick=tick, observe=lambda: None,
                              scroll_further=lambda: False)
    stop, detail = runner.drive(session, SimpleNamespace(budget={"actions": 25, "seconds": 60}), read=lambda: None,
                                page_ok=lambda: False, started=time.monotonic())
    return stop, detail, len(state["history"])


def test_a_button_that_does_nothing_stops_after_3_clicks_and_says_on_what():
    stop, detail, actions = drive(["Save"] * 9, ["start"] * 9)
    assert (stop, actions) == ("loop", 3)
    assert detail.startswith("'Save' 3 times in a row, and the page did not move on")
    assert "the last actions: click 'Save'; click 'Save'; click 'Save'" in detail


def test_a_box_that_toggles_back_and_forth_is_a_loop_too():
    stop, _, actions = drive(["Remember me"] * 11, ["on", "start"] * 6)
    assert (stop, actions) == ("loop", 3)


@pytest.mark.parametrize("clicks, prints", [
    (["Load more"] * 4, ["p1", "p2", "p3", "p4"]),                  # each click shows a new page
    (["Save", "Save", "type Email", "Save", "Save"], ["start"] * 5),  # typing between breaks the run of clicks
    (["Next", "Next", "Back", "Next", "Next"], ["start"] * 5),       # so does another target
])
def test_clicks_that_move_the_page_on_or_are_broken_up_are_no_loop(clicks, prints):
    stop, _, actions = drive(clicks, prints)
    assert (stop, actions) == ("done", len(clicks))


def test_a_loop_is_harness_with_the_reason_unless_the_page_still_passes():
    outcome, reason = verdict.classify("loop", [{"check": "page shows 'Saved'", "ok": False, "detail": None}],
                                       has_checks=True)[:2]
    assert outcome == "harness"
    assert reason.startswith("Jev clicked one target again and again")
    # With no checks at all, a loop is still QAJev's trouble: not "unverified"
    assert verdict.classify("loop", [], has_checks=False, stop_detail="'Save' 3 times")[0] == "harness"
