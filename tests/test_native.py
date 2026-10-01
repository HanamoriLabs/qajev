import json
import os
import shutil
import time
from pathlib import Path

import pytest

from qajev import native
from qajev.cli import _state_value

FIXTURE = Path(__file__).parent / "fixtures" / "godot_game"


def test_state_values_compare_as_numbers_or_equal():
    assert native.compare(3, ">= 1") and not native.compare(0, ">= 1") and native.compare(False, False)
    assert native.compare(160.0, "== 160") and not native.compare(None, "> 0") and native.compare("GAME", "GAME")
    assert _state_value("game_over=false") == ("game_over", False)
    assert _state_value('kills=>= 3') == ("kills", ">= 3") and _state_value("mode=ranked") == ("mode", "ranked")


def test_checks_come_from_the_game_state_and_errors_fail_the_run():
    obs = {"screen": "GAME", "texts": ["Playing. Core HP 160"], "state": {"kills": 2, "game_over": False},
           "fps": 58}
    checks = native.native_checks({"screen": "GAME", "text": ["Core HP"], "state": {"kills": ">= 1"},
                                   "min_fps": 30}, obs, [])
    assert all(c["ok"] for c in checks) and len(checks) == 5  # + no engine errors
    bad = native.native_checks({"screen": "MENU", "min_fps": 60}, obs, ["SCRIPT ERROR: boom"])
    assert [c["ok"] for c in bad] == [False, False, False]
    assert bad[0]["detail"] == "on GAME" and bad[2]["detail"] == "SCRIPT ERROR: boom"


def test_jev_sees_the_screen_as_text_and_never_a_quit_or_purchase():
    obs = {"screen": "MENU", "texts": ["SUHO main menu"], "state": {},
           "actions": [{"id": "menu_play", "label": "PLAY", "kind": "click", "x": 1, "y": 2},
                       {"id": "menu_quit", "label": "QUIT", "kind": "click", "x": 1, "y": 3},
                       {"id": "buy", "label": "Buy 500 gems", "kind": "click", "x": 1, "y": 4},
                       {"id": "skip_splash", "label": "Skip the studio splash", "kind": "key", "key": "Escape"}]}
    state = native.jev_state(obs, "suho")
    assert [a["label"] for a in state["actions"]] == ["PLAY", "Skip the studio splash"]
    assert [a["kind"] for a in state["actions"]] == ["click", "click"]  # a key too: Jev knows web kinds only
    assert state["text"].startswith("Screen: MENU") and state["url"] == "game://suho/menu"
    assert native.visible_actions(obs)[1] == 2


def test_a_button_named_like_jevs_own_signals_is_renamed_for_it():
    # I'm Him's settings close with a button labelled DONE: to Jev "DONE" means "I have finished", so it stopped.
    obs = {"screen": "SETTINGS", "actions": [{"id": "dom:0", "label": "DONE", "kind": "click", "x": 1, "y": 2},
                                              {"id": "dom:1", "label": "Blocked", "kind": "click", "x": 1, "y": 3}]}
    labels = [a["label"] for a in native.jev_state(obs, "imhim")["actions"]]
    assert labels == ["the 'DONE' button (closes this screen)", "the 'Blocked' button"]


class FakeGame:
    """A game that plays itself: the state advances each look; the third look is a level-up waiting for a pick."""

    errors = []

    def __init__(self):
        self.looks, self.acted = 0, []

    def call(self, **_):
        return {"ok": True, "pilot": True}

    def observe(self):
        self.looks += 1
        obs = {"screen": "GAME", "fps": 60, "perf": {"frame_ms": 2.0, "memory_mb": 50.0}, "texts": [],
               "state": {"tick": self.looks, "kills": self.looks * 3, "level": 1 + self.looks // 3,
                         "game_over": self.looks >= 6}, "actions": []}
        if self.looks == 3:
            obs.update(screen="LEVEL UP", decision=True,
                       actions=[{"id": "card_0", "label": "Laser (common, LEARN): a beam", "kind": "click"}])
        return obs

    def act(self, action):
        self.acted.append(action["id"])

    def shot(self, _path):
        return None


