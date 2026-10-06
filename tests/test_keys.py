"""Key hooks (keys.py): real key names only, a sequence repeated, a key held, all within bounds. test_live.py presses
them in a real page."""

import pytest

from qajev import keys
from qajev.session import HookFailed, Session
from qajev.suite import SuiteError, parse


def test_a_key_hook_is_one_key_or_a_sequence_repeated_or_held():
    assert keys.plan("Escape") == {"keys": ["Escape"], "repeat": 1, "interval_ms": 0, "hold_ms": 0}
    assert keys.plan({"press": ["f", "j"], "repeat": 15, "interval_ms": 30}) == {
        "keys": ["f", "j"], "repeat": 15, "interval_ms": 30, "hold_ms": 0}
    assert keys.plan({"press": "Space", "hold_ms": 1200})["hold_ms"] == 1200
    assert keys.spec("F") == keys.spec("f") == {"key": "f", "code": "KeyF", "windowsVirtualKeyCode": 70, "text": "f"}
    assert keys.spec("Backquote") == keys.spec("`")  # a code works as a name: easier in YAML than a backquote
    assert keys.spec("ArrowLeft") == {"key": "ArrowLeft", "code": "ArrowLeft", "windowsVirtualKeyCode": 37}
    assert keys.spec("Delete") == {"key": "Delete", "code": "Delete", "windowsVirtualKeyCode": 46}  # guarded on prod
    assert keys.spec("7")["code"] == "Digit7"


@pytest.mark.parametrize("value, error", [
    ("cmd+w", "refused: 'cmd+w' is a browser or system shortcut"),  # chords exist, shortcuts never
    ("Meta", "unknown key 'Meta'"),
    ("Control", "unknown key 'Control'"),
    ({"press": "w", "modifiers": 4}, "unknown key option(s) ['modifiers']"),
    ({"press": "f", "hold_ms": 5001}, "hold_ms must be a whole number from 0 to 5000"),
    ({"press": "f", "repeat": 0}, "repeat must be a whole number from 1 to 200"),
    ({"press": "f", "repeat": 201}, "repeat must be a whole number from 1 to 200"),
    ({"press": "f", "repeat": True}, "repeat must be a whole number"),
    ({"press": ["f", "j"], "repeat": 200, "interval_ms": 100}, "at most 30 s (this one: 40.0 s)"),
    ({"press": []}, "press takes 1 to 20 keys"),
])
def test_a_key_hook_outside_the_allow_list_or_its_bounds_is_refused(value, error):
    with pytest.raises(ValueError) as e:
        keys.plan(value)
    assert error in str(e.value)


def test_a_suite_with_a_bad_key_hook_is_refused_before_it_runs():
    with pytest.raises(SuiteError, match=r"scenarios\[0\].before\[0\].key: refused: 'cmd\+q' is a browser or system"):
        parse({"scenarios": [{"name": "a", "url": "http://127.0.0.1:1/", "before": [{"key": "cmd+q"}],
                              "expect": {"text": "x"}}]})
    with pytest.raises(SuiteError, match=r"steps\[0\].key: hold_ms must be"):
        parse({"scenarios": [{"name": "a", "url": "http://127.0.0.1:1/?p={client}", "clients": 2, "state": "1",
                              "steps": [{"client": "p1", "key": {"press": "f", "hold_ms": 9000}}],
                              "expect": {"text": "x"}}]})
    ok = parse({"scenarios": [{"name": "a", "url": "http://127.0.0.1:1/", "expect": {"text": "x"},
                               "before": [{"key": {"press": ["f", "j"], "repeat": 3}}, {"key": "Escape"}]}]})
    assert ok.scenarios[0].before[0] == {"key": {"press": ["f", "j"], "repeat": 3}}


