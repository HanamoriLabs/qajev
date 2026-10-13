"""A native app with a QAJev bridge, started from a play suite's `app:` (Twinbox Desktop, 13 Oct)."""

import json
import os
import sys
from pathlib import Path

import pytest

from qajev import cli
from qajev.app import AppProcess, app_spec
from qajev.cli import main
from qajev.config import HOME
from qajev.native import NativeError

# A stand-in app: it speaks the Godot bridge's protocol on QAJEV_BRIDGE_PORT and reports, in its state, the
# environment it was started with, how many times it has started in its home folder, and the last click.
FAKE = r'''
import json, os, socket, sys
home = os.environ.get("TWINBOX_DESKTOP_HOME")
if os.environ.get("FAKE_EXIT"):
    print("cannot start: no display", flush=True)
    sys.exit(3)
launches = 1
if home:
    mark = os.path.join(home, "launches")
    launches = int(open(mark).read()) + 1 if os.path.exists(mark) else 1
    open(mark, "w").write(str(launches))
for line in os.environ.get("FAKE_LOG", "").split("|"):
    if line:
        print(line, flush=True)
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", int(os.environ["QAJEV_BRIDGE_PORT"])))
srv.listen(1)
conn, _ = srv.accept()
f = conn.makefile("rw")
state = {"screen": "home", "home": home, "HOME": os.environ.get("HOME"), "api": os.environ.get("TWINBOX_API"),
         "launches": launches, "argv": sys.argv[1:], "cwd": os.getcwd(), "clicked": None,
         "env_keys": sorted(os.environ)}
for line in f:
    req = json.loads(line)
    if req["op"] == "observe":
        out = {"ok": True, "screen": state["screen"], "texts": ["Not signed in"],
               "actions": [{"id": "new", "label": "New box", "kind": "click", "x": 40, "y": 20}], "state": state}
    elif req["op"] == "act":
        state["clicked"] = req.get("click") or req.get("key")
        out = {"ok": True}
    elif req["op"] == "shot":
        open(req["path"], "wb").write(b"\x89PNG\r\n\x1a\n")
        out = {"ok": True}
    elif req["op"] == "quit":
        f.write(json.dumps({"ok": True}) + "\n"); f.flush()
        sys.exit(0)
    else:
        out = {"ok": False, "error": "unknown op"}
    f.write(json.dumps(out) + "\n"); f.flush()
'''


@pytest.fixture
def project(tmp_path):
    """An app's project folder named like a macOS bundle, so a wrong guess would take it for Electron."""
    root = tmp_path / "Twinbox Desktop.app"
    (root / "bin").mkdir(parents=True)
    exe = root / "bin" / "fake-app"
    exe.write_text(f"#!{sys.executable}\n{FAKE}")
    exe.chmod(0o755)
    return root


def spec(**extra):
    return {"command": ["bin/fake-app", "--flag"], "env": {"TWINBOX_API": "http://127.0.0.1:8790"},
            "home_env": "TWINBOX_DESKTOP_HOME", "start_wait": 20, **extra}


def test_the_app_starts_from_its_command_in_a_throwaway_home_and_its_bridge_drives_it(project):
    with AppProcess(project, spec(), args=["--more"]) as app:
        state = app.observe()["state"]
        home = Path(state["home"])
        assert home.is_dir() and home.is_relative_to(HOME / "tmp")  # never the person's real folder
        assert Path(state["HOME"]).is_relative_to(app.user_dir) and state["HOME"] != os.environ.get("HOME")
        assert state["api"] == "http://127.0.0.1:8790" and state["argv"] == ["--flag", "--more"]
        assert Path(state["cwd"]).resolve() == project.resolve()
        action = app.observe()["actions"][0]
        app.act(action)
        assert app.observe()["state"]["clicked"] == [40, 20]
        shot = app.shot(Path(app.user_dir).parent / "shot.png")
        assert shot and shot.read_bytes().startswith(b"\x89PNG")
        assert app.record["engine"] == "app" and app.engine == "app"
        user_dir = app.user_dir
    assert not Path(user_dir).exists()  # removed when the run ends
    assert app.proc.returncode == 0  # it was asked to quit and did


def test_no_secret_in_qajevs_environment_reaches_the_app_only_the_allowlist_does(project, monkeypatch):
    for name in ("TWINBOX_TOKEN", "AWS_SECRET_ACCESS_KEY", "STRIPE_SECRET_KEY", "OPENROUTER_API_KEY", "GH_TOKEN"):
        monkeypatch.setenv(name, "secret-value")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    with AppProcess(project, spec()) as app:
        keys = set(app.observe()["state"]["env_keys"])
    assert not keys & {"TWINBOX_TOKEN", "AWS_SECRET_ACCESS_KEY", "STRIPE_SECRET_KEY", "OPENROUTER_API_KEY", "GH_TOKEN"}
    assert {"PATH", "LC_ALL", "HOME", "TWINBOX_API", "TWINBOX_DESKTOP_HOME", "QAJEV_BRIDGE_PORT"} <= keys
    allowed = {"PATH", "LANG", "TERM", "TMPDIR", "USER", "LOGNAME", "SHELL", "DISPLAY", "WAYLAND_DISPLAY",
               "XDG_RUNTIME_DIR", "XAUTHORITY", "TWINBOX_API", "TWINBOX_DESKTOP_HOME", "HOME", "XDG_DATA_HOME",
               "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "QAJEV_USER_DIR", "QAJEV_BRIDGE_PORT", "QAJEV_WINDOW",
               "__CF_USER_TEXT_ENCODING", "__CFBundleIdentifier", "PWD", "SHLVL", "_"}  # last 5: macOS or a shell
    assert {k for k in keys if not k.startswith("LC_")} <= allowed, keys - allowed


