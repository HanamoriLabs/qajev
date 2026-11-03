"""A js hook that throws on purpose fails the test and names the hook; one whose own code broke is harness (3 Nov: a
hook threw "PLAYER_UNLOCK_INPUT_STUCK", the product's own state, and QAJev graded it "browser/daemon error")."""

import os
import re

import pytest
from test_ux_dom_live import ENDPOINT
from test_ux_dom_live import no_model_initialization as no_model_initialization
from test_ux_dom_live import page as page

from qajev import runner, session, suite, verdict

STUCK = "Error: PLAYER_UNLOCK_INPUT_STUCK\n    at <anonymous>:1:7\n    at unlock (game.js:9:3)"
BROKE = "TypeError: Cannot read properties of null (reading 'x')\n    at <anonymous>:1:6"


@pytest.mark.parametrize("description,kind,message,frame", [
    (STUCK, "Error", "PLAYER_UNLOCK_INPUT_STUCK", "at <anonymous>:1:7"),
    (BROKE, "TypeError", "Cannot read properties of null (reading 'x')", "at <anonymous>:1:6"),
    ("Error", "Error", "", None),
    ("STUCK", "", "STUCK", None),
    ("Error: a: b\n    at f (x.js:1:2)", "Error", "a: b", "at f (x.js:1:2)"),
])
def test_a_js_error_gives_its_type_its_message_and_its_first_frame(description, kind, message, frame):
    assert session.thrown(description) == (kind, message, frame)


def _tab(description):
    tab = session.Session.__new__(session.Session)
    tab.evaluate = lambda expression, timeout_ms=0: (_ for _ in ()).throw(session.PageScriptError(description))
    return tab


def test_a_js_hook_that_throws_an_error_is_the_pages_answer():
    with pytest.raises(session.HookThrew) as caught:
        _tab(STUCK)._run_hook({"js": "window.unlock()"}, "http://127.0.0.1:8877/")
    assert str(caught.value.at("before hook 2 of 3")) == (
        "before hook 2 of 3 (`window.unlock()`) threw Error: PLAYER_UNLOCK_INPUT_STUCK (at <anonymous>:1:7)")


@pytest.mark.parametrize("kind", ["TypeError", "ReferenceError", "SyntaxError"])
def test_a_js_hook_whose_own_code_broke_is_harness_and_says_type_message_and_frame(kind):
    with pytest.raises(session.HookBroke) as caught:
        _tab(f"{kind}: oops\n    at <anonymous>:1:6")._run_hook({"js": "x.y"}, "http://127.0.0.1:8877/")
    assert not isinstance(caught.value, session.HookThrew) and isinstance(caught.value, session.HookFailed)
    assert str(caught.value.at("after hook 1 of 1")) == (
        f"after hook 1 of 1 (`x.y`): its own code broke: {kind}: oops (at <anonymous>:1:6)")


def test_a_page_script_that_throws_keeps_its_old_message():
    with pytest.raises(session.PageScriptError, match=r"^page script failed: Error: PLAYER_UNLOCK_INPUT_STUCK"):
        session._evaluated({"exceptionDetails": {"text": "Uncaught", "exception": {"description": STUCK}}})
    with pytest.raises(session.PageScriptError, match=r"^page script failed: STUCK$"):  # a thrown string: its value
        session._evaluated({"exceptionDetails": {"text": "Uncaught",
                                                 "exception": {"type": "string", "value": "STUCK"}}})


class Tab:
    """A stand-in tab whose js hooks throw the description `throws` gives them: QAJev's own hook code turns it into the
    error, as session._run_hook does."""

    def __init__(self, throws):
        self.throws, self.calls = throws, []

    def set_device(self, *_): pass
    def arm(self, *_): pass
    def check_host(self, *_): pass
    def navigate(self, url): self.url = url
    def probe(self, *_): return {"url": self.url, "status": 200, "probe": {}}
    def capture_region_png(self, region): return b""

    def run_hook(self, hook, url):
        self.calls.append(hook)
        kind, value = next(iter(hook.items()))
        if kind == "js" and value in self.throws:
            raise session.js_hook_error(value, self.throws[value])


