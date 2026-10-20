"""Final reports must include owned native output, even when its reader finishes during close."""

import contextlib
import json
import sys
import threading
import time

import pytest

from qajev import app, cli, config, native, providers, vision

BRIDGE = r"""
import json, os, socket, sys
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", int(os.environ["QAJEV_BRIDGE_PORT"])))
server.listen(1)
connection, _ = server.accept()
stream = connection.makefile("rw")
for line in stream:
    request = json.loads(line)
    if request["op"] == "quit":
        if os.environ.get("BAD_OUTPUT"):
            sys.stdout.buffer.write(b"\xffprivate undecodable output\n")
            sys.stdout.buffer.flush()
        elif os.environ.get("FINAL_ERROR"):
            print("ERROR " + os.environ["FINAL_ERROR"], flush=True)
        stream.write('{"ok":true}\n'); stream.flush()
        break
    if request["op"] == "observe":
        response = {"screen":"HOME", "texts":["Ready"], "actions":[], "fps":60,
                    "state":{"tick":1}, "perf":{"frame_ms":1,"memory_mb":1}}
    else:
        response = {"ok":True,"pilot":True}
    stream.write(json.dumps(response) + "\n"); stream.flush()
"""


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "qajev-home"
    for module in (native, app, config):
        monkeypatch.setattr(module, "HOME", home)
    monkeypatch.setattr(native, "STATE", home / "native")
    monkeypatch.setattr(app, "STATE", home / "native")
    monkeypatch.setattr(config, "load_env", lambda *args: None)
    monkeypatch.setattr(config, "env_files", lambda *args: [])
    monkeypatch.setattr(cli, "_machine_lock", lambda *args: contextlib.nullcontext())
    monkeypatch.setattr(cli, "_wait_for_quiet", lambda *args: None)
    monkeypatch.setattr(native, "reap", lambda: [])
    monkeypatch.setattr(vision, "for_play", lambda *args, **kwargs: (False, None))
    monkeypatch.setattr(providers, "resolve", lambda *args: "fixture")
    monkeypatch.setattr(
        providers, "describe", lambda *args: {"decider": "fixture", "jev": "fixture", "text": "fixture", "keys": []}
    )
    monkeypatch.setattr(native, "OUTPUT_DRAIN_S", 0.1, raising=False)
    root = tmp_path / "project"
    root.mkdir()
    (root / "project.godot").write_text("; synthetic protocol fixture\n")
    executable = root / "fixture-app"
    executable.write_text(f"#!{sys.executable}\n" + BRIDGE)
    executable.chmod(0o755)
    monkeypatch.setattr(native, "GODOT", str(executable))
    monkeypatch.chdir(tmp_path)
    return root


def make_game(root, kind, **env):
    if kind == "app":
        return app.AppProcess(root, {"command": ["fixture-app"], "start_wait": 3, "env": env})
    return native.GodotGame(root, start_wait=3, env=env)


def play(game, name="ready", real_time=False):
    if real_time:
        return native.play_for(game, name=name, seconds=0, expect={"text": ["Ready"]}, shots=False)
    return native.play(
        game,
        name=name,
        goal=None,
        expect={"text": ["Ready"]},
        budget={"actions": 1, "seconds": 1},
        ledger=None,
        shots=False,
        wait=0,
    )


def cli_play(root, tmp_path, kind, capsys, *, env=None, real_time=False):
    suite = {"name": "final diagnostics", "steps": [{"name": "ready", "expect": {"text": ["Ready"]}}]}
    if real_time:
        suite["steps"][0]["play"] = {"seconds": 0}
    if kind == "app":
        suite["app"] = {"command": ["fixture-app"], "start_wait": 3, "env": env or {}}
    else:
        suite["env"] = env or {}
    # JSON is YAML-compatible; there is no credential/settings file in this fixture.
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(suite))
    code = cli.main(
        [
            "play",
            str(root),
            "--suite",
            str(path),
            "--json",
            "--no-shots",
            "--no-vision",
            "--out",
            str(tmp_path / "runs"),
        ]
    )
    data = json.loads(capsys.readouterr().out)
    stored = json.loads(next((tmp_path / "runs").glob("*/report.json")).read_text())
    assert stored["gate"] == data["gate"] and stored["scenarios"] == data["scenarios"]
    return code, data


