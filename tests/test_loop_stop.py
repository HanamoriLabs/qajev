"""A step stops as a loop when Jev clicks one target again and again and the page does not move on.

#802, 30 Oct: run 9 clicked one box 9 times and run 8 clicked it 11 times. Each spent the 25-action budget and said
nothing about why.
"""

import time
from types import SimpleNamespace

import pytest

from qajev import runner, verdict


def drive(clicks, prints, then="done", told=None, jev_blocks=False, first=None):
    """Jev clicks `clicks` (labels) in turn, and the page's fingerprint after each is the matching one in `prints`
    (or the whole page, as Jev reads it). first: the page before the first click. jev_blocks: like jev_ultrafast's
    agent, Jev says BLOCKED itself after 3 actions in a row that did not change the page."""
    state = {"status": "ready", "history": [], "decisions": [], "page": first or {"fingerprint": "start"}}
    moves = iter(zip(clicks, prints))

    def tick():
        move = next(moves, None)
        if move is None:
            state["status"] = then
            return
        label, print_ = move
        kind = "fill" if label.startswith("type ") else "click"
        state["decisions"].append({"operation": "CLICK"})
        page = print_ if isinstance(print_, dict) else {"fingerprint": print_}
        changed = page["fingerprint"] != state["page"]["fingerprint"]
        state["history"].append({"step": len(state["history"]) + 1, "action": label.removeprefix("type "),
                                 "kind": kind, "text": "x" if kind == "fill" else None, "page_changed": changed})
        state["page"] = page
        if jev_blocks and len(state["history"]) >= 3 and not any(h["page_changed"] for h in state["history"][-3:]):
            state["status"] = "blocked"

    session = SimpleNamespace(agent=SimpleNamespace(state=state), tick=tick, observe=lambda: None,
                              scroll_further=lambda: False)
    stop, detail = runner.drive(session, SimpleNamespace(budget={"actions": 25, "seconds": 60}), read=lambda: None,
                                page_ok=lambda: False, started=time.monotonic(),
                                step=lambda doing, **extra: (told if told is not None else []).append(doing))
    return stop, detail, len(state["history"])


def test_a_button_that_does_nothing_stops_after_3_clicks_and_says_on_what():
    stop, detail, actions = drive(["Save"] * 9, ["start"] * 9)
    assert (stop, actions) == ("loop", 3)
    assert detail.startswith("'Save' 3 times in a row, and the page did not move on")
    assert "the last actions: click 'Save'; click 'Save'; click 'Save'" in detail


def test_the_loop_stops_before_jevs_own_blocked_brings_more_clicks():
    # #802 run 9: Jev said BLOCKED after 3 dead clicks, and QAJev's two scroll-and-ask-again turns made it 3 + 3 + 3
    # (redteam L1 on #209)
    told = []
    stop, _, actions = drive(["Save"] * 9, ["start"] * 9, told=told, jev_blocks=True)
    assert (stop, actions) == ("loop", 3)
    assert not any(doing.startswith("said BLOCKED") for doing in told)


def test_a_page_with_no_fingerprint_is_never_called_a_loop():
    # Nothing to compare, so QAJev cannot say the page did not move on: Jev's own rules decide (redteam N1 on #209)
    stop, _, _ = drive(["Save"] * 3, [None] * 3)
    assert stop == "done"


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


def read(n, said, size="0.0 cm", value="0"):
    """A page as Jev reads it after click n: the step's controls are built again, so each has a new node id and box,
    and Jev's fingerprint is new each time."""
    return {"url": "http://verse.localhost/workshop/#fit", "scroll": {"y": 0, "height": 900 + n},
            "text": f"Fit\nAdjust\n{said}\nOverall size {size}", "fingerprint": f"print {n}",
            "actions": [{"id": "e1", "node": 4, "role": "button", "label": "Adjust", "kind": "click", "value": "",
                         "rect": {"x": 10, "y": 80, "w": 90, "h": 30}},
                        {"id": "e2", "node": 20 + n, "role": "slider", "label": "Overall size", "kind": "click",
                         "value": value, "rect": {"x": 10, "y": 200 + n / 10, "w": 300, "h": 20}}]}