def _run(tmp_path, monkeypatch, tab, **scenario):
    monkeypatch.setattr(runner, "wait_for_quiet", lambda _: (True, 0))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    item = {"name": "unlock", "about": "the player unlocks", "url": "http://127.0.0.1:8877/",
            "expect": {"url": "127.0.0.1:8877"}, **scenario}
    parsed = suite.parse({"devices": ["desktop"], "scenarios": [item]})
    result = runner.run_scenario(tab, parsed.scenarios[0], opts=runner.Options(shots=False),
                                 hosts={"127.0.0.1:8877"}, run_dir=tmp_path)
    return parsed.scenarios[0], result


def test_a_before_hook_that_throws_an_error_fails_the_test_and_names_the_hook(tmp_path, monkeypatch):
    tab = Tab({"window.unlock()": STUCK})
    _, result = _run(tmp_path, monkeypatch, tab, before=[{"js": "1"}, {"js": "window.unlock()"}],
                     after=[{"js": "window.after = 1"}])
    assert result["outcome"] == "fail" and result["stop"] == "hook_threw", result["reason"]
    assert result["checks"][-1] == {"check": "before hook 2 of 2 ran", "ok": False, "detail": (
        "`window.unlock()` threw Error: PLAYER_UNLOCK_INPUT_STUCK (at <anonymous>:1:7)")}
    assert result["checks"][0]["ok"] is True  # the page checks still judge the page
    assert "before hook 2 of 2 ran" in result["reason"] and "PLAYER_UNLOCK_INPUT_STUCK" in result["reason"]
    assert {"js": "window.after = 1"} not in tab.calls  # nothing after the throw runs, as after a failed hook


def test_a_before_hook_whose_own_code_broke_stays_harness_with_type_message_and_frame(tmp_path, monkeypatch):
    _, result = _run(tmp_path, monkeypatch, Tab({"x.y": BROKE}), before=[{"js": "x.y"}])
    assert result["outcome"] == "harness" and result["stop"] == "hook_failed", result["reason"]
    assert ("before hook 1 of 1 (`x.y`): its own code broke: TypeError: Cannot read properties of null (reading "
            "'x') (at <anonymous>:1:6)") in result["reason"]


def test_an_after_hook_that_throws_fails_the_test_on_its_own_check(tmp_path, monkeypatch):
    _, result = _run(tmp_path, monkeypatch, Tab({"window.check()": "Error: SAVE_LOST"}),
                     after=[{"js": "window.check()"}])
    assert result["outcome"] == "fail", result["reason"]
    assert [c["check"] for c in result["checks"] if not c["ok"]] == ["after hook 1 of 1 ran"]


def test_an_input_do_hook_that_throws_fails_the_test_and_names_its_place(tmp_path, monkeypatch):
    spec = {"name": "aim", "region": {"name": "view", "x": 0, "y": 0, "width": 8, "height": 8},
            "do": [{"js": "window.aim()"}], "min_changed": 0.2}
    tab = Tab({"window.aim()": "Error: AIM_LOCKED"})
    tab.capture_region_png = lambda region: _png()
    _, result = _run(tmp_path, monkeypatch, tab, expect={"input": spec})
    assert result["outcome"] == "fail", result["reason"]
    assert result["checks"][-1]["check"] == "input 'aim', do hook 1 of 1 ran"
    assert "capture" not in result["reason"]  # the page's answer, not a capture's trouble


def test_an_effect_hook_that_throws_fails_the_test_and_is_not_a_capture_error(tmp_path, monkeypatch):
    spec = {"name": "glow", "region": {"name": "subject", "x": 0, "y": 0, "width": 8, "height": 8},
            "off": [{"js": "glow(false)"}], "on": [{"js": "glow(true)"}], "min_changed": 0.01}
    tab = Tab({"glow(true)": "Error: GLOW_SHADER_MISSING"})
    tab.capture_region_png = lambda region: _png()
    _, result = _run(tmp_path, monkeypatch, tab, expect={"effect": spec})
    assert result["outcome"] == "fail", result["reason"]
    assert result["checks"][-1]["check"] == "effect 'glow', on hook 1 of 1 ran"
    assert "capture" not in result["reason"] and "GLOW_SHADER_MISSING" in result["reason"]


