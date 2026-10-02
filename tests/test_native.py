import json
import os
import shutil
import sys
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


def test_a_decision_still_closing_after_jevs_pick_is_not_picked_again(monkeypatch):
    # I'M HIM! HIRE: the CV stayed up ~1 s after Jev's pick. QAJev asked again, Jev said DONE (it had just hired),
    # DONE matched no offer, so QAJev clicked the first offer a second time and filed "decision not made by Jev".
    from types import SimpleNamespace

    from qajev import session as session_mod

    class Hire(FakeGame):
        def observe(self):
            obs = super().observe()
            if 3 <= self.looks <= 5:  # the same decision, still on screen for three looks
                obs.update(screen="HIRE", decision=True, actions=[{"id": "hire", "label": "HIRE! (Gulpum)"}])
            return obs

    answers = iter([{"choice": "hire", "probabilities": {"hire": 1.0}},
                    {"choice": "DONE", "probabilities": {"DONE": 1.0}}])
    asked = []

    def choose(state, goal, history):
        asked.append(state["title"])
        return {**next(answers), "latency_ms": 5}

    monkeypatch.setattr(session_mod, "load", lambda ledger: SimpleNamespace(model=SimpleNamespace(choose=choose)))
    game = Hire()
    r = native.play_for(game, name="hire", seconds=30, sample=0.01, decide="Hire the recruit.", ledger=_NoLedger())
    assert game.acted == ["hire"] and asked == ["HIRE"]  # one pick, one ask: no second click on a closing screen
    assert not [f for f in r["findings"] if f["kind"] == "decision not made by Jev"]


def test_a_game_problem_fails_the_step_it_happened_in_not_every_step_after(tmp_path):
    # I'M HIM! plan 30, 3 Oct: the watchdog raised one blocker at t=420 s; the adapter lists the last 20 blockers on
    # every look, and every later play step failed "the game reported no problems" quoting that same blocker.
    old = {"kind": "no_stage_progress", "detail": "no new stage for 240 s", "t": 420.0}
    new = {"kind": "soft_lock", "detail": "no progress", "t": 900.0}

    class Watched(FakeGame):
        listed = []

        def observe(self):
            obs = super().observe()
            obs["state"]["game_over"] = False
            obs["problems"] = list(self.listed)
            return obs

    game = Watched()
    steps = [{"name": f"boss {n}", "play": {"seconds": 0.05}} for n in (1, 2, 3)]
    listed_per_step = iter([[old], [old], [old, new]])
    real_play_for = native.play_for

    def play_for(game, **kw):
        Watched.listed = next(listed_per_step)
        return real_play_for(game, sample=0.01, **kw)

    native.play_for, saved = play_for, native.play_for
    try:
        results = native.run_session(game, steps, ledger=_NoLedger(), run_dir=tmp_path, shots=False)
    finally:
        native.play_for = saved
    seen = [[c["ok"] for c in r["checks"] if c["check"].startswith("the game reported")] for r in results]
    assert seen == [[False], [], [False]]  # the blocker fails boss 1 only; boss 3 fails on its own new soft-lock
    assert "soft_lock" in results[2]["checks"][-1]["detail"] or any(
        "soft_lock" in f["kind"] for f in results[2]["findings"])


def test_each_game_decision_is_logged_as_made_with_labels_and_the_runner_up(monkeypatch, tmp_path):
    # qajev top's decisions view (d): what the model chose by the label on screen, how sure, and the runner-up.
    from types import SimpleNamespace

    from qajev import session as session_mod

    class Menu(FakeGame):
        def __init__(self):
            super().__init__()
            self.screen = "MENU"

        def observe(self):
            return {"screen": self.screen, "texts": [], "state": {}, "fps": 60,
                    "actions": [{"id": "play", "label": "PLAY", "kind": "click", "x": 1, "y": 1},
                                {"id": "opts", "label": "OPTIONS", "kind": "click", "x": 1, "y": 2}]}

        def act(self, action):
            self.screen = "GAME" if action["id"] == "play" else self.screen

    monkeypatch.setattr(session_mod, "load", lambda ledger: SimpleNamespace(model=SimpleNamespace(
        choose=lambda state, goal, history: {"choice": "play", "latency_ms": 41,
                                             "probabilities": {"play": 0.9, "opts": 0.08, "DONE": 0.02}})))
    events = []
    r = native.play(Menu(), name="start", goal="Start a game. Stop when playing.", expect={"screen": "GAME"},
                    budget={"actions": 3, "seconds": 10}, ledger=_NoLedger(), run_dir=tmp_path, shots=False,
                    settle=0, emit=events.append)
    assert r["outcome"] == "pass"
    [d] = [e for e in events if e["event"] == "decision"]
    assert {k: d[k] for k in ("scenario", "screen", "chose", "p", "runner_up", "runner_up_p", "options", "ms")} == {
        "scenario": "start", "screen": "MENU", "chose": "PLAY", "p": 0.9, "runner_up": "OPTIONS",
        "runner_up_p": 0.08, "options": 2, "ms": 41}