@pytest.mark.parametrize("kind", ["app", "godot"])
@pytest.mark.parametrize("real_time", [False, True])
def test_cli_collects_shutdown_errors_after_close_before_publishing(
    isolated, tmp_path, monkeypatch, capsys, kind, real_time
):
    # The known scheduling control: drain only after close returned, after play/play_for copied their checks.
    read, close = native.GodotGame._read_output, native.GodotGame.close
    gates = {}

    def events(game):
        return gates.setdefault(game, (threading.Event(), threading.Event()))

    def delayed(game):
        gate, done = events(game)
        assert gate.wait(5)
        try:
            read(game)
        finally:
            done.set()

    def close_and_drain(game, *args, **kwargs):
        close(game, *args, **kwargs)
        gate, done = events(game)
        gate.set()
        assert done.wait(2)

    monkeypatch.setattr(native.GodotGame, "_read_output", delayed)
    monkeypatch.setattr(native.GodotGame, "close", close_and_drain)
    secret = "synthetic-finalization-secret"
    config.remember_secret(secret)
    code, data = cli_play(isolated, tmp_path, kind, capsys, env={"FINAL_ERROR": secret}, real_time=real_time)
    assert code == 1 and data["gate"] == "FAIL"
    result = data["scenarios"][0]
    check = next(item for item in result["checks"] if item["check"] == "no engine or script errors")
    assert check["ok"] is False and "[redacted]" in check["detail"]
    assert any(item["kind"] == "engine error" for item in result["findings"])
    assert result["diagnostics"]["errors"] == ["ERROR [redacted]"]
    assert secret not in json.dumps(data)


@pytest.mark.parametrize("kind", ["app", "godot"])
def test_close_waits_for_its_owned_reader_and_records_clean_completion(isolated, monkeypatch, kind):
    read = native.GodotGame._read_output
    exited, release, drained, returned = (threading.Event() for _ in range(4))

    def delayed(game):
        game.proc.wait(timeout=5)
        exited.set()
        assert release.wait(3)
        try:
            read(game)
        finally:
            drained.set()

    monkeypatch.setattr(native.GodotGame, "_read_output", delayed)
    monkeypatch.setattr(native, "OUTPUT_DRAIN_S", 1, raising=False)
    game = make_game(isolated, kind, FINAL_ERROR="shutdown failure")
    game.start()
    result = play(game)

    def stop():
        game.close()
        returned.set()

    closer = threading.Thread(target=stop)
    closer.start()
    try:
        assert exited.wait(3)
        assert not returned.wait(0.05)
    finally:
        release.set()
        closer.join(timeout=3)
    assert returned.is_set() and drained.is_set()
    assert result["outcome"] == "fail"
    assert result["diagnostics"] == {"complete": True, "errors": ["ERROR shutdown failure"], "reason": None}


@pytest.mark.parametrize("kind", ["app", "godot"])
def test_reader_timeout_is_bounded_and_never_publishes_a_no_errors_pass(isolated, tmp_path, monkeypatch, capsys, kind):
    read = native.GodotGame._read_output
    release, done = threading.Event(), threading.Event()

    def held(game):
        assert release.wait(5)
        try:
            read(game)
        finally:
            done.set()

    monkeypatch.setattr(native.GodotGame, "_read_output", held)
    started = time.monotonic()
    try:
        code, data = cli_play(isolated, tmp_path, kind, capsys)
        assert time.monotonic() - started < 3
        assert code == 2 and data["gate"] == "INCOMPLETE"
        result = data["scenarios"][0]
        assert result["outcome"] == "harness" and "capture" in result["reason"]
        assert result["diagnostics"]["complete"] is False
        assert all(item["ok"] is False for item in result["checks"] if item["check"] == "no engine or script errors")
    finally:
        release.set()
        assert done.wait(2)


@pytest.mark.parametrize("kind", ["app", "godot"])
def test_undecodable_output_is_incomplete_instead_of_a_false_pass(isolated, tmp_path, capsys, kind):
    code, data = cli_play(isolated, tmp_path, kind, capsys, env={"BAD_OUTPUT": "1"})
    assert code == 2 and data["gate"] == "INCOMPLETE"
    assert data["scenarios"][0]["diagnostics"]["complete"] is False
    assert "capture" in data["scenarios"][0]["reason"]
    assert "private undecodable output" not in json.dumps(data)