def test_real_time_play_reports_what_it_is_doing_as_it_goes():
    events = []
    r = native.play_for(FakeGame(), name="survive", seconds=30, sample=0.01, emit=events.append, every=0)
    assert r["stop"] == "game_over"
    steps = [e for e in events if e["event"] == "step"]
    assert all(e["scenario"] == "survive" and "at" in e for e in steps)
    doing = [e["doing"] for e in steps]
    assert any(d.startswith("playing ") and "kills 6" in d and "60 fps" in d for d in doing)  # live numbers
    assert "picked Laser (common, LEARN): a beam (first offer: Jev was not asked)" in doing  # the decision


def test_real_time_play_on_a_paused_game_stops_and_says_so():
    class Paused(FakeGame):
        def observe(self):
            obs = super().observe()
            obs.update(screen="SETTINGS", decision=False, actions=[])
            obs["state"].update(tick=1, settings_open=True, game_over=False)
            return obs

    r = native.play_for(Paused(), name="play on", seconds=30, sample=0.01, stall_after=0.1)
    assert r["stop"] == "stale" and r["outcome"] == "harness"  # QAJev's trouble, not a product finding
    assert "stayed paused on SETTINGS" in r["reason"] and r["stats"]["seconds"] < 5
    assert not any(f["kind"] == "soft-lock" for f in r["findings"])


def test_a_soft_lock_fails_the_play_step():
    class Frozen(FakeGame):
        def observe(self):
            obs = super().observe()
            obs.update(screen="GAME", decision=False, actions=[])
            obs["state"].update(tick=7, game_over=False)
            return obs

    r = native.play_for(Frozen(), name="play on", seconds=30, sample=0.01, stall_after=0.1)
    assert r["outcome"] == "fail" and "the game kept running (no soft-lock)" in r["reason"]
    assert [f["kind"] for f in r["findings"]] == ["soft-lock"]


def test_an_overlay_the_pilot_is_handling_is_not_a_soft_lock_and_game_problems_are_findings():
    class Talking(FakeGame):
        def observe(self):
            obs = super().observe()
            obs.update(screen="TALK", decision=False, actions=[])
            obs["state"].update(tick=3, overlay=self.looks < 40, game_over=self.looks >= 40)
            obs["problems"] = [{"kind": "page_error", "detail": "console: [physics] halted", "t": 12.5}] * 2
            return obs

    r = native.play_for(Talking(), name="talk", seconds=30, sample=0.01, stall_after=0.05)
    assert r["stop"] == "game_over" and not any(f["kind"] == "soft-lock" for f in r["findings"])
    assert [f["kind"] for f in r["findings"]] == ["game reported: page_error"]  # once, not per look
    assert r["outcome"] == "fail" and "console: [physics] halted" in r["findings"][0]["detail"]


def test_jev_never_reads_the_step_name_as_where_it_is():
    # A step named "settings close back to the title" put "back to the title" in the URL Jev reads: it said DONE on
    # the settings screen, every time. Jev sees the game's name, never the step's.
    class Game:
        adapter = Path("adapters/imhim.js")
        project = Path("/g/ImHim.app")

    assert native.game_name(Game()) == "imhim"
    Game.adapter = None
    assert native.game_name(Game()) == "imhim"
    assert native.jev_state({"screen": "SETTINGS"}, "imhim")["url"] == "game://imhim/settings"


def test_real_time_play_waits_for_a_game_still_loading_its_pilot():
    class Loading(FakeGame):
        calls = 0

        def call(self, **_):
            self.calls += 1
            return {"ok": True, "pilot": self.calls >= 3}  # the game's bot mounts once the game has loaded

    game = Loading()
    r = native.play_for(game, name="play", seconds=30, sample=0.01, pilot_wait=5, pilot_poll=0.01)
    assert r["stop"] == "game_over" and game.calls >= 3


def test_a_game_without_a_pilot_is_a_harness_stop_not_a_crash():
    class NoPilot(FakeGame):
        def call(self, **_):
            return {"ok": True, "pilot": False}

    r = native.play_for(NoPilot(), name="play", seconds=30, sample=0.01, pilot_wait=0.05, pilot_poll=0.01)
    assert r["outcome"] == "harness" and "no pilot" in r["reason"]
    assert not any("crash" in f["kind"] for f in r["findings"])