def test_a_press_sends_trusted_key_events_repeats_them_and_holds_the_key_down(monkeypatch):
    log = []
    page = Session.__new__(Session)
    page.call = lambda method, **p: log.append((p["type"], p["key"], p["code"], "text" in p))
    monkeypatch.setattr("qajev.session.time.sleep", lambda s: log.append(("sleep", round(s * 1000))))

    page.press({"press": ["f", "j"], "repeat": 2, "interval_ms": 30})
    assert log == [("keyDown", "f", "KeyF", True), ("keyUp", "f", "KeyF", False), ("sleep", 30),
                   ("keyDown", "j", "KeyJ", True), ("keyUp", "j", "KeyJ", False), ("sleep", 30)] * 2

    log.clear()
    page.press({"press": "Space", "hold_ms": 1500})
    assert log == [("keyDown", " ", "Space", True), ("sleep", 1500), ("keyUp", " ", "Space", False)]

    with pytest.raises(HookFailed, match="refused: 'cmd\\+w' is a browser or system shortcut"):
        page.press("cmd+w")


class FakePage:
    """A page for react hooks: answers each tick from a script, on a clock that only moves when QAJev sleeps."""

    def __init__(self, monkeypatch, answers, tick_s=0.05):
        self.answers, self.log, self.now = list(answers), [], 0.0
        self.page = Session.__new__(Session)
        self.page.call = lambda method, **p: self.log.append((round(self.now, 2), p["type"], p["code"]))
        self.timeouts = []
        self.page.evaluate = lambda expression, timeout_ms=15000: (
            self.timeouts.append(timeout_ms), self.answers.pop(0) if self.answers else {})[1]
        self.page.screenshot = lambda path: (path.parent.mkdir(parents=True, exist_ok=True), path.write_bytes(b"jpg"),
                                             path)[2]
        monkeypatch.setattr("qajev.session.time.monotonic", lambda: self.now)
        monkeypatch.setattr("qajev.session.time.sleep", lambda s: setattr(self, "now", self.now + max(s, tick_s)))


def test_a_react_hook_sends_what_its_policy_returns_and_ends_when_until_holds(monkeypatch, tmp_path):
    f = FakePage(monkeypatch, [{"actions": None}, {"actions": ["ArrowLeft", {"down": "Space"}]},
                               {"actions": [{"shot": "grip flash"}]}, {"actions": {"up": "Space"}}, {"done": True}])
    f.page.shot_dir, f.page.shot_prefix = tmp_path, "arm"
    f.page.react({"js": "policy()", "until": "over()", "every_ms": 50})
    assert f.log == [(0.05, "keyDown", "ArrowLeft"), (0.05, "keyUp", "ArrowLeft"), (0.05, "keyDown", "Space"),
                     (0.15, "keyUp", "Space")]  # pressed, then held two ticks
    assert f.page.react_log == [  # a held key is logged down and up; a frame with what it took (the fake: no time)
        {"down": "Space", "at_s": 0.05},
        {"shot": "grip-flash", "at_s": 0.1, "took_s": 0.0, "path": str(tmp_path / "arm-001-grip-flash.jpg")},
        {"up": "Space", "at_s": 0.15}]


def test_a_react_hook_releases_every_key_and_bounds_its_holds(monkeypatch):
    f = FakePage(monkeypatch, [{"actions": {"down": "s"}}] + [{"actions": None}] * 200, tick_s=0.5)
    f.page.react({"js": "hold()", "for_s": 8, "every_ms": 500})  # no until: runs its time, then ends cleanly
    assert [e for e in f.log if e[1] == "keyUp"][0][0] == 5.0  # held 5 s at most, then released
    assert f.page.react_log == [{"down": "s", "at_s": 0.0}, {"released": "s", "at_s": 5.0, "why": "held 5 s"}]

    f = FakePage(monkeypatch, [{"actions": {"down": "f"}}, {"error": "view is not a function"}])
    with pytest.raises(HookFailed, match="react policy: view is not a function"):
        f.page.react({"js": "bad()"})
    assert f.log[-1][1:] == ("keyUp", "KeyF")  # a policy that throws still lets go of the key


