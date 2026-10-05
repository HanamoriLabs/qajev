"""UX notes (ux.py), without a browser: what each measurement turns into, design consistency across pages, the
keyboard walk, and the report's UX section. test_live.py measures a real page with one fault of each kind."""

import time
from types import SimpleNamespace

from qajev import report, ux

CLEAN = {"facts": {"contrast": {"measured": 40, "low": [], "low_count": 0, "skipped": {"image_or_gradient": 0,
                                                                                       "faded": 0, "disabled": 2}},
                   "clipped": [], "clipped_count": 0, "overlaps": [], "dialog": None, "styles": {}},
         "keyboard": {"controls": 5, "reached": 5, "tabs": 6, "unreachable": [], "unreachable_count": 0,
                      "no_focus_look": [], "no_focus_look_count": 0, "stuck": None},
         "zoom": {"wide": False, "width": 640, "viewport": 640, "clipped": [], "clipped_count": 0}}


def test_a_page_that_meets_every_rule_gets_no_ux_notes():
    assert ux.notes(CLEAN) == []  # disabled controls are exempt from contrast (WCAG 1.4.3), so 2 skipped say nothing


def test_each_shortfall_is_a_measured_note_with_its_rule_and_examples():
    m = {"facts": {**CLEAN["facts"],
                   "contrast": {"measured": 40, "low_count": 1, "skipped": {"image_or_gradient": 3},
                                "low": [{"text": "Faint", "ratio": 2.1, "need": 4.5, "fg": "#aaaaaa", "bg": "#ffffff",
                                         "size": 14, "path": "p.faint"}]},
                   "clipped": [{"text": "Cut", "cut": "width", "box": "40x18", "content": "120x18", "path": "div.cut"}],
                   "clipped_count": 1,
                   "dialog": {"path": "div#modal", "named": False, "modal": True, "focus_inside": True,
                              "text": "Subscribe"}},
         "keyboard": {**CLEAN["keyboard"], "unreachable": [{"path": "div.fake", "said": "Buy"}], "unreachable_count": 1,
                      "stuck": {"path": "input#trap", "said": "trap"}},
         "zoom": {"wide": True, "width": 1000, "viewport": 640, "clipped": [], "clipped_count": 0},
         "dialog_escape_closes": False}
    notes = {n["kind"]: n for n in ux.notes(m)}
    assert set(notes) == {"low text contrast", "contrast not measured", "text cut off", "not reachable by keyboard",
                          "keyboard trap", "sideways scroll at 200% zoom", "dialog"}
    assert all(n["basis"] == "measured" and n["rule"] for n in notes.values())
    assert notes["low text contrast"]["samples"] == ["'Faint' 2.1:1 (needs 4.5:1; #aaaaaa on #ffffff, 14px; p.faint)"]
    assert notes["low text contrast"]["rule"].startswith("WCAG 2.2 1.4.3: 4.5:1, or 3:1")
    assert notes["contrast not measured"]["detail"] == "3 text(s) image or gradient (check those by eye)"
    assert notes["dialog"]["detail"] == ("the open dialog 'Subscribe' (div#modal) has no accessible name, "
                                         "does not close on Escape")
    assert "1000 px wide in a 640 px viewport" in notes["sideways scroll at 200% zoom"]["detail"]


def test_design_consistency_names_each_page_that_differs_and_both_values():
    h2 = {"font-family": "Inter", "font-size": "32px", "font-weight": "700", "color": "rgb(0, 0, 0)"}
    page = lambda style, text="Plans": {"h2": [{"style": style, "count": 2, "example": text, "path": "h2"}]}  # noqa
    notes = ux.consistency([("home", page(h2)), ("pricing", page(h2)), ("docs", page(h2)),
                            ("about", page({**h2, "font-size": "28px", "color": "rgb(51, 51, 51)"}, "Team"))])
    assert notes == [{"basis": "measured", "kind": "h2 style differs",
                      "detail": "about: color rgb(51, 51, 51) (vs rgb(0, 0, 0)); font-size 28px (vs 32px); 3 page(s) "
                                "use the other",  # properties in a fixed (alphabetical) order
                      "rule": "the same kind of element looks the same on every page (computed styles compared)",
                      "samples": ["'Team' (h2)"]}]
    assert ux.consistency([("home", page(h2)), ("pricing", page(h2))]) == []  # one style everywhere: nothing to say