def test_the_pilot_is_off_while_jev_works_a_goal_step():
    # I'm Him's bot closes any pause menu it sees: left on, it undid Jev's "pause the game".
    class Recording(FakeGame):
        def __init__(self):
            super().__init__()
            self.pilot_calls = []

        def call(self, **request):
            self.pilot_calls.append(request.get("on"))
            return {"ok": True, "pilot": True}

    game = Recording()
    native.run_session(game, [{"name": "look", "expect": {}}], ledger=_NoLedger(), run_dir=None, shots=False)
    assert game.pilot_calls == [False]


class _NoLedger:
    def spent(self):
        return 0.0


def test_a_game_can_hide_its_online_screens_from_jev():
    # Hypervolley's PLAY ONLINE, RANKED and TOURNAMENTS call its server: a QA run must never open them.
    obs = {"screen": "TITLE", "hide": ["PLAY ONLINE", "ranked", "TOURNAMENTS"],
           "actions": [{"id": "a", "label": "SINGLE PLAYER"}, {"id": "b", "label": "PLAY ONLINE"},
                       {"id": "c", "label": "RANKED"}, {"id": "d", "label": "Tournaments"}]}
    allowed, hidden = native.visible_actions(obs)
    assert [a["label"] for a in allowed] == ["SINGLE PLAYER"] and hidden == 3


def test_adapters_by_name_or_path():
    assert native.adapter_path("suho").name == "suho.gd"
    assert native.adapter_path("hypervolley").name == "hypervolley.gd"
    with pytest.raises(native.NativeError, match="no adapter"):
        native.adapter_path("nope")


live = pytest.mark.skipif(os.environ.get("QAJEV_LIVE") != "1" or not shutil.which(native.GODOT) and
                          not Path(native.GODOT).exists(), reason="set QAJEV_LIVE=1 (starts Godot)")


@live
def test_the_bridge_drives_a_godot_game_without_touching_real_input():
    with native.GodotGame(FIXTURE, headless=True) as game:
        pid = game.proc.pid
        assert json.loads((native.STATE / f"{pid}.json").read_text())["engine"] == "godot"
        obs = game.observe()
        labels = {a["label"]: a for a in obs["actions"]}
        assert {"Start", "Quit game"} <= set(labels) and "Main menu" in obs["texts"]  # found without an adapter
        started = time.perf_counter()
        game.act(labels["Start"])
        time.sleep(0.2)
        assert "Playing" in game.observe()["texts"]
        game.act({"kind": "key", "key": "Escape"})
        deadline = time.monotonic() + 3
        while "Paused" not in game.observe()["texts"] and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "Paused" in game.observe()["texts"]
        assert time.perf_counter() - started < 3 and game.errors == []
    assert game.proc.poll() is not None, "the game must be stopped"
    assert not (native.STATE / f"{pid}.json").exists() and not Path(game.user_dir).exists()


@live
def test_a_button_on_a_scaled_layer_is_clicked_where_it_is_drawn():
    with native.GodotGame(FIXTURE, headless=True) as game:
        scaled = next(a for a in game.observe()["actions"] if a["label"] == "Scaled")
        assert (scaled["x"], scaled["y"]) == (560, 340)  # (200 + 80, 150 + 20) on a 2x layer
        game.act(scaled)
        time.sleep(0.2)
        assert "Scaled clicked" in game.observe()["texts"]


@live
@pytest.mark.skipif(os.environ.get("QAJEV_LIVE_JEV") != "1", reason="set QAJEV_LIVE_JEV=1 (paid Jev calls)")
def test_jev_plays_the_fixture_game_end_to_end(tmp_path):
    import subprocess
    import sys

    env_file = os.environ.get("QAJEV_TEST_ENV_FILE", str(Path.home() / "Development/jev-ultrafast/.env"))
    out = subprocess.run(
        [sys.executable, "-m", "qajev", "play", str(FIXTURE), "--headless", "--name", "start the game",
         "--goal", "Start the game. Stop when you are playing.", "--expect-text", "Playing", "--max-actions", "4",
         "--cost-cap", "0.05", "--out", str(tmp_path), "--env-file", env_file, "--json", "--quiet"],
        capture_output=True, text=True, timeout=180, env={k: v for k, v in os.environ.items() if k != "QAJEV_JOB"},
    )
    report = json.loads(out.stdout)
    (scenario,) = report["scenarios"]
    assert scenario["outcome"] == "pass", scenario
    assert [h["action"] for h in scenario["history"]] == ["Start"]
    assert scenario["guard_hidden"] == 1  # "Quit game" was never offered to Jev
    assert report["browser"]["surface"] == "native" and report["cost"]["usd"] <= 0.05
    assert (Path(report["run_dir"]) / "report.html").exists() and out.returncode == 0