def _png():
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(out, format="PNG")
    return out.getvalue()


def test_a_planted_hook_that_throws_is_graded_a_catch(tmp_path, monkeypatch):
    scenario, result = _run(tmp_path, monkeypatch, Tab({"window.unlock()": STUCK}), before=[{"js": "window.unlock()"}],
                            about="plants a hook that throws the product's own error",
                            expect_fail="before hook 1 of 1 ran")
    assert scenario.expect_fail == {"check": "before hook 1 of 1 ran", "words": "before hook 1 of 1 ran"}
    assert verdict.planted(result, scenario.expect_fail["check"])[0] == "pass"
    # the same test with no throw is the fault not caught
    scenario, result = _run(tmp_path, monkeypatch, Tab({}), before=[{"js": "window.unlock()"}],
                            about="plants a hook that throws the product's own error",
                            expect_fail="before hook 1 of 1 ran")
    assert verdict.planted(result, scenario.expect_fail["check"]) == ("fail", verdict.PLANTED_MISSED)


@pytest.mark.parametrize("value,before,says", [
    ("before hook 2 of 1 ran", [{"js": "1"}], "this test has 1 before hook(s)"),
    ("before hook 1 of 2 ran", [{"js": "1"}], "this test has 1 before hook(s)"),
    ("after hook 1 of 1 ran", [{"js": "1"}], "this test has 0 after hook(s)"),
    ("before hook 1 of 1 ran", [{"wait_for": "#x"}], "must name one of the test's js before hooks"),
])
def test_expect_fail_names_only_a_js_hook_the_test_has(value, before, says):
    item = {"name": "t", "about": "plants a hook that throws", "url": "http://127.0.0.1:8877/", "before": before,
            "expect": {"url": "8877"}, "expect_fail": value}
    with pytest.raises(suite.SuiteError, match=re.escape(says)):
        suite.parse({"devices": ["desktop"], "scenarios": [item]})


@pytest.mark.parametrize("error,broke", [("Error: no score", False), ("TypeError: x.toFixed is not a function", True)])
def test_a_js_check_that_throws_on_purpose_still_fails_and_one_that_broke_is_harness(error, broke):
    check = verdict.page_checks({"js": "game.score()"}, {"url": "x", "js": {"error": error}})[0]
    assert check["ok"] is False and bool(check.get("broke")) is broke
    assert verdict.classify("checked", [check], has_checks=True)[0] == ("harness" if broke else "fail")


@pytest.mark.skipif(not ENDPOINT or os.uname().sysname != "Linux", reason="requires approved CPU fixture browser")
def test_browser_a_real_hook_that_throws_and_one_that_breaks(page):
    tab, _ = page
    with pytest.raises(session.HookThrew) as threw:
        tab._run_hook({"js": "throw new Error('PLAYER_UNLOCK_INPUT_STUCK')"}, "about:blank")
    assert threw.value.said.startswith("Error: PLAYER_UNLOCK_INPUT_STUCK (at "), threw.value.said
    with pytest.raises(session.HookBroke) as broke:
        tab._run_hook({"js": "null.x"}, "about:blank")
    assert not isinstance(broke.value, session.HookThrew)
    assert broke.value.said.startswith("TypeError: Cannot read properties of null (reading 'x') (at "), broke.value.said
    with pytest.raises(session.HookThrew) as text:
        tab._run_hook({"js": "throw 'STUCK'"}, "about:blank")
    assert text.value.said == "STUCK"
    with pytest.raises(session.HookThrew) as rejected:  # an async hook: its rejection is the page's answer too
        tab._run_hook({"js": "Promise.reject(new Error('LATE'))"}, "about:blank")
    assert rejected.value.said.startswith("Error: LATE"), rejected.value.said