@pytest.mark.parametrize("said", ["Hoodie 3 on the boy in 4 poses ({}.{} s).", "Fitted again in {}{} ms."])
def test_a_step_that_builds_the_same_controls_again_and_says_how_long_it_took_is_a_loop(said):
    # verse #1000, 31 Oct: Jev clicked the step "Adjust" 8 times in a row. Each click built its sliders again and the
    # line under the views gave the time the 4 poses took, so each fingerprint was new and the loop was never seen.
    stop, detail, actions = drive(["Adjust"] * 8, [read(n, said.format(n + 1, 9 - n)) for n in range(8)])
    assert (stop, actions) == ("loop", 3)
    assert detail.startswith("'Adjust' 3 times in a row, and the page did not move on")


@pytest.mark.parametrize("shown", ["words", "value"])
def test_a_control_built_again_that_shows_a_new_value_each_time_is_no_loop(shown):
    # A "+" that raises a size 1 cm a click moves the page on, even when it builds its controls again: in the words,
    # or only in the slider's own value
    pages = [read(n, "Fitted again in 412 ms.", **({"size": f"+{n}.0 cm"} if shown == "words" else {"value": str(n)}))
             for n in range(4)]
    stop, _, actions = drive(["+"] * 4, pages)
    assert (stop, actions) == ("done", 4)


def tabs(n, url="http://127.0.0.1/t", y=0, selected=0, checked="false"):
    """A page with 3 tabs and a box, its controls built again on each read (a new node id, so a new fingerprint)."""
    controls = [{"id": f"e{i + 1}", "node": 10 * n + i, "role": "tab", "label": name, "kind": "click", "value": "",
                 "selected": str(i == selected).lower()} for i, name in enumerate(["Size", "Length", "Width"])]
    box = {"id": "e4", "node": 10 * n + 3, "role": "checkbox", "label": "Remember me", "kind": "click", "value": "on",
           "checked": checked}
    return {"url": url, "scroll": {"y": y, "height": 2000}, "text": "Size Length Width\nRemember me",
            "fingerprint": f"print {n}", "actions": [*controls, box]}


def test_a_box_that_toggles_from_jevs_first_click_is_a_loop():
    # redteam L1 on #219: the page before the first click is read the same way as the pages after it
    stop, _, actions = drive(["Remember me"] * 6, [tabs(n + 1, checked=("true", "false")[n % 2]) for n in range(6)],
                             first=tabs(0))
    assert (stop, actions) == ("loop", 3)


@pytest.mark.parametrize("moved", ["url", "y", "selected"])
def test_a_click_that_moves_only_the_address_the_scroll_or_the_selected_tab_is_no_loop(moved):
    # redteam L1 on #219: the same words and controls, but the page moved on in one of these
    def page(n):
        return tabs(n, **{"url": {"url": f"http://127.0.0.1/t?page={n}"}, "y": {"y": 560 * n},
                          "selected": {"selected": n % 3}}[moved])

    stop, _, actions = drive(["Next"] * 4, [page(n + 1) for n in range(4)], first=page(0))
    assert (stop, actions) == ("done", 4)


def test_a_loop_is_harness_with_the_reason_unless_the_page_still_passes():
    outcome, reason = verdict.classify("loop", [{"check": "page shows 'Saved'", "ok": False, "detail": None}],
                                       has_checks=True)[:2]
    assert outcome == "harness"
    assert reason.startswith("Jev clicked one target again and again")
    # With no checks at all, a loop is still QAJev's trouble: not "unverified"
    assert verdict.classify("loop", [], has_checks=False, stop_detail="'Save' 3 times")[0] == "harness"