def test_jev_types_into_a_native_text_field_with_its_text_helper(monkeypatch):
    from types import SimpleNamespace

    from qajev import session as session_mod

    class Form:
        errors, typed = [], []

        def observe(self):
            done = bool(self.typed)
            return {"screen": "SEARCH", "texts": ["Searched" if done else "Search settings"], "state": {},
                    "actions": [{"id": "fill:0", "label": "Search settings", "kind": "fill", "x": 10, "y": 20}]}

        def act(self, action):
            self.typed.append(action.get("text"))

        def shot(self, _path):
            return None

    seen = {}

    def choose(state, goal, history):
        seen["kinds"] = [a["kind"] for a in state["actions"]]
        return {"choice": "fill:0", "probabilities": {"fill:0": 0.9}, "latency_ms": 5}

    def field_text(context):
        seen["context"] = context
        return "about phone", {"model": "fake", "latency_ms": 1}

    fake = SimpleNamespace(model=SimpleNamespace(choose=choose, field_text=field_text,
                                                 field_context=lambda goal, action, page, history: {
                                                     "goal": goal, "field": action["label"], "page": page["title"]}))
    monkeypatch.setattr(session_mod, "load", lambda ledger: fake)
    game = Form()
    r = native.play(game, name="search", goal="Search for 'about phone'. Stop when results show.",
                    expect={"text": ["Searched"]}, budget={"actions": 3, "seconds": 30}, ledger=None, settle=0)
    assert seen["kinds"] == ["fill"]  # a text field stays a field to Jev, so it can choose to type
    assert game.typed == ["about phone"] and seen["context"]["field"] == "Search settings"
    assert r["outcome"] == "pass" and r["history"][0]["text"] == "about phone"


def test_a_text_check_counts_what_buttons_say_too():
    obs = {"screen": "SETTINGS", "texts": ["Settings"], "actions": [{"id": "a", "label": "General", "kind": "click"}]}
    (check, _errors) = native.native_checks({"text": ["General"]}, obs, [])
    assert check["ok"], check


def test_a_failed_step_skips_the_rest_only_if_the_device_is_really_gone():
    class Device:
        errors, alive = [], True

        def call(self, **_):
            return {"ok": True}

        def observe(self):
            if not self.alive:
                raise native.NativeError("lost the game")
            return {"screen": "HOME", "texts": ["Home"], "actions": [], "state": {}}

        def shot(self, _path):
            return None

    class Ledger:
        def spent(self):
            return 0.0

    broken = {"name": "a", "play": {"seconds": 1}}  # its device call fails: the step ends harness
    device = Device()
    device.call = lambda **_: (_ for _ in ()).throw(native.NativeError("tap failed"))  # the step itself fails
    steps = [broken, {"name": "b", "expect": {"text": ["Home"]}}]
    results = native.run_session(device, steps, ledger=Ledger(), run_dir=None, shots=False)
    assert [r["outcome"] for r in results] == ["harness", "pass"]  # the device still answers: carry on
    device.alive = False
    results = native.run_session(device, steps, ledger=Ledger(), run_dir=None, shots=False)
    assert [r["outcome"] for r in results] == ["harness", "skipped"]


def test_steps_after_an_app_that_would_not_open_are_skipped_until_the_next_open():
    class Device:
        errors = []

        def call(self, **request):
            if request.get("target") == "missing.app":
                raise native.NativeError("missing.app is not installed")
            return {"ok": True}

        def observe(self):
            return {"screen": "HOME", "texts": ["Home"], "actions": [], "state": {}}

        def shot(self, _path):
            return None

    class Ledger:
        def spent(self):
            return 0.0

    steps = [{"name": "open it", "open": "missing.app"}, {"name": "use it", "expect": {"text": ["Home"]}},
             {"name": "back to settings", "open": "settings.app", "expect": {"text": ["Home"]}}]
    results = native.run_session(Device(), steps, ledger=Ledger(), run_dir=None, shots=False)
    assert [r["outcome"] for r in results] == ["harness", "skipped", "pass"]
    assert "missing.app" in results[1]["reason"]