@pytest.mark.parametrize("answer, error", [
    ({"actions": "cmd+q"}, "react: refused: 'cmd+q' is a browser or system shortcut"),
    ({"actions": {"hold": "f"}}, "a key action is a key name, {press: k}, {down: k} or {up: k}"),
    ({"actions": ["f"] * 11}, "returned 11 actions at once"),
])
def test_a_react_policy_is_held_to_the_allow_list(monkeypatch, answer, error):
    f = FakePage(monkeypatch, [answer])
    with pytest.raises(HookFailed, match=error.replace("{", r"\{").replace("}", r"\}").replace("+", r"\+")):
        f.page.react({"js": "p()"})
    assert f.log == []


def test_a_react_hook_that_never_sees_until_fails_and_one_without_it_just_ends(monkeypatch):
    f = FakePage(monkeypatch, [{"actions": None}] * 100, tick_s=0.25)
    with pytest.raises(HookFailed, match=r"react: 'won\(\)' did not hold within 2 s"):
        f.page.react({"js": "p()", "until": "won()", "for_s": 2})
    for bad, error in (({"js": ""}, "react needs js"), ({"js": "p()", "every_ms": 5}, "every_ms must be"),
                       ({"js": "p()", "for_s": 600}, "for_s must be"), ({"js": "p()", "keys": 1}, "unknown react")):
        with pytest.raises(ValueError, match=error):
            keys.react_plan(bad)


def test_the_frames_a_react_hook_kept_reach_the_result_and_both_reports(monkeypatch, tmp_path):
    import time as clock
    from types import SimpleNamespace

    from qajev import report, runner, suite

    class Page:  # runs the react hook by noting what a policy would have kept: a frame and a forced release
        def set_device(self, device): pass
        def arm(self, mode, speech=None): pass
        def check_host(self, url): pass
        def navigate(self, url): self.url = url
        def probe(self, expect): return {"url": self.url, "status": 200, "text": [True], "probe": {}}
        def why_failed(self, url): return None
        def screenshot(self, path): return None

        def run_hook(self, hook, url):
            path = self.shot_dir / f"{self.shot_prefix}-grip-flash.jpg"
            self.react_log += [{"shot": "grip-flash", "at_s": 2.4, "path": str(path)},
                               {"released": "s", "at_s": 7.1, "why": "held 5 s"}]

    monkeypatch.setattr(runner, "wait_for_quiet", lambda opts: (True, 1.0))
    s = suite.parse({"scenarios": [{"name": "arm", "url": "http://127.0.0.1:8765/", "expect": {"text": "Arm"},
                                    "before": [{"react": {"js": "null"}}]}]})
    result = runner.run_scenario(Page(), s.scenarios[0], opts=runner.Options(), hosts={"127.0.0.1"}, run_dir=tmp_path)
    assert result["react"][0]["path"] == "shots/arm-grip-flash.jpg"  # relative to the run, like every screenshot
    ledger = {"usd": 0.0, "usd_typesafe_estimated": 0.0, "usd_text": 0.0, "calls": {"typesafe": 0, "text": 0},
              "tokens": {"typesafe": 0, "text": 0}, "errors": 0, "text_cost_reported": True, "cap_usd": 1.0}
    data = report.build(SimpleNamespace(name="arm"), [{**result, "about": "x"}], [ledger], browser={},
                        started_at=clock.time(), strict=False, interrupted=False, run_dir=tmp_path)
    report.write(tmp_path, data)
    md, html = (tmp_path / "report.md").read_text(), (tmp_path / "report.html").read_text()
    assert "- Frames: [grip-flash](shots/arm-grip-flash.jpg) at 2.4 s" in md
    assert "- Released s at 7.1 s: held 5 s" in md
    assert 'src="shots/arm-grip-flash.jpg"' in html and "grip-flash at 2.4 s" in html


