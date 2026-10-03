import asyncio
import json

import pytest

from qajev import mcp_server
from qajev.mcp_server import ToolError


def run_folder(tmp_path):
    (tmp_path / "shots").mkdir()
    (tmp_path / "shots" / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    (tmp_path / "shots" / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    (tmp_path / "report.json").write_text(json.dumps({"scenarios": [
        {"name": "home", "outcome": "pass", "reason": "ok", "shot": "shots/a.jpg"},
        {"name": "pricing", "outcome": "fail", "reason": "no price", "shot": "shots/b.jpg"},
        {"name": "sneaky", "outcome": "fail", "reason": "x", "shot": "../../etc/passwd"},
    ]}))
    return tmp_path


def test_qa_screenshot_shows_the_first_failing_scenario_by_default(tmp_path):
    out = asyncio.run(mcp_server.qa_screenshot(str(run_folder(tmp_path))))
    assert out[0] == "pricing: fail - no price" and type(out[1]).__name__ == "Image"
    named = asyncio.run(mcp_server.qa_screenshot(str(tmp_path), scenario="home"))
    assert named[0].startswith("home: pass")


def test_qa_screenshot_never_reads_outside_the_run_folder(tmp_path):
    with pytest.raises(ToolError):
        asyncio.run(mcp_server.qa_screenshot(str(run_folder(tmp_path)), scenario="sneaky"))


def test_qa_play_passes_a_suite_and_the_games_settings_to_the_cli(monkeypatch):
    seen = {}

    async def fake_run(args, ctx, background, title=None):
        seen.update(args=args, title=title)
        return {"gate": "PASS", "run_dir": "/runs/x", "scenarios": []}

    monkeypatch.setattr(mcp_server, "_run_report", fake_run)
    asyncio.run(mcp_server.qa_play("/games/ImHim.app", None, suite="/q/menus.yaml", game_env={"MODE": "demo"},
                                   game_args=["--query=autoplay=1"], headless=False))
    args = seen["args"]
    assert args[:2] == ["play", "/games/ImHim.app"]
    assert args[args.index("--suite") + 1] == "/q/menus.yaml"
    assert args[args.index("--game-env") + 1] == "MODE=demo"
    assert "--game-arg=--query=autoplay=1" in args and "--headless" not in args and "--no-shots" not in args
    asyncio.run(mcp_server.qa_play("/games/ImHim.app", None, shots=False))
    assert "--no-shots" in seen["args"]  # shots are on unless asked off (Electron takes them even headless)


def test_qa_play_can_allow_quit_and_expect_the_game_to_close(monkeypatch):
    # The same opt-in as a suite's allow: [QUIT] + expect: {closed: true}; the job is titled like a CLI run
    # (game and test name), not "play desktop" from the folder.
    from qajev.cli import build_parser

    seen = {}

    async def fake_run(args, ctx, background, title=None):
        seen.update(args=args, title=title)
        return {"gate": "PASS", "run_dir": "/runs/x", "scenarios": []}

    monkeypatch.setattr(mcp_server, "_run_report", fake_run)
    asyncio.run(mcp_server.qa_play("/r/sidescroller/desktop", None, adapter="imhim", goal="Choose QUIT.",
                                   allow=["QUIT"], hide=["CREDITS"], expect_closed=True, name="quit test"))
    parsed = build_parser().parse_args(seen["args"])
    assert parsed.allow == ["QUIT"] and parsed.hide == ["CREDITS"] and parsed.expect_closed
    assert seen["title"] is None  # jobs.describe_argv names it
    assert mcp_server.jobs.describe_argv(seen["args"]) == "play imhim · quit test"


def test_agents_see_exactly_the_qa_tools():
    tools = sorted(t.name for t in asyncio.run(mcp_server.server.list_tools()))
    assert tools == sorted([
        "qa_browser", "qa_check", "qa_doctor", "qa_job", "qa_jobs", "qa_nightly", "qa_play", "qa_project_run",
        "qa_projects", "qa_report", "qa_reports", "qa_rerun", "qa_run_suite", "qa_screenshot", "qa_smoke", "qa_stop"])


def test_website_tools_pass_devices_and_real_devices_to_the_cli(monkeypatch):
    seen = []

    async def fake_report(args, ctx, background, title=None):
        seen.append(args)
        return {"gate": "PASS", "run_dir": "/runs/x", "scenarios": []}

    monkeypatch.setattr(mcp_server, "_run_report", fake_report)
    asyncio.run(mcp_server.qa_check("http://127.0.0.1:8765/", None, devices=["desktop"], real_devices=["ios"]))
    asyncio.run(mcp_server.qa_run_suite(None, suite_path="s.yaml", real_devices=["ios", "android"]))
    asyncio.run(mcp_server.qa_project_run("demo", None, real_devices=["android"]))
    check, suite, project = seen
    assert check[check.index("--devices") + 1] == "desktop"
    assert check[check.index("--real-devices") + 1] == "ios"
    assert suite[suite.index("--real-devices") + 1] == "ios,android"
    assert project[project.index("--real-devices") + 1] == "android"


def test_rerun_never_brings_shell_commands_this_server_does_not_allow(monkeypatch):
    # A terminal run with --allow-commands, rerun from MCP: that permission was the person's, not this server's.
    from qajev import jobs

    real = jobs.rerun_argv
    monkeypatch.setattr(mcp_server, "_allow_commands", False)
    monkeypatch.setattr(jobs, "rerun_argv", lambda job, failed: (["run", "s.yaml", "--allow-commands"], "all of it"))
    monkeypatch.setattr(jobs, "rerun_cwd", lambda job: "/projects/shop")
    started = []
    monkeypatch.setattr(jobs, "start", lambda *a, **k: started.append(a) or {})
    with pytest.raises(ToolError, match="does not allow"):
        asyncio.run(mcp_server.qa_rerun(None, "20261003-055116-afb3"))
    assert not started

    async def fake_run(args, ctx, background, title=None, cwd=None, rerun=None):
        started.append((args, cwd, rerun))
        return {"job": "x"}

    monkeypatch.setattr(mcp_server, "_run_report", fake_run)
    monkeypatch.setattr(jobs, "rerun_argv", lambda job, failed: (["run", "s.yaml"], "all of it"))
    asyncio.run(mcp_server.qa_rerun(None, "20261003-055116-afb3", failed=False, background=True))
    # where the job ran (s.yaml is that folder's), and the new job says what it reruns
    assert started == [(["run", "s.yaml"], "/projects/shop", {"of": "20261003-055116-afb3", "failed": False})]
    monkeypatch.setattr(jobs, "rerun_argv", real)  # a job id that is a path is no job at all
    with pytest.raises(ToolError, match="no job"):
        asyncio.run(mcp_server.qa_rerun(None, "../elsewhere/x"))


def test_qa_play_vision_is_the_default_unless_turned_off(monkeypatch):
    seen = {}

    async def fake_run(args, ctx, background, title=None):
        seen["args"] = args
        return {"gate": "PASS", "run_dir": "/runs/x", "scenarios": []}

    monkeypatch.setattr(mcp_server, "_run_report", fake_run)
    asyncio.run(mcp_server.qa_play("/games/ImHim.app", None))
    assert "--vision" not in seen["args"] and "--no-vision" not in seen["args"]
    asyncio.run(mcp_server.qa_play("/games/ImHim.app", None, vision=False))
    assert "--no-vision" in seen["args"]
    asyncio.run(mcp_server.qa_play("/games/ImHim.app", None, vision=True))
    assert "--vision" in seen["args"]
