"""The reaper on real processes: a dead run's real Chrome is stopped by its record's pid, and a process in another
PID namespace is never stopped, though it runs as this user and looks like a dead run's Chrome (dash, 2 Nov)."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from qajev import chrome

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.environ.get("QAJEV_LIVE") != "1", reason="explicit isolated Linux proof only")]


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


@pytest.fixture
def state(monkeypatch, tmp_path):
    if sys.platform != "linux":
        pytest.skip("Linux PID namespaces")
    monkeypatch.setattr(chrome, "STATE", tmp_path / "state")
    monkeypatch.setattr(chrome, "EPHEMERAL_ROOT", tmp_path / "ephemeral")
    return chrome.STATE


def test_a_dead_runs_real_chrome_is_stopped_by_its_record_and_only_from_its_namespace(state, monkeypatch):
    if not Path("/usr/lib/chromium/chromium").is_file():
        pytest.skip("Linux Chromium fixture required")
    monkeypatch.setenv("QAJEV_CHROME", "/usr/lib/chromium/chromium")
    monkeypatch.setenv("QAJEV_CHROME_FLAGS", "--no-sandbox --disable-gpu")
    record = chrome.start("reap", headless=True, ephemeral=True, port=0)
    path = state / f"{record['state_key']}.json"
    try:
        saved = json.loads(path.read_text())
        assert saved["pid_ns"] is not None and saved["pid_ns"] == chrome.pid_namespace()
        assert chrome.ours(saved["pid"])
        mine = f"chrome {saved['pid']} ({Path(saved['profile_dir']).name})"
        # Written in another namespace: its owner looks dead from here, and nothing of it is touched.
        path.write_text(json.dumps({**saved, "owner_pid": dead_pid(), "pid_ns": "pid:[1]"}))
        assert mine not in chrome.reap()
        assert chrome.version(saved["cdp_url"]) and Path(saved["profile_dir"]).is_dir() and path.exists()
        # Written here, by a run that died: stopped by that pid, its profile and record gone.
        path.write_text(json.dumps({**saved, "owner_pid": dead_pid()}))
        assert mine in chrome.reap()
        assert not chrome.alive(saved["pid"])
        assert not Path(saved["profile_dir"]).exists() and not path.exists()
    finally:
        if path.exists():
            chrome.stop(record["state_key"])
        elif chrome.alive(record["pid"]):
            chrome._kill(record["pid"], group=True)  # the Chrome this test started, by its own pid


def namespace(pid="self"):
    try:
        return os.readlink(f"/proc/{pid}/ns/pid")
    except OSError:
        return None


def in_new_namespace(*argv):
    """A process in a PID namespace of its own (a user namespace maps it to this user), as a sandboxed job's is:
    -> (the unshare process, the process's pid as this namespace sees it)."""
    if not shutil.which("unshare"):
        pytest.skip("no unshare")
    outer = subprocess.Popen(["unshare", "--user", "--map-root-user", "--pid", "--fork", sys.executable, "-c",
                              "import time; time.sleep(60)", *argv], stderr=subprocess.PIPE)
    children = Path(f"/proc/{outer.pid}/task/{outer.pid}/children")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if outer.poll() is not None:
            said = outer.stderr.read().decode()[:200] if outer.stderr else ""
            pytest.skip(f"unshare cannot make a PID namespace here: {said}")
        try:
            inner = children.read_text().split()
        except OSError:
            inner = []
        if inner and namespace(inner[0]) not in (None, namespace()):
            return outer, int(inner[0])
        time.sleep(0.05)
    outer.kill()
    pytest.skip("unshare started no process in a new PID namespace")


def test_reap_never_stops_a_real_process_in_another_pid_namespace(state):
    profile = chrome.EPHEMERAL_ROOT / f"qajev-default-{dead_pid()}-abc123"
    profile.mkdir(parents=True)
    outer, pid = in_new_namespace(f"--user-data-dir={profile}", "--remote-debugging-port=9399")
    try:
        assert Path(f"/proc/{pid}").stat().st_uid == os.getuid()  # this user's, as far as this namespace sees
        state.mkdir()
        path = state / "default.ephemeral-1-abc123.json"
        path.write_text(json.dumps({"profile": "default", "pid": pid, "port": 9399, "launch_port": 9399,
                                    "profile_dir": str(profile), "ephemeral": True, "owner_pid": dead_pid(),
                                    "temp_dir": None, "pid_ns": namespace()}))
        assert chrome.owns(json.loads(path.read_text()))  # it is the recorded Chrome's command line
        reaped = chrome.reap()
        assert not any(f"chrome {pid} " in r for r in reaped), reaped
        assert chrome.alive(pid) and outer.poll() is None
        assert profile.is_dir() and path.exists()
    finally:
        outer.kill()
        outer.wait()
