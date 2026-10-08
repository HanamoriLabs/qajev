"""Muted by default (José, 8 Oct 2026): QAJev's Chrome and the games it starts make no sound unless the test asks for it
(--audio, `audio: true` in a suite or project, audio=True on an MCP tool). Muted, a page still plays its media and
Web Audio (only the output is silent), so a check that a sound plays keeps working. No real Chrome or game runs."""

import asyncio
from pathlib import Path

import pytest

from qajev import chrome, electron, mcp_server, native, project, runner
from qajev import suite as suite_mod
from qajev.cli import _options, build_parser

FIXTURES = Path(__file__).parent / "fixtures"


class FakeProc:
    pid, returncode = 4242, None

    def poll(self):
        return None


@pytest.fixture
def launch(monkeypatch, tmp_path):
    """chrome._launch with a pretend Chrome; returns (start, spawned argv list)."""
    spawned = []
    monkeypatch.setattr(chrome, "STATE", tmp_path / "state")
    monkeypatch.setattr(chrome, "binary", lambda: "/usr/bin/chromium")
    monkeypatch.setattr(chrome, "version", lambda url, timeout=1.0: {"Browser": "Chrome"})
    monkeypatch.setattr(chrome.sys, "platform", "linux")
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda argv, **_kw: spawned.append(argv) or FakeProc())

    def start(*, headless=True, audio=False):
        return chrome._launch("default", tmp_path / "profile", 9351, headless, False, True, 2, audio=audio)

    return start, spawned


def test_qajevs_chrome_is_muted_unless_the_test_asks_for_sound(launch):
    start, spawned = launch
    muted = start()
    start(headless=False)
    assert all("--mute-audio" in argv for argv in spawned) and muted["audio"] is False
    spawned.clear()
    loud = start(audio=True)
    assert "--mute-audio" not in spawned[0] and loud["audio"] is True
    assert chrome.run_record(loud)["audio"] is True and chrome.run_record(muted)["audio"] is False


def test_a_test_that_needs_sound_never_reuses_a_muted_chrome(monkeypatch):
    running = {"alive": True, "headless": False, "port": 9351, "ephemeral": False, "audio": False}
    monkeypatch.setattr(chrome, "status", lambda profile=None: [dict(running)])
    with pytest.raises(chrome.ChromeError, match="muted"):
        chrome.start("signed-in", audio=True)
    assert chrome.start("signed-in")["port"] == 9351  # a muted test reuses it as before
    # A Chrome an older QAJev started played sound: reused by a muted test, its report says sound was on
    running.pop("audio")
    assert chrome.run_record({**chrome.start("signed-in"), "cdp_url": "x", "profile": "p"})["audio"] is True


def test_the_cli_and_its_runs_ask_for_sound_with_audio():
    parser = build_parser()
    for command in (["check", "https://example.com", "--goal", "g"], ["run", "suite.yaml"],
                    ["smoke", "https://example.com"]):
        assert _options(parser.parse_args(command)).audio is False
        assert _options(parser.parse_args([*command, "--audio"])).audio is True
    assert parser.parse_args(["play", "game", "--audio"]).audio is True
    assert parser.parse_args(["browser", "start", "--audio"]).audio is True


def test_a_suite_or_a_project_asks_for_sound_with_audio_true(tmp_path):
    base = "name: s\nbase_url: https://example.com\nscenarios:\n  - {name: a, about: x, goal: y, expect: {text: z}}\n"
    assert suite_mod.loads(base).audio is False
    assert suite_mod.loads("audio: true\n" + base).audio is True
    with pytest.raises(suite_mod.SuiteError, match="audio"):
        suite_mod.loads("audio: loud\n" + base)
    (tmp_path / ".qajev").mkdir()
    (tmp_path / ".qajev" / "project.toml").write_text(
        'name = "p"\naudio = true\n[env.prod]\nbase_url = "https://example.com"\n'
        '[[objective]]\nname = "o"\ngoal = "g"\nabout = "a"\nexpect = {text = ["t"]}\n')
    assert project.suite_data(project.load(str(tmp_path)))["audio"] is True


def test_the_run_starts_chrome_with_the_sound_the_suite_or_the_cli_asked_for(monkeypatch, tmp_path):
    asked = []

    class Started(Exception):
        pass

    def fake_start(profile, **kw):
        asked.append(kw.get("audio"))
        raise Started

    monkeypatch.setattr(runner.chrome, "start", fake_start)
    base = "name: s\nbase_url: https://example.com\nscenarios:\n  - {name: a, about: x, goal: y, expect: {text: z}}\n"
    for text, cli_audio in ((base, False), ("audio: true\n" + base, False), (base, True)):
        with pytest.raises(Started):
            runner.run(suite_mod.loads(text), runner.Options(load_high=None, out_dir=tmp_path, audio=cli_audio))
    assert asked == [False, True, True]


def test_electron_and_godot_games_start_silent_unless_the_test_asks_for_sound(monkeypatch, tmp_path):
    started = []

    class Stop(Exception):
        pass

    def fake_popen(argv, **_kw):
        started.append(argv)
        raise Stop

    monkeypatch.setattr(native, "HOME", tmp_path)
    monkeypatch.setattr(native, "STATE", tmp_path / "state")
    monkeypatch.setattr(electron, "HOME", tmp_path)
    monkeypatch.setattr(electron, "STATE", tmp_path / "state")
    monkeypatch.setenv("QAJEV_ELECTRON", "/bin/echo")
    monkeypatch.setattr(electron.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(native.subprocess, "Popen", fake_popen)
    for audio in (False, True):
        for game in (electron.ElectronGame(FIXTURES / "electron_app", audio=audio),
                     native.GodotGame(FIXTURES / "godot_game", headless=True, audio=audio)):
            with pytest.raises(Stop):
                game.start()
    electron_muted, godot_muted, electron_loud, godot_loud = started
    assert "--mute-audio" in electron_muted and "--mute-audio" not in electron_loud
    i = godot_muted.index("--audio-driver")
    assert godot_muted[i + 1] == "Dummy" and "--audio-driver" not in godot_loud


def test_mcp_tools_ask_for_sound_only_when_the_agent_says_so(monkeypatch):
    seen = []

    async def fake_run(args, ctx, background, title=None):
        seen.append(args)
        return {"gate": "PASS", "run_dir": "/runs/x", "scenarios": []}

    monkeypatch.setattr(mcp_server, "_run_report", fake_run)
    goal = "Look at the page. Stop when it is shown."
    asyncio.run(mcp_server.qa_check("https://example.com", None, goal=goal, expect_text=["x"]))
    asyncio.run(mcp_server.qa_check("https://example.com", None, goal=goal, expect_text=["x"], audio=True))
    asyncio.run(mcp_server.qa_smoke("https://example.com", None, audio=True))
    asyncio.run(mcp_server.qa_play("/games/g.app", None, audio=True))
    assert "--audio" not in seen[0] and all("--audio" in args for args in seen[1:])


def test_the_report_says_whether_the_run_had_sound():
    from qajev import report_html

    assert report_html.sound({"audio": False}) == "muted (QAJev's default)"
    assert report_html.sound({"audio": True}) == "on (the test asked for it)"
    assert report_html.sound({"cdp_url": "x", "managed": False}) is None  # an attached Chrome: not QAJev's to say
