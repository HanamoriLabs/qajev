"""QAJEV_GODOT_BRIDGE_PORT: a Godot game's bridge on one fixed loopback port, for a firewall that opens only it."""

import os
import signal
import socket
import sys
import time

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


# A "game" that does not listen itself: it starts another process that listens on the bridge port (an impostor that
# took the port), writes that process's pid, and waits.
IMPOSTOR_GAME = r"""
import os, subprocess, sys, time
listener = ("import os, socket, time\n"
            "s = socket.socket(); s.bind(('127.0.0.1', int(os.environ['QAJEV_BRIDGE_PORT']))); s.listen(4)\n"
            "while True:\n    c, _ = s.accept()\n")
child = subprocess.Popen([sys.executable, "-c", listener])
with open(os.environ["IMPOSTOR_PID_FILE"], "w") as f:
    f.write(str(child.pid))
time.sleep(60)
"""

TCP_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def tcp_row(port, state, inode):
    cols = ["0:", f"0100007F:{port:04X}", "00000000:0000", state, "00000000:00000000", "00:00000000", "00000000",
            "1000", "0", inode]
    return "   " + " ".join(cols) + "\n"


def running(pid):
    """The process exists and has not ended (a zombie has ended)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rpartition(")")[2].split()[0] != "Z"
    except OSError:
        return False


def fake_proc(tmp_path, rows, pid=4242, fds=()):
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "tcp").write_text(TCP_HEADER + "".join(rows))
    fd_dir = tmp_path / str(pid) / "fd"
    fd_dir.mkdir(parents=True)
    for n, inode in enumerate(fds):
        (fd_dir / str(n)).symlink_to(f"socket:[{inode}]")
    return pid


def test_the_bridge_is_the_games_own_socket(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "PROC", str(tmp_path))
    pid = fake_proc(tmp_path, [tcp_row(9442, "0A", "777"), tcp_row(9442, "01", "888")], fds=["777"])
    assert native.listener_inodes(9442) == {"777"}  # 01 is a connection, not a listener
    assert native.bridge_impostor(9442, pid) == ""


def test_a_listener_that_is_not_the_games_is_named(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "PROC", str(tmp_path))
    pid = fake_proc(tmp_path, [tcp_row(9442, "0A", "777"), tcp_row(9442, "0A", "999")], fds=["777"])
    assert "another program listens on port 9442 (1 socket(s)" in native.bridge_impostor(9442, pid)
    assert "nothing listens" in native.bridge_impostor(9443, pid)


def test_without_proc_tables_nothing_is_claimed(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "PROC", str(tmp_path / "none"))
    assert native.listener_inodes(9442) is None and native.bridge_impostor(9442, 1) == ""


@pytest.mark.skipif(not os.path.exists("/proc/net/tcp"), reason="needs Linux /proc tables")
def test_a_game_whose_bridge_port_another_process_holds_is_refused(project, monkeypatch, tmp_path):
    (project / "fake-godot").write_text(f"#!{sys.executable}\n" + IMPOSTOR_GAME)
    pid_file = tmp_path / "impostor.pid"
    port = unused_port()
    monkeypatch.setenv(native.GODOT_BRIDGE_PORT, str(port))
    game = native.GodotGame(project, start_wait=10, env={"IMPOSTOR_PID_FILE": str(pid_file)})
    with pytest.raises(native.NativeError, match=f"{native.GODOT_BRIDGE_PORT}={port}: another program listens"):
        game.start()
    assert game.sock is None and game.proc is not None and game.proc.poll() is not None  # closed and stopped
    impostor = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and running(impostor):
        time.sleep(0.1)
    alive = running(impostor)
    if alive:  # never leave it running (K18), then fail
        os.kill(impostor, signal.SIGKILL)
    assert not alive, "the impostor outlived the game's process group"
