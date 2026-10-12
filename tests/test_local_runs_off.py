"""QAJEV_LOCAL_RUNS=off: this machine runs no QAJev test; runs go to QAJev Cloud (José, 12 Oct: "Why do I keep seeing
qajev running locally").

Every browser run here takes the machine lock (jobs.machine_lock), whatever started it: a check, a suite, a smoke, a
play, the nightly. With the setting off in QAJev's env file, the lock is refused before any browser starts. The file is
read at every run, so a shell cannot turn it back on for one run, and a long-lived process sees the change.
"""

import pytest

from qajev import config, jobs


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(jobs, "HOME", tmp_path)
    monkeypatch.setattr(jobs, "LOCK", tmp_path / "run.lock")
    monkeypatch.delenv("QAJEV_ENV_FILE", raising=False)
    monkeypatch.delenv("QAJEV_LOCAL_RUNS", raising=False)
    return path


def test_with_local_runs_off_no_run_takes_the_browser_here(env_file):
    env_file.write_text("QAJEV_RUNNER=cloud\nQAJEV_LOCAL_RUNS=off\n")
    with pytest.raises(jobs.LocalRunsOff, match="--runner cloud"), jobs.machine_lock("check x"):
        pytest.fail("a run started on this machine")


def test_the_shell_cannot_turn_it_back_on_and_the_file_is_read_at_each_run(env_file, monkeypatch):
    env_file.write_text("QAJEV_LOCAL_RUNS=off\n")
    monkeypatch.setenv("QAJEV_LOCAL_RUNS", "on")  # an old process's loaded copy, or a run that tries to bypass it
    with pytest.raises(jobs.LocalRunsOff), jobs.machine_lock("check x"):
        pass
    env_file.write_text("QAJEV_LOCAL_RUNS=on\n")
    monkeypatch.delenv("QAJEV_LOCAL_RUNS")
    with jobs.machine_lock("check x"):
        pass  # turned back on in the file: runs here again


def test_without_the_setting_runs_here_as_before(env_file):
    env_file.write_text("QAJEV_RUNNER=auto\n")
    with jobs.machine_lock("check x"):
        pass


def test_a_refused_run_is_busy_so_every_caller_reports_it_and_starts_nothing(env_file):
    env_file.write_text("QAJEV_LOCAL_RUNS=off\n")
    assert issubclass(jobs.LocalRunsOff, jobs.Busy)
