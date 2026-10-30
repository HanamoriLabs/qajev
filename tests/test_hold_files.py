"""A browser run waits while another job holds the machine alone (QAJEV_HOLD_FILES), and its report says so.

workload-gpu, 30 Oct: rea's GPU jobs write gpu-solo.hold ("<job> <pid> <time>") while they need the box alone. A
QAJev run that started in a gap between holds skewed their numbers (redteam N3).
"""

import os
import subprocess
import sys
import threading
import time

import pytest

from qajev import browser_pool, jobs, report, report_html


@pytest.fixture(autouse=True)
def pool(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_pool, "ROOT", tmp_path / "pool")
    monkeypatch.setattr(browser_pool, "available_memory", lambda: 32 * 1024**3)
    monkeypatch.setattr(jobs, "LOCK", tmp_path / "legacy.lock")
    monkeypatch.setattr(jobs, "local_runs_off", lambda: False)
    monkeypatch.delenv("QAJEV_BROWSER_RUNS", raising=False)
    monkeypatch.delenv("QAJEV_HOLD_FILES", raising=False)
    monkeypatch.delenv("QAJEV_HOLD_OWNER", raising=False)


@pytest.fixture
def other_job():
    """A live process that is not this run nor one it runs under: the job that owns the hold."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield proc.pid
    proc.kill()
    proc.wait()


def _hold(monkeypatch, tmp_path, text, *more):
    hold = tmp_path / "gpu-solo.hold"
    hold.write_text(text)
    monkeypatch.setenv("QAJEV_HOLD_FILES", os.pathsep.join([str(hold), *map(str, more)]))
    return hold


def test_a_live_hold_of_another_job_keeps_the_run_out_and_says_whose_it_is(monkeypatch, tmp_path, other_job):
    hold = _hold(monkeypatch, tmp_path, f"walk-run {other_job} 16:20:00\n")
    events = []
    with pytest.raises(jobs.Busy, match="gpu-solo.hold") as error:
        with jobs.machine_lock("suite", wait=0.3, emit=events.append):
            raise AssertionError("the run started while another job held the machine")
    assert f"walk-run {other_job}" in str(error.value)
    assert events and events[0]["event"] == "waiting"
    assert str(hold) in events[0]["reason"] and f"walk-run {other_job}" in events[0]["reason"]
    assert not browser_pool.holders()


def test_the_run_starts_when_the_hold_goes_and_its_timing_logs_the_wait(monkeypatch, tmp_path, other_job):
    hold = _hold(monkeypatch, tmp_path, f"prod-vs-dev {other_job} 16:20:00\n")
    threading.Timer(0.5, hold.unlink).start()
    started = time.monotonic()
    with jobs.machine_lock("suite", wait=5):
        waited = time.monotonic() - started
        measured = browser_pool.timing()
    assert waited >= 0.4
    [logged] = measured["hold_waits"]
    assert logged["file"] == str(hold) and logged["owner"] == f"prod-vs-dev {other_job} 16:20:00"
    assert 0.4 <= logged["seconds"] <= waited + 0.1
    assert measured["hold_seconds"] == logged["seconds"]
    assert measured["queue_seconds"] >= logged["seconds"]


@pytest.mark.parametrize("owner", ["dead", "self", "parent"])
def test_a_stale_hold_or_this_runs_own_hold_does_not_wait(monkeypatch, tmp_path, owner):
    if owner == "dead":
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        pid = proc.pid
    else:
        pid = os.getpid() if owner == "self" else os.getppid()
    _hold(monkeypatch, tmp_path, f"baseline {pid} 16:20:00\n")
    with jobs.machine_lock("suite", wait=0.3):
        assert "hold_waits" not in browser_pool.timing()


def test_a_hold_that_names_no_process_is_honoured(monkeypatch, tmp_path):
    _hold(monkeypatch, tmp_path, "")
    with pytest.raises(jobs.Busy, match="owner unknown"):
        with jobs.machine_lock("suite", wait=0.3):
            raise AssertionError("an empty hold was ignored")


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_verses_real_owner_line_names_its_pid_at_the_end(monkeypatch, tmp_path, other_job):
    _hold(monkeypatch, tmp_path, f"verse-perf 00:35:41: current-bank GPU confirm {other_job}\n")
    with pytest.raises(jobs.Busy, match="verse-perf"):
        with jobs.machine_lock("suite", wait=0.3):
            raise AssertionError("a live Verse hold was ignored")
    _hold(monkeypatch, tmp_path, f"verse-perf 00:35:41: current-bank GPU confirm {_dead_pid()}\n")
    with jobs.machine_lock("suite", wait=0.3):
        pass  # Verse's job is gone: its hold is stale


def test_a_line_with_two_numbers_names_no_process_and_is_honoured(monkeypatch, tmp_path, other_job):
    # Taking the first number as the pid would read a dead number here and skip a live hold.
    _hold(monkeypatch, tmp_path, f"look {_dead_pid()} started by {other_job}\n")
    with pytest.raises(jobs.Busy, match="started by"):
        with jobs.machine_lock("suite", wait=0.3):
            raise AssertionError("a hold with two numbers was skipped")


def test_reas_chain_hold_names_no_pid_and_only_its_own_tagged_runs_pass(monkeypatch, tmp_path):
    _hold(monkeypatch, tmp_path, "rea chain 1031fx 16:57:20: waits for the browser; no new look (early-hold)\n")
    for owner in (None, "rea chain 9999zz", "re"):  # untagged, another chain, a tag that is only part of a word
        if owner:
            monkeypatch.setenv("QAJEV_HOLD_OWNER", owner)
        with pytest.raises(jobs.Busy, match="rea chain 1031fx"):
            with jobs.machine_lock("look", wait=0.3):
                raise AssertionError(f"a run tagged {owner!r} passed rea's chain hold")
    monkeypatch.setenv("QAJEV_HOLD_OWNER", "rea chain 1031fx")
    with jobs.machine_lock("look", wait=0.3):
        assert "hold_waits" not in browser_pool.timing()


LOOK = """
import pathlib, sys
from qajev import browser_pool, jobs
tmp = pathlib.Path(sys.argv[1])
browser_pool.ROOT, jobs.LOCK, jobs.local_runs_off = tmp / "pool", tmp / "legacy.lock", lambda: False
try:
    with jobs.machine_lock("look", wait=0.5):
        print("admitted", flush=True)
