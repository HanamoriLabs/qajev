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
# environment it was started with, how many times it has started in its home folder, and the last click. With
# QAJEV_BRIDGE_KEY it reads the key line first and closes on a wrong one, as Twinbox's bridge does; it reports the
# files in its home, with their modes, so a test sees what QAJev wrote there.
FAKE = r'''
import hashlib, json, os, socket, stat, sys
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
key = os.environ.get("QAJEV_BRIDGE_KEY")
if key is not None and f.readline().rstrip("\n") != key:
    conn.close()
    sys.exit(5)
state = {"screen": "home", "home": home, "HOME": os.environ.get("HOME"), "api": os.environ.get("TWINBOX_API"),
         "launches": launches, "argv": sys.argv[1:], "cwd": os.getcwd(), "clicked": None,
         "env_keys": sorted(os.environ), "key": key and {"length": len(key), "hex": all(c in "0123456789abcdef"
                                                                                          for c in key),
                                                          "sha": hashlib.sha256(key.encode()).hexdigest()}}
for line in f:
    req = json.loads(line)
    if home:
        paths = [os.path.join(d, n) for d, _, names in os.walk(home) for n in names if n != "launches"]
        state["home_files"] = {os.path.relpath(p, home): [oct(stat.S_IMODE(os.stat(p).st_mode)), open(p).read()]
                               for p in paths}
    if req["op"] == "observe" and os.environ.get("FAKE_LEAK"):  # an app that logs what it holds
        print("ERROR leaked " + " ".join(c for _, c in state.get("home_files", {}).values()).strip(), flush=True)
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


# ---- bridge_key: a new key each launch, given only to the app (Twinbox's bridge, 14 Oct) ----

def test_with_bridge_key_qajev_gives_the_app_a_new_key_each_launch_and_sends_it_first(project):
    with AppProcess(project, spec(bridge_key=True)) as app:
        first = app.observe()["state"]["key"]  # the stand-in closes its bridge on a wrong key: this call proves it
        assert first["length"] == 64 and first["hex"]
        assert "QAJEV_BRIDGE_KEY" not in json.dumps(app.record) and app._key not in json.dumps(app.record)
        app.relaunch()
        assert app.observe()["state"]["key"]["sha"] != first["sha"]  # new for each launch


def test_without_bridge_key_the_app_gets_no_key():
    assert app_spec({"command": ["x"]})["bridge_key"] is False


def test_the_bridge_key_is_redacted_from_what_qajev_writes(project):
    from qajev.config import redact

    with AppProcess(project, spec(bridge_key=True)) as app:
        assert redact(f"the app printed {app._key}") == "the app printed [redacted]"


# ---- app.files: a seeded TEST account's token, handed to the app under test (the Orchestrator, 14 Oct) ----

TOKEN = "tb_test_token_7f3a9c51"
TOKEN_PATH = ".twinbox/token@twinbox-staging.example"


def fixture(tmp_path, **data):
    data = {"test_account": True, "account": "qa-journey@twinbox.invalid", "token": TOKEN, **data}
    path = tmp_path / "staging-test.json"
    path.write_text(json.dumps(data))
    return path


def files_spec(path, **extra):
    return spec(files={TOKEN_PATH: f"seed:{path}#token"}, test_domains=["twinbox.invalid"], **extra)


def test_app_files_writes_the_token_0600_inside_the_throwaway_home_and_it_goes_with_the_home(project, tmp_path):
    with AppProcess(project, files_spec(fixture(tmp_path))) as app:
        files = app.observe()["state"]["home_files"]
        assert files[TOKEN_PATH] == ["0o600", TOKEN + "\n"]
        assert oct(os.stat(Path(app.user_dir) / "app" / ".twinbox").st_mode & 0o777) == "0o700"
        assert app.file_notes == [f"{TOKEN_PATH}: test token from staging-test.json"]
        user_dir = app.user_dir
        app.relaunch()  # a relaunch keeps the home as the app left it, and writes nothing again
        assert app.observe()["state"]["home_files"][TOKEN_PATH] == ["0o600", TOKEN + "\n"]
    assert not Path(user_dir).exists()


@pytest.mark.parametrize("files, says", [
    ({"/etc/token": "seed:f.json#token"}, "inside the app's home"),
    ({"../token": "seed:f.json#token"}, "inside the app's home"),
    ({".twinbox/../../token": "seed:f.json#token"}, "inside the app's home"),
    ({"./token": "seed:f.json#token"}, "inside the app's home"),
    ({"token": "tb_live_value"}, "only from a test fixture"),
    ({"token": "env:TWINBOX_TOKEN"}, "only from a test fixture"),
    ({"token": "keychain:twinbox/me"}, "only from a test fixture"),
    ({"token": "seed:f.json"}, "seed:FILE#KEY"),
])
def test_a_wrong_app_files_entry_is_refused(files, says):
    with pytest.raises(NativeError, match=says):
        app_spec({"command": ["x"], "home_env": "H", "files": files, "test_domains": ["twinbox.invalid"]})


@pytest.mark.parametrize("value, says", [
    ({"command": ["x"], "files": {"t": "seed:f.json#token"}, "test_domains": ["twinbox.invalid"]}, "needs home_env"),
    ({"command": ["x"], "home_env": "H", "files": {"t": "seed:f.json#token"}}, "app.test_domains"),
    ({"command": ["x"], "home_env": "H", "files": {"t": "seed:f.json#token"}, "test_domains": ["*.invalid"]},
     "app.test_domains"),
    ({"command": ["x"], "test_domains": ["twinbox.invalid"]}, "only with app.files"),
    ({"command": ["x"], "bridge_key": "yes"}, "app.bridge_key"),
])
def test_app_files_needs_a_home_and_test_domains_and_bridge_key_is_true_or_false(value, says):
    with pytest.raises(NativeError, match=says):
        app_spec(value)


@pytest.mark.parametrize("data, says", [
    ({"test_account": False}, "not a test-user fixture"),
    ({"account": "jose@hanamorilabs.com"}, "must name its test account"),
    ({"account": "qa@twinbox.invalid.evil.example"}, "must name its test account"),
    ({"account": None}, "must name its test account"),
    ({"token": 42}, "no text value"),
])
def test_a_fixture_that_is_not_a_named_test_account_in_a_test_domain_is_refused(project, tmp_path, data, says):
    from qajev.app import app_files

    with pytest.raises(NativeError, match=says):
        app_files(app_spec(files_spec(fixture(tmp_path, **data))))


def test_a_symlink_or_a_file_already_there_is_never_written_through(tmp_path):
    from qajev.app import write_private

    home, elsewhere = tmp_path / "home", tmp_path / "elsewhere"
    home.mkdir()
    elsewhere.mkdir()
    (home / ".twinbox").symlink_to(elsewhere)
    with pytest.raises(NativeError, match="symbolic link"):
        write_private(home, TOKEN_PATH, TOKEN)
    assert list(elsewhere.iterdir()) == []
    (home / "token").symlink_to(elsewhere / "stolen")
    with pytest.raises(NativeError, match="cannot write it"):
        write_private(home, "token", TOKEN)
    assert not (elsewhere / "stolen").exists()


def test_the_home_and_its_token_go_on_a_crash_a_failed_start_and_a_stop(project, tmp_path):
    seed = fixture(tmp_path)
    with pytest.raises(RuntimeError), AppProcess(project, files_spec(seed)) as app:  # a crash in the run
        user_dir = app.user_dir
        raise RuntimeError("the run broke")
    assert not Path(user_dir).exists()
    with pytest.raises(KeyboardInterrupt), AppProcess(project, files_spec(seed)) as app:  # qa_stop / SIGTERM
        user_dir = app.user_dir
        raise KeyboardInterrupt
    assert not Path(user_dir).exists()
    app = AppProcess(project, files_spec(seed, env={"FAKE_EXIT": "1"}))
    with pytest.raises(NativeError, match="exited with 3"), app:  # the app dies before its bridge comes up
        pass
    assert app.user_dir and not Path(app.user_dir).exists()


def test_a_home_left_by_a_killed_run_is_reaped_and_a_live_runs_home_is_not(tmp_path):
    import subprocess

    from qajev import native

    (HOME / "tmp").mkdir(parents=True, exist_ok=True)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    dead = HOME / "tmp" / f"qajev-app-{gone.pid}-x1"
    live = HOME / "tmp" / f"qajev-app-{os.getppid()}-x2"
    for folder in (dead, live):
        (folder / "app" / ".twinbox").mkdir(parents=True)
        (folder / "app" / TOKEN_PATH).write_text(TOKEN)
    try:
        assert f"app home {dead.name}" in native.reap()
        assert not dead.exists() and live.exists()
    finally:
        import shutil

        shutil.rmtree(live, ignore_errors=True)
        shutil.rmtree(dead, ignore_errors=True)


def test_a_web_suite_cannot_carry_app_files():
    """Guard 1: app.files exists only on an app launch; a suite of pages refuses an app: block."""
    from qajev.suite import SuiteError, parse

    with pytest.raises(SuiteError, match=r"unknown key\(s\) \['app'\]"):
        parse({"name": "x", "base_url": "https://example.com", "app": {"files": {"t": "seed:f.json#token"}},
               "scenarios": [{"name": "a", "about": "b", "expect": {"text": ["c"]}}]})


def test_play_events_and_logs_never_carry_a_token(tmp_path, monkeypatch):
    from qajev.config import remember_secret

    remember_secret(TOKEN)
    monkeypatch.setattr(cli, "_job_dir", tmp_path)
    emit = cli._printer(cli.build_parser().parse_args(["play", ".", "--quiet"]))
    emit({"event": "step", "doing": f"the app shows {TOKEN}"})
    log = (tmp_path / "events.jsonl").read_text()
    assert TOKEN not in log and "[redacted]" in log


PLAN = """# Twinbox journey, local

The app gets the staging test account's token through app.files, from the test fixture.
"""


def _approved(tmp_path, text):
    from qajev import planfile

    (tmp_path / "plan.md").write_text(f"{text}\nApproved by the Orchestrator 14 Oct 2026 03:00 "
                                      f"sha256:{planfile.digest(text)}\n")


def _files_suite(tmp_path, seed, plan=True):
    return APP_SUITE.replace("  start_wait: 20\n", f"  start_wait: 20\n  test_domains: [twinbox.invalid]\n  files:\n"
                                                    f"    {TOKEN_PATH}: seed:{seed.name}#token\n").replace(
        "steps:\n", "plan: plan.md\nsteps:\n" if plan else "steps:\n")


def test_play_hands_the_token_to_the_app_and_the_report_shows_only_its_file_and_fixture(
        tmp_path, monkeypatch, project, capsys):
    seed = fixture(tmp_path)
    _approved(tmp_path, PLAN)
    suite = _files_suite(tmp_path, seed).replace("state: {api", f"state: {{home_files: {{'{TOKEN_PATH}': "
                                                               f"['0o600', \"{TOKEN}\\n\"]}}, api")
    code, out = _play(tmp_path, monkeypatch, project, suite, capsys)
    assert code == 0 and out["gate"] == "PASS", out
    assert out["browser"]["app_files"] == [f"{TOKEN_PATH}: test token from staging-test.json"]
    assert TOKEN not in json.dumps(out)
    report = next((tmp_path / "runs").glob("*/report.json")).read_text()
    assert TOKEN not in report and TOKEN not in next((tmp_path / "runs").glob("*/report.html")).read_text()


@pytest.mark.parametrize("plan, says", [
    (None, "app.files needs an approved test plan"),
    ("unapproved", "app.files needs an approved test plan"),
    ("silent", "it does not name app.files"),
])
def test_play_refuses_app_files_unless_the_approved_plan_names_it(tmp_path, monkeypatch, project, capsys, plan, says):
    seed = fixture(tmp_path)
    if plan == "unapproved":
        (tmp_path / "plan.md").write_text(PLAN)
    elif plan == "silent":
        _approved(tmp_path, "# Twinbox journey, local\n\nThe app shows Home.\n")
    code, out = _play(tmp_path, monkeypatch, project, _files_suite(tmp_path, seed, plan=plan is not None), capsys)
    assert code == cli.EXIT_CONFIG and says in out["error"]


def test_play_refuses_a_fixture_outside_the_test_domains_at_load(tmp_path, monkeypatch, project, capsys):
    seed = fixture(tmp_path, account="jose@hanamorilabs.com")
    _approved(tmp_path, PLAN)
    code, out = _play(tmp_path, monkeypatch, project, _files_suite(tmp_path, seed), capsys)
    assert code == cli.EXIT_CONFIG and "must name its test account" in out["error"]
    assert not list((tmp_path / "runs").glob("*")) if (tmp_path / "runs").exists() else True


def test_a_token_the_app_leaks_into_its_log_is_redacted_from_the_report(tmp_path, monkeypatch, project, capsys):
    leaked = "tb_test_token_only_this_test_d41c"  # remembered by no other test, so only app.files can redact it
    seed = fixture(tmp_path, token=leaked)
    _approved(tmp_path, PLAN)
    suite = _files_suite(tmp_path, seed).replace('TWINBOX_API: "http://127.0.0.1:8790"}',
                                                 'TWINBOX_API: "http://127.0.0.1:8790", FAKE_LEAK: "1"}')
    code, out = _play(tmp_path, monkeypatch, project, suite, capsys)
    report = next((tmp_path / "runs").glob("*/report.json")).read_text()
    assert "ERROR leaked [redacted]" in report, out  # the app's error line is in the report, its token is not
    assert leaked not in report and leaked not in json.dumps(out)


def test_app_files_itself_refuses_a_fixture_that_is_not_a_test_account(project, tmp_path, monkeypatch):
    """Guard 2 alone: with the vault's own test_account check stubbed out, app.files still refuses the fixture."""
    from qajev import vault
    from qajev.app import app_files

    def lenient(ref):  # a vault that would hand over any fixture's value
        _, file, key = vault.parse(ref)
        return vault.seed_value(json.loads(Path(file).read_text()), key)

    monkeypatch.setattr(vault, "resolve", lenient)
    with pytest.raises(NativeError, match="not a test-user fixture"):
        app_files(app_spec(files_spec(fixture(tmp_path, test_account=False))))
