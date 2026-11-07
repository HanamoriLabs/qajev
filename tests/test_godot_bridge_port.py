"""QAJEV_GODOT_BRIDGE_PORT: a Godot game's bridge on one fixed loopback port, for a machine whose firewall opens only it."""

import socket
import sys

import pytest

from qajev import native

BRIDGE = r"""
import json, os, socket
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", int(os.environ["QAJEV_BRIDGE_PORT"])))
server.listen(1)
connection, _ = server.accept()
stream = connection.makefile("rw")
for line in stream:
    if json.loads(line)["op"] == "quit":
        stream.write('{"ok":true}\n'); stream.flush()
        break
    stream.write(json.dumps({"ok": True}) + "\n"); stream.flush()
"""


def unused_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def project(tmp_path, monkeypatch):
    home = tmp_path / "qajev-home"
    monkeypatch.setattr(native, "HOME", home)
    monkeypatch.setattr(native, "STATE", home / "native")
    root = tmp_path / "project"
    root.mkdir()
    (root / "project.godot").write_text("; synthetic protocol fixture\n")
    executable = root / "fake-godot"
    executable.write_text(f"#!{sys.executable}\n" + BRIDGE)
    executable.chmod(0o755)
    monkeypatch.setattr(native, "GODOT", str(executable))
    return root


def test_without_the_setting_the_first_free_port_is_used(monkeypatch):
    monkeypatch.setattr(native, "free_port", lambda: 9417)
    assert native.godot_bridge_port({}) == 9417
    assert native.godot_bridge_port({native.GODOT_BRIDGE_PORT: "  "}) == 9417


@pytest.mark.parametrize("raw", ["abc", "94.42", "-1", "80", "1023", "65536", "99999", "9442 9443", "٩٤٤٢"])
def test_a_value_that_is_not_a_port_is_refused(raw):
    with pytest.raises(native.NativeError, match="must be a port number from 1024 to 65535"):
        native.godot_bridge_port({native.GODOT_BRIDGE_PORT: raw})


def test_a_fixed_port_in_use_is_an_error_never_a_move_to_another_port(project, monkeypatch):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        monkeypatch.setenv(native.GODOT_BRIDGE_PORT, str(port))
        game = native.GodotGame(project, start_wait=3)
        with pytest.raises(native.NativeError, match=f"{native.GODOT_BRIDGE_PORT}={port}: another program listens"):
            game.start()
        assert game.proc is None  # refused before the game started


def test_the_game_gets_the_fixed_port_and_keeps_it_on_a_relaunch(project, monkeypatch):
    port = unused_port()
    monkeypatch.setenv(native.GODOT_BRIDGE_PORT, str(port))
    with native.GodotGame(project, start_wait=5) as game:
        assert game.port == port and game.record["port"] == port
        assert game.sock is not None and game.sock.getpeername()[1] == port
        game.relaunch()  # close waits for the first game to end, so its port is free again
        assert game.port == port and game.sock.getpeername()[1] == port
