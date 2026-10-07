"""A run stops itself when the process that started it ends. SideGame1, 7 Oct: the lane stopped a run script for
high load, but `qajev play` lived on under launchd with no lane lease, its game window at 120% CPU for 20 minutes."""

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The run: `qajev play` through cli.main, with the game swapped for a loop that ends only on an interrupt, the way
# cmd_play ends and closes the game on Ctrl-C or SIGTERM.
RUN = """
import sys, time
from qajev import cli

def play(args):
    open(sys.argv[1], "w").write("running")
    try:
        while True:
            time.sleep(0.05)
    except KeyboardInterrupt:
        open(sys.argv[1], "w").write("stopped: game closed")
        return 130

cli.cmd_play = play
sys.exit(cli.main(["play", "some-game"]))
"""

# The script a lane runs: it starts the run, waits until it plays, then ends (as a lane stop ends it).
SCRIPT = """
import subprocess, sys, time
run = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)  # not our pipes: the test reads ours to its end
for _ in range(200):
    if open(sys.argv[2]).read():
        break
    time.sleep(0.05)
print(run.pid, flush=True)
"""


def _start(tmp_path, **env):
    (tmp_path / "run.py").write_text(RUN)
    (tmp_path / "script.py").write_text(SCRIPT)
    marker = tmp_path / "marker"
    marker.write_text("")
    base = {k: v for k, v in os.environ.items() if k != "QAJEV_OUTLIVE_PARENT"}
    env = {**base, "PYTHONPATH": str(ROOT), "QAJEV_HOME": str(tmp_path / "home"), "QAJEV_JOB": "1", **env}
    out = subprocess.run([sys.executable, str(tmp_path / "script.py"), str(tmp_path / "run.py"), str(marker)],
                         capture_output=True, text=True, env=env, timeout=30)
    return int(out.stdout.strip()), marker


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait(condition, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return False


def test_a_run_whose_script_ended_stops_itself_and_closes_its_game(tmp_path):
    pid, marker = _start(tmp_path)
    try:
        assert _wait(lambda: marker.read_text().startswith("stopped"), 15), "the run went on with no one to stop it"
        assert marker.read_text() == "stopped: game closed"
        assert _wait(lambda: not _alive(pid), 10)
    finally:
        if _alive(pid):
            os.kill(pid, 9)


def test_a_run_detached_on_purpose_keeps_going(tmp_path):
    pid, marker = _start(tmp_path, QAJEV_OUTLIVE_PARENT="1")
    try:
        time.sleep(5)  # more than two of the watch's looks
        assert marker.read_text() == "running" and _alive(pid)
    finally:
        os.kill(pid, 9)