except jobs.Busy as error:
    print("waited:", error, flush=True)
"""

OWNER = """
import os, subprocess, sys, time
hold, tmp, look = sys.argv[1:]
open(hold, "w").write(f"rea-look-chain {os.getpid()} 16:20:00\\n")
# the look runs a few processes below the hold's owner, as systemd-run --scope and bash put it on workload-gpu
out = subprocess.run(["bash", "-c", f'"{sys.executable}" -c "$0" "$1"; exit $?', look, tmp],  # no exec: bash stays
                     capture_output=True, text=True)
print("own look:", out.stdout.strip() or out.stderr.strip()[-300:], flush=True)
time.sleep(60)
"""


def test_a_look_under_the_holds_owner_runs_and_a_look_of_another_job_waits(monkeypatch, tmp_path):
    hold = tmp_path / "gpu-solo.hold"
    monkeypatch.setenv("QAJEV_HOLD_FILES", str(hold))
    owner = subprocess.Popen([sys.executable, "-c", OWNER, str(hold), str(tmp_path), LOOK],
                             stdout=subprocess.PIPE, text=True)
    assert owner.stdout is not None
    try:
        assert owner.stdout.readline().strip() == "own look: admitted"
        other = subprocess.run([sys.executable, "-c", LOOK, str(tmp_path)], capture_output=True, text=True)
        assert other.stdout.startswith("waited:"), other.stdout + other.stderr[-300:]
        assert f"rea-look-chain {owner.pid}" in other.stdout
    finally:
        owner.kill()
        owner.wait()


def test_every_listed_file_counts_and_a_missing_one_holds_nothing(monkeypatch, tmp_path, other_job):
    second = tmp_path / "tmp-copy.hold"
    _hold(monkeypatch, tmp_path, f"walk-run {other_job} 16:20:00\n", second)
    (tmp_path / "gpu-solo.hold").unlink()
    with jobs.machine_lock("suite", wait=0.3):
        pass  # neither file exists
    second.write_text(f"walk-run {other_job} 16:20:00\n")
    with pytest.raises(jobs.Busy, match="tmp-copy.hold"):
        with jobs.machine_lock("suite", wait=0.3):
            raise AssertionError("the second hold file was ignored")


def test_without_the_setting_a_hold_file_changes_nothing(tmp_path, other_job):
    (tmp_path / "gpu-solo.hold").write_text(f"walk-run {other_job} 16:20:00\n")
    with jobs.machine_lock("suite", wait=0.3):
        assert "hold_waits" not in browser_pool.timing()


def test_the_report_says_how_long_the_run_waited_and_for_whose_hold(tmp_path):
    data = report.build(type("S", (), {"name": "s"})(), [], [], browser={}, started_at=time.time(), strict=False,
                        interrupted=False, run_dir=tmp_path)
    data["timing"] = {"queue_seconds": 42.4, "run_seconds": 3.0, "hold_seconds": 41.9,
                      "hold_waits": [{"file": "/tmp/gpu-solo.hold", "owner": "walk-run 4242 16:20:00",
                                      "seconds": 41.9}]}
    line = "Waited 42 s before Chrome started, for another job's hold: /tmp/gpu-solo.hold (walk-run 4242 16:20:00)."
    assert line in report.markdown(data)
    assert report_html._e(line) in report_html.render(data)
    del data["timing"]["hold_waits"]
    assert "another job's hold" not in report.markdown(data)
