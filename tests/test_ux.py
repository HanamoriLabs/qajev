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


def test_a_goal_not_reached_is_blamed_on_the_guard_or_the_tool_before_the_page():
    # 5 Oct testers: Marketing's goal needed foleyapp.com, which the guard hid (no --host); SideGame1's needed the
    # backquote key, which Jev cannot press. Both were reported as the page's findability. Neither says anything
    # about the page.
    hidden = [{"label": "Open on the web", "why": "off-site link", "match": "my.foleyapp.com"},
              {"label": "Sign out", "why": "danger", "match": "sign out"}]
    goal = "Find where to download Foley for your Mac. Stop when the download page shows."
    assert ux.blocked_by(goal, "stuck", hidden) == (
        "blocked by the guard: it hid 'Open on the web' (off-site link: my.foleyapp.com), which the goal needs; "
        "add --host my.foleyapp.com to let Jev follow it")
    assert ux.blocked_by(goal, "harness", hidden).startswith("blocked by the guard")  # same cause, same words
    assert ux.blocked_by(goal, "pass", hidden) is None and ux.blocked_by(goal, "fail", hidden) is None
    assert ux.blocked_by("Find the price. Stop when it shows.", "stuck", hidden) is None  # nothing it needed
    key = "Open the game's developer menu (the backquote key, `, opens it) and press Play now."
    assert ux.blocked_by(key, "stuck", []) == (
        "the goal needs a key press (backquote); Jev can only click, type, choose and scroll: press it with a "
        "`key` hook before the goal")
    assert ux.blocked_by("Drag the slider to 50. Stop when it shows 50.", "stuck", []).startswith(
        "the goal needs a drag")
    assert ux.blocked_by("Find the price. Stop when $29 shows.", "stuck", []) is None  # a real struggle: the page's


def test_a_key_word_or_a_shared_word_alone_does_not_blame_the_tool_or_the_guard():
    # Orchestrator's review of #35, 5 Oct: a backtick around code, a product's "key" or "tab", or one word shared with
    # a hidden danger control turned the page's real struggle into harness. On FlockTab, keys and tabs are the product.
    page_s = ["Run `tab claude` and find the spend. Stop when the spend shows.",
              "Press Create, then copy the new key. Stop when the key shows.",
              "Tap Settings and find your API key. Stop when it shows.",
              "Hit the tab's Close button. Stop when the tab is gone.",
              "Find out what happens when you hit the tab cap. Stop when the limit shows."]
    for goal in page_s:
        assert ux.blocked_by(goal, "stuck", []) is None, goal
    danger = [{"label": "Delete account", "why": "danger", "match": "Delete"},
              {"label": "Buy Pro plan", "why": "danger", "match": "Buy"}]
    for goal in ["Find your account settings. Stop when they show.",
                 "Find the price of the Pro plan. Stop when it shows."]:
        assert ux.blocked_by(goal, "stuck", danger) is None, goal
    # Still QAJev's own: a danger control the goal asks for by its action, and the keys named as keys.
    assert ux.blocked_by("Delete the test account. Stop when it is gone.", "stuck", danger).startswith(
        "blocked by the guard: it hid 'Delete account' (danger: Delete)")
    assert ux.blocked_by("Buy the Pro plan. Stop when the receipt shows.", "stuck", danger).startswith(
        "blocked by the guard: it hid 'Buy Pro plan'")
    offsite = [{"label": "Get the desktop app", "why": "off-site link", "match": "downloads.example.org"}]
    assert ux.blocked_by("Find where to download the desktop app. Stop when the download page shows.", "stuck",
                         offsite).startswith("blocked by the guard: it hid 'Get the desktop app'")
    fixture = "Open the menu with the backquote key (`) and press Start. Stop when Started shows."
    for goal, key in [(fixture, "backquote"),
                      ("Press the backquote key to open the console. Stop when it shows.", "backquote"),
                      ("Press Escape to close the dialog. Stop when it is closed.", "Escape"),
                      ("Press the Tab key until Pricing has focus. Stop when it does.", "Tab"),
                      ("Press Tab twice, then Enter. Stop when the form is sent.", "Tab"),
                      ("Hold the W key to run. Stop when the player moves.", "a key")]:
        assert ux.blocked_by(goal, "stuck", []) == (
            f"the goal needs a key press ({key}); Jev can only click, type, choose and scroll: press it with a "
            "`key` hook before the goal"), goal
    assert ux.blocked_by("Drag the slider to 50. Stop when it shows 50.", "stuck", []).startswith(
        "the goal needs a drag")


def test_findability_counts_qajevs_recovery_scrolls_apart_from_jevs():
    history = [{"kind": "click", "action": "Play", "url": "http://h/"}]
    notes = ux.struggle(history, [], "stuck", [{"after": "BLOCKED", "scrolled": 2}])
    assert notes[0]["detail"] == ("not reached (stuck) in 1 action(s) over 1 page(s), 0 backtrack(s), 0 scroll(s), "
                                  "0 unsure step(s), QAJev's own recovery scrolls: 2")
    # FlockTab1: Jev said DONE and was right; the tester's expectation failed. That is no findability struggle.
    done = ux.struggle(history, [], "fail", [], jev_done=True)
    assert done[0]["detail"].startswith("Jev finished, but the checks failed in 1 action(s)")


def test_a_goal_the_guard_blocked_is_harness_with_no_struggle_signals(monkeypatch):
    from qajev import runner, suite

    history = [{"kind": "scroll", "action": "Scroll down", "url": "https://h/"}] * 6
    state = {"status": "blocked", "history": history, "decisions": [], "text_calls": [], "elapsed_ms": 900}
    hidden = [{"label": "Open on the web", "why": "off-site link", "match": "my.foleyapp.com"}]

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
        def probe(self, expect): return {"url": "https://h/", "status": 200, "text": [False],
                                         "probe": {"hidden": 1, "hidden_controls": hidden}}

    monkeypatch.setattr(runner, "wait_for_quiet", lambda opts: (True, 1.0))
    monkeypatch.setattr(runner, "drive", lambda *a, **k: ("blocked", "no way forward"))
    s = suite.parse({"scenarios": [{"name": "download", "url": "https://h/", "goal": "Find where to download Foley.",
                                    "expect": {"text": "All downloads"}}]})
    result = runner.run_scenario(Page(), s.scenarios[0], opts=runner.Options(), hosts={"h"}, run_dir=None)
    assert result["outcome"] == "harness"
    assert "blocked by the guard: it hid 'Open on the web'" in result["reason"]
    assert not [n for n in result.get("ux") or [] if n["basis"] == "struggle"]  # the page is not to blame


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
