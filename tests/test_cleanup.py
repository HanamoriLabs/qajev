"""What a dead run leaves behind gets cleaned up; an interrupted run finishes its own cleanup."""

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

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


@pytest.fixture
def state(tmp_path, monkeypatch):
    """This test's own state records and throwaway profiles: no other run's are read or touched."""
    monkeypatch.setattr(chrome, "STATE", tmp_path / "state")
    monkeypatch.setattr(chrome, "EPHEMERAL_ROOT", tmp_path / "ephemeral")
    chrome.EPHEMERAL_ROOT.mkdir()
    return chrome.STATE


def namespace():
    """This process's PID namespace, as /proc names it (None where there are none)."""
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def orphan_of(state, owner, **record):
    """A dead run's Chrome: a stand-in with its command line, its profile, and its run's state record."""
    profile = chrome.EPHEMERAL_ROOT / f"qajev-default-{owner}-abc123"
    profile.mkdir()
    proc = sleeper(f"--user-data-dir={profile}", "--remote-debugging-port=9399")
    state.mkdir(exist_ok=True)
    path = state / f"default.ephemeral-{owner}-abc123.json"
    path.write_text(json.dumps({"profile": "default", "pid": proc.pid, "port": 9399, "launch_port": 9399,
                                "profile_dir": str(profile), "ephemeral": True, "owner_pid": owner,
                                "temp_dir": None, "pid_ns": namespace(), **record}))
    return proc, profile, path


@pytest.fixture
def signals(monkeypatch):
    """Every signal sent, as (pid, signal); a pid in `refused` gets the kernel's answer for another user's process."""
    sent, refused, kill = [], set(), os.kill

    def send(pid, sig):
        if sig:
            sent.append((pid, sig))
            if pid in refused:
                raise PermissionError(1, "Operation not permitted")
        return kill(pid, sig)

    monkeypatch.setattr(chrome.os, "kill", send)
    return SimpleNamespace(sent=sent, refused=refused, to=lambda pid: [s for p, s in sent if p == pid])


def test_reap_stops_an_orphaned_ephemeral_chrome_and_deletes_its_profile(state, owner):
    orphan, profile, record = orphan_of(state, owner)
    helper = sleeper(f"--user-data-dir={profile}", "--type=renderer")  # helpers are not ours to kill
    try:
        reaped = chrome.reap()
        assert any(str(orphan.pid) in r for r in reaped), reaped
        assert wait_gone(orphan)
        assert not profile.exists() and not record.exists()
        assert helper.poll() is None
    finally:
        for p in (orphan, helper):
            p.kill()


def test_reap_never_stops_a_chrome_found_only_by_its_command_line(state, owner, signals):
    # The Orchestrator, 2 Nov: cleanup stops only what a run recorded starting, never a process found by its name.
    # This one looks like a dead run's Chrome, but no run of this QAJev home recorded it.
    orphan, _, record = orphan_of(state, owner)
    record.unlink()
    try:
        reaped = chrome.reap()
        assert not any(str(orphan.pid) in r for r in reaped), reaped
        assert signals.to(orphan.pid) == [] and orphan.poll() is None  # its stray folder may go: it is ours
    finally:
        orphan.kill()


def test_a_refused_stop_never_aborts_the_run(state, owner, signals):
    # dash, 2 Nov: on a shared box, QAJev's cleanup at the start of a run sent SIGTERM to a Chrome of another user's
    # QAJev (a test job in its own PID namespace, so its owner looked dead), got PermissionError, and the run aborted.
    orphan, profile, record = orphan_of(state, owner)
    signals.refused.add(orphan.pid)  # the kernel refuses it: as for a process that turns out to be another user's
    try:
        reaped = chrome.reap()  # no PermissionError
        assert not any(str(orphan.pid) in r for r in reaped), reaped
        assert orphan.poll() is None and profile.exists() and record.exists()
    finally:
        signals.refused.clear()
        orphan.kill()


def test_reap_never_stops_another_users_chrome(state, owner, signals, monkeypatch):
    # A QAJev that runs as root could stop any user's Chrome: the user check is what keeps it to its own.
    orphan, profile, record = orphan_of(state, owner)
    foreign(monkeypatch, orphan.pid)
    try:
        chrome.reap()
        assert signals.to(orphan.pid) == [] and orphan.poll() is None
        assert profile.exists() and record.exists()
    finally:
        orphan.kill()


def test_reap_never_stops_a_chrome_in_another_pid_namespace(state, owner, signals, monkeypatch):
    # A sandboxed job's Chrome, as the reaper sees it: this user's, in a PID namespace of its own.
    orphan, profile, record = orphan_of(state, owner)
    real = chrome.pid_namespace
    monkeypatch.setattr(chrome, "pid_namespace", lambda pid="self": "pid:[1]" if pid == orphan.pid else real(pid))
    try:
        chrome.reap()
        assert signals.to(orphan.pid) == [] and orphan.poll() is None
        assert profile.exists() and record.exists()
    finally:
        orphan.kill()


def test_reap_does_not_judge_a_record_written_in_another_pid_namespace(state, owner, signals):
    # Its owner pid means another process here, so a dead-looking owner proves nothing: its Chrome, its record and
    # its profile all stay, though the profile's name says a dead run's.
    orphan, profile, record = orphan_of(state, owner, pid_ns="pid:[1]")
    try:
        reaped = chrome.reap()
        assert signals.to(orphan.pid) == [] and orphan.poll() is None
        assert profile.exists() and record.exists(), reaped
    finally:
        orphan.kill()


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


def test_reap_leaves_another_users_leftover_folder(state, owner, monkeypatch):
    # /tmp is shared: a dead-looking run's folder there can be another user's, and is never ours to delete.
    folder = chrome.EPHEMERAL_ROOT / f"qajev-default-{owner}-abc123"
    folder.mkdir()
    uid = os.getuid()
    monkeypatch.setattr(chrome.os, "getuid", lambda: uid + 1)  # the folder is now another user's
    reaped = chrome.reap()
    assert folder.is_dir() and not any(folder.name in r for r in reaped), reaped


def foreign(monkeypatch, pid):
    """Process `pid` is another user's, as far as the reaper can tell."""
    user_of = chrome.user_of
    monkeypatch.setattr(chrome, "user_of", lambda p: os.getuid() + 1 if p == pid else user_of(p))


def test_reap_never_stops_another_users_harness_daemon(tmp_path, monkeypatch, signals):
    monkeypatch.setenv("BH_RUNTIME_DIR", str(tmp_path))
    daemon = sleeper("-m", "browser_harness.daemon")
    foreign(monkeypatch, daemon.pid)
    name = f"qajev-{dead_pid()}-a1b2c3"
    (tmp_path / f"bu-{name}.pid").write_text(json.dumps({"pid": daemon.pid}))
    try:
        chrome.reap()
        assert signals.to(daemon.pid) == [] and daemon.poll() is None
        assert (tmp_path / f"bu-{name}.pid").exists()
    finally:
        daemon.kill()


def test_reap_never_stops_another_users_daemon_found_by_its_name(tmp_path, monkeypatch, owner, signals):
    monkeypatch.setenv("BH_RUNTIME_DIR", str(tmp_path))  # no pid file: only the process is left to find
    daemon = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "-m", "browser_harness.daemon"],
                              env={**os.environ, "BU_NAME": f"qajev-{owner}-d4e5f6"})
    foreign(monkeypatch, daemon.pid)
    try:
        chrome.reap()
        assert signals.to(daemon.pid) == [] and daemon.poll() is None
    finally:
        daemon.kill()