def test_frames_are_capped_per_tick_and_per_hook_and_never_overwrite_each_other(monkeypatch, tmp_path):
    # Orchestrator's review of #32: shots skipped the 10-action cap, and every round's "cue" frame overwrote the last.
    f = FakePage(monkeypatch, [{"actions": [{"shot": "cue"}] * 5}, {"actions": [{"shot": "cue"}]}, {"done": True}])
    f.page.shot_dir, f.page.shot_prefix = tmp_path, "arm"
    f.page.react({"js": "p()", "until": "d()"})
    frames = [e for e in f.page.react_log if "shot" in e]
    assert [e["path"] for e in frames] == [str(tmp_path / n) for n in ("arm-001-cue.jpg", "arm-002-cue.jpg",
                                                                        "arm-003-cue.jpg")]  # 2 in tick 1, 1 in tick 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["arm-001-cue.jpg", "arm-002-cue.jpg", "arm-003-cue.jpg"]
    notes = [e for e in f.page.react_log if "note" in e]
    assert len(notes) == 1 and notes[0]["note"].startswith("frame cap reached (2 a tick, 60 a hook)")

    f = FakePage(monkeypatch, [{"actions": [{"shot": "x"}, {"shot": "y"}]}] * 40 + [{"done": True}])
    f.page.shot_dir, f.page.shot_prefix = tmp_path / "many", "c"
    f.page.react({"js": "p()", "until": "d()"})
    assert len([e for e in f.page.react_log if "shot" in e]) == 60  # the hook's cap: 60 of the 80 asked for
    assert len([e for e in f.page.react_log if "note" in e]) == 1  # said once


def test_a_ticks_evaluate_never_outlasts_the_hook_and_every_held_key_is_released(monkeypatch):
    f = FakePage(monkeypatch, [{"actions": None}] * 100, tick_s=0.5)
    f.page.react({"js": "p()", "every_ms": 50, "for_s": 2})
    assert f.timeouts[0] == 1000 and f.timeouts[-1] <= 500  # max(4 x every_ms, 1 s), then what is left of for_s

    f = FakePage(monkeypatch, [{"actions": [{"down": "a"}, {"down": "b"}, {"down": "c"}]}, {"error": "boom"}])
    ups = []

    def call(method, **p):
        if p["type"] == "keyUp":
            ups.append(p["code"])
            if p["code"] == "KeyA":
                raise RuntimeError("socket closed")

    f.page.call = call
    with pytest.raises(HookFailed, match="boom"):
        f.page.react({"js": "p()"})
    assert ups == ["KeyA", "KeyB", "KeyC"]  # a failed keyUp for a does not leave b and c down


def test_a_chord_carries_its_modifiers_and_a_browser_shortcut_is_refused():
    # verse1, 6 Oct: the Verse town editor opens on Shift+A, which the key hook could not send.
    shift_a = keys.spec("Shift+A")
    assert shift_a["modifiers"] == 8 and shift_a["key"] == "A" and shift_a["text"] == "A" and shift_a["code"] == "KeyA"
    assert keys.spec("ctrl+shift+KeyK")["modifiers"] == 10 and "text" not in keys.spec("Ctrl+k")
    assert keys.spec("Alt+ArrowLeft")["modifiers"] == 1 and keys.spec("Meta+Digit1")["modifiers"] == 4
    assert "modifiers" not in keys.spec("a")  # a plain key stays plain
    for shortcut in ("Meta+q", "Ctrl+w", "Ctrl+R", "Meta+t", "Ctrl+n", "Meta+l", "Ctrl+p"):
        with pytest.raises(ValueError, match="shortcut"):
            keys.spec(shortcut)
    for bad in ("Hyper+a", "Shift+Shift+a", "Shift+", "+a"):
        with pytest.raises(ValueError):
            keys.spec(bad)
    assert keys.plan({"press": ["Shift+A", "Escape"]})["keys"] == ["Shift+A", "Escape"]
    assert keys.actions([{"down": "Shift+w"}, {"up": "Shift+w"}]) == [("down", "Shift+w"), ("up", "Shift+w")]
