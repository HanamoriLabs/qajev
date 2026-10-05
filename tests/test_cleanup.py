"""What a dead run leaves behind gets cleaned up; an interrupted run finishes its own cleanup."""

import json
import os
import subprocess
import sys
import time

import pytest

from qajev import chrome, cli


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def sleeper(*argv):
    # A stand-in whose command line looks like the process the reaper hunts for.
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *argv])


@pytest.fixture
def owner(monkeypatch):
    """An owner pid alive to every other reaper on the machine and dead only to this test's own reap. A QAJev run
    starting elsewhere, or a second suite, reaps machine-wide: with a truly dead owner it could stop this test's
    orphan first and leave this test's reap nothing to report."""
    proc = sleeper()
    alive = chrome.alive
    monkeypatch.setattr(chrome, "alive", lambda pid: pid != proc.pid and alive(pid))
    yield proc.pid
    proc.kill()


def wait_gone(proc, seconds=10):
    deadline = time.monotonic() + seconds
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    return proc.poll() is not None


def test_reap_stops_an_orphaned_ephemeral_chrome_and_deletes_its_profile(tmp_path, owner):
    profile = tmp_path / f"qajev-default-{owner}-abc123"
    profile.mkdir()
    orphan = sleeper(f"--user-data-dir={profile}", "--remote-debugging-port=9399")
    helper = sleeper(f"--user-data-dir={profile}", "--type=renderer")  # helpers are not ours to kill
    try:
        reaped = chrome.reap()
        assert any(str(orphan.pid) in r for r in reaped), reaped
        assert wait_gone(orphan)
        assert not profile.exists()
        assert helper.poll() is None
    finally:
        for p in (orphan, helper):
            p.kill()


def test_reap_leaves_a_live_owners_chrome_alone(tmp_path):
    import os

    profile = tmp_path / f"qajev-default-{os.getpid()}-abc123"
    profile.mkdir()
    mine = sleeper(f"--user-data-dir={profile}", "--remote-debugging-port=9399")
    try:
        chrome.reap()
        time.sleep(0.2)
        assert mine.poll() is None and profile.exists()
    finally:
        mine.kill()


def test_reap_stops_an_orphaned_harness_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_RUNTIME_DIR", str(tmp_path))
    owner = dead_pid()
    daemon = sleeper("-m", "browser_harness.daemon")
    name = f"qajev-{owner}-a1b2c3"
    (tmp_path / f"bu-{name}.pid").write_text(json.dumps({"pid": daemon.pid}))
    (tmp_path / f"bu-{name}.sock").write_text("")
    try:
        reaped = chrome.reap()
        assert any(name in r for r in reaped), reaped
        assert wait_gone(daemon)
        assert not list(tmp_path.glob(f"bu-{name}.*"))
    finally:
        daemon.kill()


def test_a_second_signal_cannot_abort_cleanup(monkeypatch):
    monkeypatch.setattr(cli, "_interrupted", False)
    with pytest.raises(KeyboardInterrupt):
        cli._interrupt(15, None)
    assert cli._interrupt(15, None) is None  # ignored: cleanup keeps going


def test_progress_to_a_closed_pipe_is_dropped_not_fatal():
    calls = []

    def write(event):
        calls.append(event)
        raise BrokenPipeError

    emit = cli._safe(write)
    emit({"event": "start"})
    emit({"event": "scenario"})  # must not raise, and must not keep trying
    assert len(calls) == 1


def test_reap_removes_a_stray_temp_profile_of_a_dead_run(owner):
    import tempfile
    from pathlib import Path

    chrome.EPHEMERAL_ROOT.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=f"qajev-default-{owner}-", dir=chrome.EPHEMERAL_ROOT))
    reaped = chrome.reap()
    assert not folder.exists() and any(folder.name in r for r in reaped)


def test_a_failed_chrome_start_deletes_its_temp_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(chrome, "binary", lambda: "/usr/bin/false")  # exits at once: the start fails
    monkeypatch.setattr(chrome, "EPHEMERAL_ROOT", tmp_path)
    with pytest.raises(chrome.ChromeError):
        chrome.start("failing", ephemeral=True, headless=True, wait=5)
    assert not list(tmp_path.glob("qajev-failing-*"))


def test_stopping_our_own_child_returns_as_soon_as_it_exits():
    # A child that exited is a zombie until reaped, and a zombie still answers kill(pid, 0): without reaping, every
    # stop waited out its whole grace period (5 s per Chrome, 15 s per harness daemon, at the end of every run).
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    began = time.monotonic()
    chrome._kill(child.pid)
    assert time.monotonic() - began < 2
    assert chrome.reaped(child.pid)


def test_reap_stops_an_orphaned_daemon_whose_pid_file_is_gone(tmp_path, monkeypatch, owner):
    # Seen for real: a daemon outlived its run and its runtime files, so only its process was left to find.
    monkeypatch.setenv("BH_RUNTIME_DIR", str(tmp_path))  # empty: no pid file to go by
    env = {**os.environ, "BU_NAME": f"qajev-{owner}-d4e5f6"}
    ours = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "-m", "browser_harness.daemon"],
                            env=env)
    theirs = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "-m", "browser_harness.daemon"],
                              env={**os.environ, "BU_NAME": f"other-{dead_pid()}-x"})
    try:
        reaped = chrome.reap()
        assert any(f"daemon {ours.pid}" in r for r in reaped), reaped
        assert wait_gone(ours)
        assert theirs.poll() is None  # another project's daemon is none of our business
    finally:
        ours.kill()
        theirs.kill()