class Keys:
    """A page for the keyboard walk: each Tab moves focus to the next id in `order` (None: the body)."""

    def __init__(self, order, looks):
        self.order, self.looks, self.at = list(order), looks, -1

    def press(self, key):
        assert key == "Tab"
        self.at += 1

    def evaluate(self, _expression):
        cid = self.order[self.at % len(self.order)]
        return {"id": cid, "changed": self.looks.get(cid)}


def test_the_keyboard_walk_finds_unreachable_controls_missing_focus_and_a_trap():
    controls = [{"id": f"c{i}", "path": f"button#b{i}", "said": f"B{i}"} for i in range(4)]
    walk = ux.keyboard(Keys(["c0", "c1", None, "c3"], {"c0": True, "c1": False, "c3": True}), controls)
    assert walk["reached"] == 3 and walk["unreachable"] == [{"path": "button#b2", "said": "B2"}]
    assert walk["no_focus_look"] == [{"path": "button#b1", "said": "B1"}] and walk["stuck"] is None
    trap = ux.keyboard(Keys(["c0", "c1", "c1", "c1", "c1"], {"c0": True, "c1": True}), controls)
    assert trap["stuck"] == {"path": "button#b1", "said": "B1"}


def test_a_game_that_takes_tab_is_not_measurable_but_a_page_that_does_is_blocked():
    # SideGame1, 5 Oct: a game binds Tab and cancels its keydown, so focus never moves. That says nothing about the
    # game's keyboard reach; reporting "not reachable" (WCAG 2.1.1) was a false alarm. The Orchestrator: on an ordinary
    # page, cancelling every Tab IS the failure: a keyboard user is locked out.
    class Taken(Keys):
        def __init__(self, game):
            super().__init__([None], {})
            self.game = game

        def evaluate(self, expression):
            if "canvas" in expression:
                return self.game
            return {"id": None, "cancelled": True} if "activeElement" in expression else True

    controls = [{"id": "c0", "path": "button.title-press", "said": "PRESS ANY KEY"}]
    walk = ux.keyboard(Taken(game=True), controls)
    assert walk["tab_taken"] is True and walk["game"] is True and walk["tabs"] == 7
    kinds = {n["kind"]: n for n in ux.notes({"keyboard": walk})}
    assert set(kinds) == {"keyboard not measurable"}
    assert kinds["keyboard not measurable"]["detail"] == ("Tab is taken by the game: all 7 Tab presses were cancelled "
                                                          "and focus never moved")
    page = {n["kind"]: n for n in ux.notes({"keyboard": ux.keyboard(Taken(game=False), controls)})}
    assert set(page) == {"keyboard blocked"}
    assert page["keyboard blocked"]["rule"].startswith("WCAG 2.2 2.1.1")
    assert page["keyboard blocked"]["detail"] == ("the page cancels Tab: all 7 Tab presses were cancelled and focus "
                                                  "never moved, so a keyboard user cannot reach its 1 control(s)")
    # Focus that never moves without the page cancelling Tab is still a real finding.
    still = ux.keyboard(Keys([None], {}), controls)
    assert still["tab_taken"] is False
    assert "not reachable by keyboard" in {n["kind"] for n in ux.notes({"keyboard": still})}


def test_the_report_has_a_ux_section_by_page_and_for_design_consistency(tmp_path):
    ledger = {"usd": 0.0, "usd_typesafe_estimated": 0.0, "usd_text": 0.0, "calls": {"typesafe": 0, "text": 0},
              "tokens": {"typesafe": 0, "text": 0}, "errors": 0, "text_cost_reported": True, "cap_usd": 0.0}
    note = {"basis": "measured", "kind": "low text contrast", "detail": "1 of 40 text(s) below WCAG AA",
            "rule": "WCAG 2.2 1.4.3", "samples": ["'Faint' 2.1:1"]}
    result = {"name": "home", "url": "http://h/", "goal": None, "outcome": "pass", "reason": "loaded", "checks": [],
              "findings": [], "screens": [], "ux": [note], "about": "x"}
    data = report.build(SimpleNamespace(name="smoke h"), [result], [ledger], browser={}, started_at=time.time(),
                        strict=False, interrupted=False, run_dir=tmp_path)
    data["ux"] = {"consistency": {"desktop": [{"basis": "measured", "kind": "h2 style differs",
                                               "detail": "about: font-size 28px (vs 32px)"}]}}
    report.write(tmp_path, data)
    md, html = (tmp_path / "report.md").read_text(), (tmp_path / "report.html").read_text()
    assert "- **low text contrast** (measured): 1 of 40 text(s) below WCAG AA. Rule: WCAG 2.2 1.4.3" in md
    assert "  - 'Faint' 2.1:1" in md and "### Design consistency (desktop)" in md
    assert "They never change the gate" in md and data["gate"] == "PASS"  # a UX note never turns the run red
    assert "<h2>UX</h2>" in html and "h2 style differs" in html and "(measured)" in html