def test_a_decision_jev_answers_done_says_so_and_does_not_borrow_its_probability(monkeypatch):
    from types import SimpleNamespace

    from qajev import session as session_mod

    monkeypatch.setattr(session_mod, "load", lambda ledger: SimpleNamespace(model=SimpleNamespace(
        choose=lambda state, goal, history: {"choice": "DONE", "probabilities": {"DONE": 0.9}, "latency_ms": 5})))
    events = []
    r = native.play_for(FakeGame(), name="pick", seconds=30, sample=0.01, decide="Pick a card.", ledger=_NoLedger(),
                        emit=events.append, every=0)
    [f] = [f for f in r["findings"] if f["kind"] == "decision not made by Jev"]
    assert "Jev answered DONE" in f["detail"]
    assert "picked Laser (common, LEARN): a beam (first offer: Jev answered DONE)" in [
        e["doing"] for e in events if e["event"] == "step"]
    [pick] = [h for h in r["history"] if h["action"].startswith("Laser")]
    assert pick["probability"] is None  # 0.9 was DONE's, not the card's


def test_strict_decisions_fails_the_step_at_the_first_choice_jev_did_not_make(monkeypatch):
    # Seven Dawns: a route run is branch evidence only when Jev made every choice; the first-offer fallback (an S3
    # finding, the run going on) would quietly change the route.
    from types import SimpleNamespace

    from qajev import session as session_mod

    def jev(choice):
        monkeypatch.setattr(session_mod, "load", lambda ledger: SimpleNamespace(model=SimpleNamespace(
            choose=lambda state, goal, history: {"choice": choice, "probabilities": {choice: 0.9}, "latency_ms": 5})))

    jev("DONE")
    game = FakeGame()
    r = native.play_for(game, name="route", seconds=30, sample=0.01, decide="Pick a card.", ledger=_NoLedger(),
                        strict_decisions=True)
    assert game.acted == [] and r["outcome"] == "fail" and r["stop"] == "strict", r["reason"]
    [check] = [c for c in r["checks"] if c["check"].startswith("Jev made every decision")]
    assert not check["ok"] and "at LEVEL UP" in check["detail"] and "Jev answered DONE" in check["detail"]
    jev("card_0")  # Jev's own pick: the strict step plays on and passes
    r = native.play_for(FakeGame(), name="route", seconds=30, sample=0.01, decide="Pick a card.",
                        ledger=_NoLedger(), strict_decisions=True)
    assert r["outcome"] == "pass" and r["stop"] == "game_over", r["reason"]
    assert all(c["ok"] for c in r["checks"])


def test_a_game_step_with_vision_shows_clef_the_screen_and_judges_its_looks(monkeypatch):
    from types import SimpleNamespace

    from qajev import providers
    from qajev import session as session_mod

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

    class Windowed(FakeGame):
        def shot(self, path):
            path.write_bytes(png)
            return path

    seen = []

    def choose(state, goal, history):  # the decision is sent while the picture hook is on
        seen.append(providers.SEE() if providers.SEE else None)
        return {"choice": "DONE", "probabilities": {"DONE": 0.9}, "latency_ms": 5}

    def post_json(url, key, body):
        assert body["images"][0].startswith("data:image/png;base64,")
        return {"answers": {"look_0": {"type": "noul", "noul": 0.9}}}

    monkeypatch.setattr(session_mod, "load", lambda ledger: SimpleNamespace(
        model=SimpleNamespace(choose=choose, post_json=post_json)))
    r = native.play(Windowed(), name="menu", goal="Open the menu.", expect={"looks": ["the menu is open"]},
                    budget={"actions": 5, "seconds": 10}, ledger=_NoLedger(), vision=True)
    assert seen and seen[0].startswith("data:image/png;base64,") and providers.SEE is None
    assert {"check": "looks: the menu is open", "ok": True, "detail": "p 0.90"} in r["checks"]


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


