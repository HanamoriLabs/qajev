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