@pytest.mark.parametrize("kind", ["app", "godot"])
def test_a_delayed_previous_launch_cannot_read_or_poison_the_relaunch(isolated, monkeypatch, kind):
    read = native.GodotGame._read_output
    release, old_done = threading.Event(), threading.Event()
    launches = []

    def delayed_first(game):
        launches.append(game.proc.pid)
        first = len(launches) == 1
        if first:
            assert release.wait(5)
        try:
            read(game)
        finally:
            if first:
                old_done.set()

    monkeypatch.setattr(native.GodotGame, "_read_output", delayed_first)
    game = make_game(isolated, kind, FINAL_ERROR="old launch only")
    game.start()
    first = play(game, "first launch")
    try:
        game.env["FINAL_ERROR"] = ""
        game.relaunch()
        second = play(game, "second launch")
        release.set()
        assert old_done.wait(1), "the previous reader followed the new process instead of its own stream"
        game.close()
        native.finalize_output(game, [first, second])
        assert first["outcome"] == "fail" and first["diagnostics"]["errors"] == ["ERROR old launch only"]
        assert second["outcome"] == "pass" and second["diagnostics"]["errors"] == []
    finally:
        release.set()
        game.close()


def test_interactive_play_does_not_close_the_game_and_finalization_keeps_a_product_fail(isolated, monkeypatch):
    game = make_game(isolated, "godot", FINAL_ERROR="late failure")
    with game:
        result = play(game)
        assert game.proc is not None
        assert result["outcome"] == "pass" and game.proc.poll() is None
        native.finalize_output(game, [result])
        assert result["outcome"] == "pass" and game.proc.poll() is None
        assert game.observe()["screen"] == "HOME"
    assert result["outcome"] == "fail" and result["diagnostics"]["complete"] is True
    native.finalize_output(game, [result])
    native.finalize_output(game, [result])
    assert len([item for item in result["findings"] if item["kind"] == "engine error"]) == 1


def test_incomplete_capture_does_not_downgrade_an_existing_product_failure(isolated, monkeypatch):
    read = native.GodotGame._read_output
    release, done = threading.Event(), threading.Event()

    def held(game):
        assert release.wait(5)
        try:
            read(game)
        finally:
            done.set()

    monkeypatch.setattr(native.GodotGame, "_read_output", held)
    game = make_game(isolated, "godot")
    try:
        with game:
            result = native.play(
                game,
                name="wrong screen",
                goal=None,
                expect={"screen": "OTHER"},
                budget={"actions": 1, "seconds": 1},
                ledger=None,
                shots=False,
                wait=0,
            )
            assert result["outcome"] == "fail"
        assert result["outcome"] == "fail" and result["diagnostics"]["complete"] is False
        assert "capture" in result["reason"]
    finally:
        release.set()
        assert done.wait(2)


def test_final_diagnostics_retain_every_error_beyond_the_finding_summary(isolated):
    lines = [f"ERROR shutdown error {index}" for index in range(14)]
    game = make_game(isolated, "godot", FINAL_ERROR="\n".join(lines)[6:])
    with game:
        result = play(game)
    assert result["outcome"] == "fail" and result["diagnostics"]["errors"] == lines
    assert result["diagnostics"]["complete"] is True


def test_explicit_allow_errors_keeps_the_diagnostics_without_failing_the_error_check(isolated):
    game = make_game(isolated, "app", FINAL_ERROR="an allowed error")
    with game:
        result = native.play(
            game,
            name="allowed errors",
            goal=None,
            expect={"text": ["Ready"], "no_errors": False},
            budget={"actions": 1, "seconds": 1},
            ledger=None,
            shots=False,
            wait=0,
        )
    assert result["outcome"] == "pass" and result["diagnostics"]["errors"] == ["ERROR an allowed error"]
    assert not any(item["check"] == "no engine or script errors" for item in result["checks"])


@pytest.mark.parametrize("kind", ["app", "godot"])
def test_long_known_secret_is_redacted_before_any_error_line_limit(isolated, tmp_path, capsys, kind):
    secret = "synthetic-long-secret-" + "x" * 400 + "-tail"
    config.remember_secret(secret)
    code, data = cli_play(isolated, tmp_path, kind, capsys, env={"FINAL_ERROR": secret})
    assert code == 1 and data["gate"] == "FAIL"
    result = data["scenarios"][0]
    assert result["diagnostics"]["errors"] == ["ERROR [redacted]"]
    check = next(item for item in result["checks"] if item["check"] == "no engine or script errors")
    assert check["detail"] == "ERROR [redacted]"
    assert secret[:128] not in json.dumps(data)