def test_jevs_own_run_says_how_findable_the_goal_was():
    # José, 5 Oct: struggle signals from runs already made, free. Each is evidence, not a verdict.
    history = [{"kind": "click", "action": "Products", "url": "http://h/"},
               {"kind": "scroll", "action": "page", "url": "http://h/products"},
               {"kind": "click", "action": "Home", "url": "http://h/products#top"},
               {"kind": "click", "action": "Plans", "url": "http://h/"},
               *[{"kind": "scroll", "action": "page", "url": "http://h/plans"} for _ in range(3)]]
    screens = [{"step": 1, "next_step": "Products", "p": 0.9, "runner_up": "Plans", "runner_up_p": 0.05},
               {"step": 4, "next_step": "Plans", "p": 0.48, "runner_up": "Pricing", "runner_up_p": 0.41}]
    notes = {n["kind"]: n for n in ux.struggle(history, screens, "pass",
                                               [{"after": "visible", "scrolled": 2, "found": True}])}
    assert notes["findability"]["detail"] == ("reached in 7 action(s) over 4 page(s), 1 backtrack(s), 4 scroll(s), "
                                              "1 unsure step(s), QAJev scrolled 2 more screen(s) to bring it on screen")
    assert notes["unclear choice"]["samples"] == ["step 4: 'Plans' (0.48) vs 'Pricing' (0.41)"]
    assert notes["backtracked"]["samples"] == ["http://h/"]  # home, left for /products, came back
    assert set(notes) == {"findability", "unclear choice", "backtracked", "searched by scrolling", "below the fold"}
    assert all(n["basis"] == "struggle" for n in notes.values())
    smooth = ux.struggle([{"kind": "click", "action": "Pricing", "url": "http://h/"}],
                         [{"step": 1, "next_step": "Pricing", "p": 0.95, "runner_up": "Docs", "runner_up_p": 0.02}],
                         "stuck")
    assert [n["kind"] for n in smooth] == ["findability"]
    assert smooth[0]["detail"].startswith("not reached (stuck) in 1 action(s) over 1 page(s)")
    assert ux.struggle([], [], "pass") == []


def test_a_goal_scenarios_result_carries_its_struggle_signals(monkeypatch):
    from qajev import runner, suite

    history = [{"kind": "click", "action": "Pricing", "url": "http://127.0.0.1:8765/", "page_changed": True}]
    state = {"status": "done", "history": history, "decisions": [], "text_calls": [], "elapsed_ms": 900}

    class Page:
        assists = []

        def __init__(self):
            self.agent = SimpleNamespace(state=state)
            self.ledger = SimpleNamespace(spent=lambda: 0.0)

        def set_device(self, device): pass
        def arm(self, mode, speech=None): pass
        def check_host(self, url): pass
        def navigate(self, url): self.url = url
        def reset_agent(self, task): pass
        def why_failed(self, url): return None
        def probe(self, expect): return {"url": "http://127.0.0.1:8765/pricing", "status": 200, "text": [True],
                                         "probe": {}}

    monkeypatch.setattr(runner, "wait_for_quiet", lambda opts: (True, 1.0))
    monkeypatch.setattr(runner, "drive", lambda *a, **k: ("done", None))
    s = suite.parse({"scenarios": [{"name": "price", "url": "http://127.0.0.1:8765/", "goal": "Find the price.",
                                    "expect": {"text": "$29"}}]})
    result = runner.run_scenario(Page(), s.scenarios[0], opts=runner.Options(), hosts={"127.0.0.1"}, run_dir=None)
    assert result["outcome"] == "pass"
    assert result["ux"] == [{"basis": "struggle", "kind": "findability",
                             "detail": "reached in 1 action(s) over 1 page(s), 0 backtrack(s), 0 scroll(s), "
                                       "0 unsure step(s)",
                             "rule": "Jev's own run: its actions, the pages it went through, and how sure each choice "
                                     "was"}]
