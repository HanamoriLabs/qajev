import os
import time
from pathlib import Path

import pytest

from qajev import electron, native

FIXTURE = Path(__file__).parent / "fixtures" / "electron_app"
ADAPTER = Path(__file__).parent / "fixtures" / "web_adapters" / "fixture.js"


def test_keys_become_chromium_key_events():
    assert electron.key_event("i") == {"key": "i", "code": "KeyI", "windowsVirtualKeyCode": 73, "text": "i"}
    assert electron.key_event("Escape") == {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27}
    assert electron.key_event("Enter")["text"] == "\r" and electron.key_event("Space")["key"] == " "
    assert electron.key_event("3")["code"] == "Digit3"


def test_electron_apps_are_recognised_and_started_with_their_own_binary(tmp_path, monkeypatch):
    assert electron.is_electron(FIXTURE) and electron.is_electron(tmp_path / "Game.app")
    assert not electron.is_electron(Path(__file__).parent / "fixtures" / "godot_game")
    bundle = tmp_path / "Game.app" / "Contents" / "MacOS"
    bundle.mkdir(parents=True)
    (bundle / "Game").write_text("")
    assert electron.command_for(tmp_path / "Game.app") == [str(bundle / "Game")]
    monkeypatch.setenv("QAJEV_ELECTRON", str(bundle / "Game"))
    assert electron.command_for(FIXTURE) == [str(bundle / "Game"), str(FIXTURE.resolve())]
    with pytest.raises(native.NativeError, match="no web adapter"):
        electron.adapter_path("nope")


def test_an_adapter_action_goes_to_the_adapter_in_the_page():
    game = electron.ElectronGame.__new__(electron.ElectronGame)
    sent = []
    game.evaluate = lambda expression: sent.append(expression) or {"ok": True}
    assert game.act({"id": "decide:3:1", "kind": "adapter", "op": "decide", "index": 1}) == {"ok": True}
    assert "__qajevAdapter" in sent[0] and '"index": 1' in sent[0]


live = pytest.mark.skipif(os.environ.get("QAJEV_LIVE") != "1" or not os.environ.get("QAJEV_ELECTRON"),
                          reason="set QAJEV_LIVE=1 and QAJEV_ELECTRON=<an Electron binary> (starts Electron)")


@live
def test_an_electron_game_is_driven_through_its_own_window(monkeypatch):
    with electron.ElectronGame(FIXTURE, adapter=ADAPTER) as game:
        pid, profile = game.proc.pid, Path(game.user_dir)
        obs = game.observe()
        assert obs["screen"] == "MENU" and "Main menu" in obs["texts"][0]
        labels = {a["label"]: a for a in obs["actions"]}
        assert {"Start", "Quit game"} <= set(labels)
        game.act(labels["Start"])
        time.sleep(0.3)
        obs = game.observe()
        assert obs["screen"] == "GAME" and obs["state"]["ticks"] > 0 and obs["fps"] > 0
        game.act(next(a for a in obs["actions"] if a["id"] == "pause"))  # a key, into the page only
        time.sleep(0.2)
        assert game.observe()["screen"] == "PAUSED" and game.errors == []
        game.act({"kind": "key", "key": "b"})
        time.sleep(0.3)
        assert any("fixture boom" in e for e in game.errors)  # uncaught page errors are caught
        assert (profile / "Local State").exists() or any(profile.iterdir())  # the throwaway profile is used
    assert game.proc.poll() is not None and not profile.exists()
    assert not (native.STATE / f"{pid}.json").exists()


def test_a_closed_app_is_a_native_error_not_a_crash():
    # Live: QAJev's fixture quit through its own button and the next look raised websockets' ConnectionClosedError,
    # which ended the whole run with a traceback instead of the step's result.
    from websockets.exceptions import ConnectionClosedError

    class Gone:
        def send(self, _data):
            raise ConnectionClosedError(None, None)

    game = electron.ElectronGame.__new__(electron.ElectronGame)
    game.ws, game.proc, game._pending, game._next = Gone(), None, {}, 0
    game._lock = __import__("threading").Lock()
    with pytest.raises(electron.NativeError, match="closed"):
        game.send("Runtime.evaluate")


def test_punctuation_keys_carry_their_code():
    # I'M HIM! opens its dev menu on e.code === "Backquote" and switches settings tabs on BracketLeft/BracketRight;
    # a one-character key used to go out with an empty code, so the game ignored it.
    assert electron.key_event("`") == {"key": "`", "code": "Backquote", "windowsVirtualKeyCode": 192, "text": "`"}
    assert electron.key_event("Backquote")["code"] == "Backquote" and electron.key_event("Backquote")["key"] == "`"
    assert electron.key_event("[")["code"] == "BracketLeft" and electron.key_event("]")["code"] == "BracketRight"
    assert electron.key_event("a")["code"] == "KeyA" and electron.key_event("5")["code"] == "Digit5"


PS_BEFORE = """  100     1   0:05.00 /Applications/Game.app/Contents/MacOS/Game
  101   100   0:01.00 /Applications/Game.app/Contents/Frameworks/Game Helper (GPU).app/x --type=gpu-process
  102   100   1:00.00 /Applications/Game.app/Contents/Frameworks/Game Helper (Renderer).app/x --type=renderer
  200     1   0:02.00 /Applications/Other.app/x --type=renderer
"""


def test_the_renderers_cpu_time_is_read_from_the_apps_own_process_tree():
    # macOS m:ss.cc, Linux [dd-]hh:mm:ss
    assert electron._cpu_seconds("1:00.50") == 60.5 and electron._cpu_seconds("00:01:02") == 62
    assert electron._cpu_seconds("1-00:00:01") == 86401 and electron._cpu_seconds("bad") is None
    assert electron._renderer_times(PS_BEFORE, 100) == {102: 60.0}  # not the GPU, not another app's renderer


def test_a_pegged_renderer_means_the_game_froze_an_idle_one_means_the_link():
    after_busy = PS_BEFORE.replace("1:00.00", "1:02.85")
    after_idle = PS_BEFORE.replace("1:00.00", "1:00.10")
    for after, frozen in ((after_busy, True), (after_idle, False)):
        outputs = iter([PS_BEFORE, after])
        cpu = electron.renderer_cpu(100, window=3.0, ps=lambda: next(outputs), sleep=lambda _s: None)
        assert (cpu is not None and cpu >= native.FROZEN_CPU) is frozen
    assert electron.renderer_cpu(100, ps=lambda: "", sleep=lambda _s: None) is None  # no renderer found: unknown


def test_a_frozen_game_is_s1_and_a_silent_link_is_s2():
    [froze] = native._lost_game(native.NoAnswer("no answer to Runtime.evaluate within 20 s", cpu=0.97), "fight")
    assert (froze["severity"], froze["kind"]) == ("S1", "game froze") and "97%" in froze["detail"]
    [link] = native._lost_game(native.NoAnswer("no answer to Runtime.evaluate within 20 s", cpu=0.03), "fight")
    assert (link["severity"], link["kind"]) == ("S2", "game stopped answering")
    [unknown] = native._lost_game(native.NoAnswer("no answer to Runtime.evaluate within 20 s"), "fight")
    assert unknown["severity"] == "S2"