def test_a_godot_games_saves_are_sent_to_the_throwaway_folder(tmp_path):
    # Godot 4 has no --user-data-dir (the flag QAJev passed was ignored): user:// follows HOME.
    env = native.save_isolation(tmp_path)
    assert env["HOME"] == str(tmp_path / "home") and Path(env["HOME"]).is_dir()
    assert env["QAJEV_USER_DIR"] == str(tmp_path)
    assert all(env[k].startswith(env["HOME"]) for k in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"))


@live
def test_a_godot_game_saves_into_the_throwaway_folder_never_the_players_own(tmp_path, monkeypatch):
    # Seven Dawns: two runs changed the player's real settings and checkpoint files.
    real = Path.home() / "Library/Application Support/Godot/app_userdata/QAJev fixture game"
    if sys.platform != "darwin":
        real = Path.home() / ".local/share/godot/app_userdata/QAJev fixture game"

    def listing():  # every file (logs and shader caches included) with its time
        return {str(p): p.stat().st_mtime for p in real.rglob("*") if p.is_file()} if real.exists() else {}

    before = listing()
    with native.GodotGame(FIXTURE, headless=True) as game:
        start = next(a for a in game.observe()["actions"] if a["label"] == "Start")
        game.act(start)
        deadline = time.monotonic() + 3
        saved = []
        while not saved and time.monotonic() < deadline:
            saved = list(Path(game.user_dir).rglob("settings.cfg"))
            time.sleep(0.1)
        assert saved, "the game's save must land in QAJev's throwaway folder"
    assert listing() == before, "the player's own user:// folder changed"

    # Fail closed: when user:// is not inside the throwaway folder, the game does not run at all.
    real_isolation = native.save_isolation
    elsewhere = real_isolation(tmp_path / "elsewhere")  # user:// lands here, not in the run's folder
    monkeypatch.setattr(native, "save_isolation", lambda user_dir: {**elsewhere, "QAJEV_USER_DIR": str(user_dir)})
    with pytest.raises(native.NativeError, match="refused to start the game"):
        native.GodotGame(FIXTURE, headless=True).start()
    # A near miss: a sibling folder whose name merely starts with the run's ("...-ab" vs "...-abc") is not inside it.
    monkeypatch.setattr(native, "save_isolation", lambda user_dir: {
        **real_isolation(str(user_dir) + "c"), "QAJEV_USER_DIR": str(user_dir)})
    with pytest.raises(native.NativeError, match="refused to start the game"):
        native.GodotGame(FIXTURE, headless=True).start()


def test_a_seed_comes_only_from_the_games_own_qa_folder(tmp_path):
    assert native.seed_folder(FIXTURE, "qa/legacy-save") == (FIXTURE / "qa/legacy-save").resolve()
    for outside in (".", "qa/../", "../site", "qa/missing"):
        with pytest.raises(native.NativeError, match="inside the game's qa/ folder"):
            native.seed_folder(FIXTURE, outside)
    game = tmp_path / "game"
    (game / "qa/sneaky").mkdir(parents=True)
    (tmp_path / "private.cfg").write_text("the player's own")
    (game / "qa/sneaky/settings.cfg").symlink_to(tmp_path / "private.cfg")
    with pytest.raises(native.NativeError, match="leads outside"):
        native.seed_folder(game, "qa/sneaky")


def test_a_relaunch_revives_a_game_an_earlier_step_closed():
    class Closing(FakeGame):
        relaunched = 0

        def relaunch(self):
            self.relaunched += 1

    game = Closing()
    steps = [{"name": "quit", "js": "1"}, {"name": "after quit"}, {"name": "again", "relaunch": True},
             {"name": "continue", "expect": {}}]
    original = native.js_step
    native.js_step = lambda *a, **k: {"name": "quit", "outcome": "pass", "closed": True, "checks": []}
    try:
        results = native.run_session(game, steps, ledger=_NoLedger(), run_dir=None, shots=False)
    finally:
        native.js_step = original
    outcomes = [(r["name"], r["outcome"]) for r in results]
    assert outcomes[1] == ("after quit", "skipped") and outcomes[2] == ("again", "pass")
    assert game.relaunched == 1 and outcomes[3][1] != "skipped"  # the relaunched game is played again
    r = native.relaunch_step(FakeGame(), {}, name="again", ledger=_NoLedger(), run_dir=None, shots=False, emit=None)
    assert r["outcome"] == "harness" and "relaunch needs a Godot game" in r["reason"]


@live
def test_a_seeded_save_is_there_at_launch_and_a_save_survives_a_relaunch():
    # Seven Dawns: save -> relaunch -> Continue, and legacy-save fixtures, all inside the throwaway folder.
    with native.GodotGame(FIXTURE, headless=True, seed="qa/legacy-save") as game:
        assert "Welcome back" in game.observe()["texts"]  # the seed was copied in before the game read it
    with native.GodotGame(FIXTURE, headless=True) as game:
        assert "Main menu" in game.observe()["texts"]
        game.act(next(a for a in game.observe()["actions"] if a["label"] == "Start"))  # saves settings.cfg
        folder = game.user_dir
        deadline = time.monotonic() + 3
        while not list(Path(folder).rglob("settings.cfg")) and time.monotonic() < deadline:
            time.sleep(0.1)
        game.relaunch()
        assert game.user_dir == folder and "Welcome back" in game.observe()["texts"]
    assert not Path(folder).exists()  # the throwaway folder still goes at the end


@live
def test_a_seed_that_cannot_be_copied_keeps_the_game_from_starting(tmp_path):
    game = tmp_path / "game"
    shutil.copytree(FIXTURE, game, ignore=shutil.ignore_patterns(".godot"))
    locked = game / "qa/legacy-save/settings.cfg"
    locked.chmod(0)  # unreadable: the copy fails
    try:
        with pytest.raises(native.NativeError, match="could not seed the save folder"):
            native.GodotGame(game, headless=True, seed="qa/legacy-save").start()
    finally:
        locked.chmod(0o644)


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


def test_an_idle_step_keeps_the_game_running_untouched_then_checks(monkeypatch):
    # I'M HIM! analytics opt-out proof: a release build (no bot, so no `play` step) had to run about 90 s untouched
    # in the same session, because the setting lives in that session's throwaway profile.
    clock = {"t": 0.0}
    monkeypatch.setattr(native.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(native.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))

    class Device:
        errors = []

        def __init__(self, dies_at=None):
            self.dies_at, self.acted = dies_at, []

        def call(self, **request):
            return {"ok": True}

        def observe(self):
            if self.dies_at is not None and clock["t"] >= self.dies_at:
                raise native.NativeError("the game closed its bridge (crashed or quit)")
            return {"screen": "SETTINGS", "texts": ["Settings"], "actions": [], "state": {}}

        def act(self, action):
            self.acted.append(action)

        def shot(self, _path):
            return None

    events, game = [], Device()
    steps = [{"name": "stay a while", "idle": 90, "expect": {"text": ["Settings"]}}]
    [r] = native.run_session(game, steps, ledger=_NoLedger(), run_dir=None, shots=False, emit=events.append)
    assert r["outcome"] == "pass" and clock["t"] >= 90 and game.acted == []
    assert any("idle" in e["doing"] for e in events if e["event"] == "step")

    clock["t"] = 0.0
    steps.append({"name": "after", "expect": {}})
    r, after = native.run_session(Device(dies_at=30), steps, ledger=_NoLedger(), run_dir=None, shots=False)
    assert r["outcome"] == "harness" and r["findings"][0]["kind"] == "game crashed or closed"
    assert after["outcome"] == "skipped"


def test_a_suite_can_allow_quit_in_a_game_and_expect_it_to_close(monkeypatch):
    # I'M HIM! session_end proof: QUIT is hidden from Jev by default (it ends the test), so a suite that wants the
    # game closed says so: allow: [QUIT] and expect: {closed: true}. A clean exit passes; a crash still does not.
    from types import SimpleNamespace

    from qajev import session as session_mod

    def menu(allow=None):
        return {"screen": "TITLE MENU", "texts": ["NEW GAME QUIT"], "state": {},
                "actions": [{"id": "dom:0", "label": "NEW GAME", "kind": "click", "x": 1, "y": 1},
                            {"id": "dom:1", "label": "QUIT", "kind": "click", "x": 1, "y": 2}], **(allow or {})}

    assert [a["label"] for a in native.visible_actions(menu())[0]] == ["NEW GAME"]  # hidden by default

    class Game(native.GodotGame):
        def __init__(self, code, **lists):
            native.GodotGame.__init__(self, Path(__file__).parent / "fixtures" / "godot_game", **lists)
            self.code, self.quit = code, False
            self.proc = SimpleNamespace(poll=lambda: self.code if self.quit else None, wait=lambda timeout=None: 0)

        def call(self, **request):
            if request.get("op") == "act":
                self.quit = request.get("click") == [1, 2]
                return {"ok": True}
            if self.quit:
                raise native.NativeError("the game closed its bridge (crashed or quit)")
            return menu()

    seen = []

    def choose(state, goal, history):
        seen.append([a["label"] for a in state["actions"]])
        return {"choice": "dom:1", "probabilities": {"dom:1": 0.9}, "latency_ms": 5}

    monkeypatch.setattr(session_mod, "load", lambda ledger: SimpleNamespace(model=SimpleNamespace(choose=choose)))
    steps = [{"name": "quit", "goal": "Choose QUIT. Stop when the game has closed.", "expect": {"closed": True}},
             {"name": "after", "expect": {}}]
    clean, after = native.run_session(Game(0, allow=["QUIT"]), steps, ledger=_NoLedger(), run_dir=None, shots=False)
    assert "QUIT" in seen[0]
    assert clean["outcome"] == "pass" and any(c["check"] == "the game closed" and c["ok"] for c in clean["checks"])
    assert after["outcome"] == "skipped" and "closed" in after["reason"]
    [crash] = native.run_session(Game(139, allow=["QUIT"]), steps[:1], ledger=_NoLedger(), run_dir=None,
                                 shots=False)
    assert crash["outcome"] != "pass"


def test_js_and_crash_renderer_steps_for_a_crash_report_proof(monkeypatch):
    # I'M HIM! Sentry proof: a js step throws an uncaught error in the page; crash_renderer kills the page's renderer
    # (Page.crash) while the app runs on, so an idle step after it watches the process, not the dead page.
    from types import SimpleNamespace

    clock = {"t": 0.0}
    monkeypatch.setattr(native.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(native.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))

    class App:
        def __init__(self):
            self.errors, self.ran, self.renderer_gone = [], [], False
            self.proc = SimpleNamespace(poll=lambda: None)

        def observe(self):
            if self.renderer_gone:
                raise native.NativeError("no answer to Runtime.evaluate within 20 s")
            return {"screen": "TITLE", "texts": ["I'M HIM!"], "actions": [], "state": {}}

        def call(self, **_):
            return {"ok": True}

        def run_js(self, expression):
            self.ran.append(expression)
            if "bad(" in expression:
                raise native.NativeError("page script failed: ReferenceError: bad is not defined")
            return 42

        def crash_renderer(self):
            self.renderer_gone = True
            return True

        def shot(self, _path):
            return None

    app = App()
    steps = [{"name": "probe", "js": "setTimeout(() => { throw new Error('probe') }, 0); 42"},
             {"name": "broken script", "js": "bad()"},
             {"name": "crash", "crash_renderer": True},
             {"name": "let it upload", "idle": 10},
             {"name": "look again", "expect": {}}]
    probe, broken, crash, idle, after = native.run_session(app, steps, ledger=_NoLedger(), run_dir=None, shots=False)
    assert probe["outcome"] == "pass" and probe["js_result"] == 42 and app.ran[0].startswith("setTimeout")
    assert broken["outcome"] == "fail" and "bad is not defined" in broken["checks"][0]["detail"]
    assert crash["outcome"] == "pass" and {c["check"] for c in crash["checks"]} == {
        "the renderer crashed", "the app is still running"}
    assert idle["outcome"] == "pass" and clock["t"] >= 10  # watched the process, not the dead page
    assert after["outcome"] == "skipped" and "renderer" in after["reason"]

    class Godot(App):
        run_js = crash_renderer = None  # a Godot game has no page

    [r] = native.run_session(Godot(), steps[:1], ledger=_NoLedger(), run_dir=None, shots=False)
    assert r["outcome"] == "harness" and "Electron" in r["reason"]
