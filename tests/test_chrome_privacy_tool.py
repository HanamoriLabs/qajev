"""Proof-controller failures stop its own child; no real subprocess or browser."""

import importlib.util
import signal
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def controller(monkeypatch, tmp_path):
    path = Path(__file__).parents[1] / "tools/prove-chrome-password.py"
    spec = importlib.util.spec_from_file_location("password_proof", path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    monkeypatch.setattr(tool.sys, "platform", "linux")

    class Child:
        returncode = None
        stubborn = False

        def __init__(self):
            self.signals, self.waits, self.killed = [], [], False

        def poll(self):
            return self.returncode

        def send_signal(self, value):
            self.signals.append(value)

        def wait(self, timeout):
            self.waits.append(timeout)
            if self.stubborn and not self.killed:
                raise subprocess.TimeoutExpired("fixture", timeout)
            self.returncode = -9 if self.killed else 130
            return self.returncode

        def kill(self):
            self.killed = True

    child = Child()
    original_readlink = tool.os.readlink
    monkeypatch.setattr(tool.os, "readlink", lambda path, *a, **kw:
                        "/tmp/gpu-heavy.lock" if str(path) == "/proc/self/fd/7" else original_readlink(path, *a, **kw))
    monkeypatch.setattr(tool.os, "getpriority", lambda *_: 19)
    monkeypatch.setattr(tool.os, "getloadavg", lambda: (0, 0, 0))
    monkeypatch.setattr(tool.sys, "argv", [str(path), "--out", str(tmp_path / "proof")])
    monkeypatch.setattr(tool.subprocess, "Popen", lambda *_a, **_kw: child)
    monkeypatch.setattr(tool.time, "sleep", lambda _seconds: setattr(child, "returncode", 0))
    return tool, child


def test_successful_proof_does_not_signal_a_finished_child(controller):
    tool, child = controller
    assert tool.main() == 0
    assert child.signals == [] and not child.killed


def test_interrupted_proof_stops_and_waits_for_its_child(controller, monkeypatch):
    tool, child = controller

    def interrupt(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(tool.time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        tool.main()
    assert child.signals == [signal.SIGINT] and child.waits == [30]
    assert child.returncode == 130


@pytest.mark.parametrize("stubborn", [False, True])
def test_load_stop_reaps_a_stubborn_proof_child(controller, monkeypatch, stubborn):
    tool, child = controller
    child.stubborn = stubborn
    loads = iter([(0, 0, 0), (61, 0, 0)])
    monkeypatch.setattr(tool.os, "getloadavg", lambda: next(loads))
    with pytest.raises(Exception) as error:
        tool.main()
    assert isinstance(error.value, RuntimeError) and "load1" in str(error.value)
    assert child.signals == [signal.SIGINT]
    assert child.killed is stubborn
    assert child.waits == ([30, 5] if stubborn else [30])
    assert child.returncode == (-9 if stubborn else 130)