@pytest.mark.parametrize("name", ["HOME", "XDG_CONFIG_HOME", "QAJEV_BRIDGE_PORT", "QAJEV_USER_DIR"])
def test_app_env_may_not_set_the_throwaway_home_or_the_bridge(project, name):
    with pytest.raises(NativeError, match=f"QAJev sets {name} itself"):
        app_spec({"command": ["x"], "env": {name: "/Users/someone"}})
    with pytest.raises(NativeError, match=f"--game-env: QAJev sets {name} itself"):
        AppProcess(project, spec(), env={name: "/Users/someone"})


def test_the_throwaway_home_wins_over_any_env_that_reached_the_app(project):
    app = AppProcess(project, spec())
    app.env["HOME"] = "/Users/someone"  # past the refusals, as a later change could let through
    with app:
        assert app.observe()["state"]["HOME"] != "/Users/someone"


def test_a_relaunch_keeps_the_apps_home_so_a_saved_setting_survives_a_restart(project):
    with AppProcess(project, spec()) as app:
        assert app.observe()["state"]["launches"] == 1
        app.relaunch()
        assert app.observe()["state"]["launches"] == 2


def test_error_lines_and_panics_count_as_the_apps_errors_and_a_mention_of_error_does_not(project):
    log = ("2026-10-13T23:00:00Z ERROR twinbox_desktop::api: start failed|"
           "thread 'main' panicked at src/stream.rs:12:5|"
           "2026-10-13T23:00:01Z  INFO showing the error notice to the person")
    with AppProcess(project, spec(env={"FAKE_LOG": log})) as app:
        app.observe()
        assert len(app.errors) == 2
        assert app.errors[0].endswith("ERROR twinbox_desktop::api: start failed")
        assert "panicked at" in app.errors[1]


def test_an_app_that_exits_before_its_bridge_answers_says_why(project):
    with pytest.raises(NativeError, match=r"the app exited with 3 before its bridge came up: cannot start: no display"):
        AppProcess(project, spec(env={"FAKE_EXIT": "1"})).start()


@pytest.mark.parametrize("value, says", [
    ("bin/fake-app", "must be a mapping"),
    ({}, "app.command"),
    ({"command": []}, "app.command"),
    ({"command": ["x"], "adapter": "suho"}, "unknown key"),
    ({"command": ["x"], "env": {"BAD NAME": "1"}}, "app.env"),
    ({"command": ["x"], "home_env": "A-B"}, "not an environment variable name"),
    ({"command": ["x"], "home_env": "H", "env": {"H": "/Users/me/.twinbox"}}, "do not also set it"),
    ({"command": ["x"], "start_wait": 0}, "app.start_wait"),
    ({"command": ["x"], "start_wait": "soon"}, "app.start_wait"),
])
def test_a_wrong_app_spec_is_refused_naming_what_is_wrong(value, says):
    with pytest.raises(NativeError, match=says):
        app_spec(value)


def test_a_command_with_no_program_is_refused_before_anything_starts(project):
    with pytest.raises(NativeError, match="no program at .*bin/missing"):
        AppProcess(project, spec(command=["bin/missing"]))


def _play(tmp_path, monkeypatch, project, suite_text, capsys):
    monkeypatch.setattr(cli, "_wait_for_quiet", lambda args: None)
    suite = tmp_path / "app.yaml"
    suite.write_text(suite_text)
    env = tmp_path / ".env"
    env.write_text("")
    code = main(["play", str(project), "--suite", str(suite), "--json", "--env-file", str(env),
                 "--out", str(tmp_path / "runs"), "--no-shots"])
    out = capsys.readouterr().out.strip()
    lines = out.splitlines()  # a finished run prints indented JSON; a refusal prints one line
    start = max(i for i, line in enumerate(lines) if line.startswith("{"))
    return code, json.loads("\n".join(lines[start:]))


APP_SUITE = """name: app home
about: The app opens on Home, signed out, with its own settings.
app:
  command: [bin/fake-app]
  home_env: TWINBOX_DESKTOP_HOME
  env: {TWINBOX_API: "http://127.0.0.1:8790"}
  start_wait: 20
steps:
  - name: Home shows signed out
    about: With no token the app says it is not signed in and reads the stand-in API.
    expect:
      screen: home
      text: ["Not signed in"]
      state: {api: "http://127.0.0.1:8790", launches: 1}
"""


def test_qajev_play_runs_an_app_suite_through_its_bridge_and_app_wins_over_the_electron_guess(
        tmp_path, monkeypatch, project, capsys):
    code, out = _play(tmp_path, monkeypatch, project, APP_SUITE, capsys)
    assert code == 0, out
    assert out["gate"] == "PASS"
    assert out["browser"]["engine"] == "app"


def test_a_godot_key_in_an_app_suite_is_refused_at_load(tmp_path, monkeypatch, project, capsys):
    code, out = _play(tmp_path, monkeypatch, project, APP_SUITE.replace("app:\n", "seed: qa/saves\napp:\n"), capsys)
    assert code == cli.EXIT_CONFIG
    assert "app: starts the app from its own command; seed is for a Godot or Electron game" in out["error"]


def test_a_wrong_app_key_is_refused_at_load(tmp_path, monkeypatch, project, capsys):
    code, out = _play(tmp_path, monkeypatch, project, APP_SUITE.replace("  start_wait: 20\n", "  start_wait: 0\n"),
                      capsys)
    assert code == cli.EXIT_CONFIG and "app.start_wait" in out["error"]
