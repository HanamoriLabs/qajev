"""Linux behaviour found by the launch check in a python:3.12-slim container (Debian Chromium, procps-ng)."""

import os
from types import SimpleNamespace

from qajev import chrome, jobs, nightly
from qajev import session as S


def test_ps_asks_for_unlimited_width(monkeypatch):
    # procps-ng cuts `ps` at 80 columns when stdout is not a terminal (MCP server, background jobs, CI): the reaper then
    # never saw --user-data-dir / --remote-debugging-port and leaked orphaned Chromes.
    seen = []
    monkeypatch.setattr(chrome.subprocess, "run",
                        lambda args, **kw: seen.append(args) or SimpleNamespace(stdout="", returncode=0))
    chrome.cmdline(123)
    chrome.reap()
    assert seen[0][:2] == ["ps", "ww"]
    assert seen[1][:2] == ["ps", "-axww"]


def test_a_job_is_alive_without_ps(monkeypatch):
    # Slim images and minimal CI have no `ps` at all: a live run must not crash its own status check.
    def no_ps(*a, **kw):
        raise FileNotFoundError("ps")

    monkeypatch.setattr(jobs.subprocess, "run", no_ps)
    assert jobs.alive(os.getpid()) is True


def test_extra_chrome_flags_come_from_the_environment(monkeypatch):
    # Docker / Ubuntu 23.10+ CI: "No usable sandbox!" unless Chrome gets --no-sandbox, which QAJev never adds itself.
    monkeypatch.setenv("QAJEV_CHROME_FLAGS", "--no-sandbox --disable-dev-shm-usage")
    assert chrome.flags()[-2:] == ["--no-sandbox", "--disable-dev-shm-usage"]
    monkeypatch.delenv("QAJEV_CHROME_FLAGS")
    assert chrome.flags() == chrome.FLAGS


def test_nightly_uninstall_without_launchctl(monkeypatch, tmp_path):
    monkeypatch.setattr(nightly.shutil, "which", lambda name: None)
    monkeypatch.setattr(nightly, "PLIST", tmp_path / "none.plist")

    def boom(*a, **kw):
        raise AssertionError("launchctl must not be called where it does not exist")

    monkeypatch.setattr(nightly.subprocess, "run", boom)
    assert nightly.uninstall() is False


def test_arming_keeps_the_tab_in_front_and_downloads_denied():
    # Headless Linux Chromium gives a background tab no frames (each screenshot waited ~31 s), and another CDP session
    # in the same browser undid the download deny (an installer landed in ~/Downloads).
    calls = []
    s = S.Session.__new__(S.Session)
    s.headless, s.hosts, s.guard_opts, s.guard_cfg, s.script_id = True, set(), {}, None, None
    s.call = lambda method, **p: calls.append(method) or {"identifier": "1"}
    s.jev = SimpleNamespace(cdp=lambda method, **p: calls.append(method) or {})
    s.evaluate = lambda *a, **kw: None
    s.arm("readonly")
    assert "Page.bringToFront" in calls and "Browser.setDownloadBehavior" in calls
    calls.clear()
    s.arm("readonly")  # same guard: still re-asserted
    assert "Page.bringToFront" in calls and "Browser.setDownloadBehavior" in calls
    calls.clear()
    s.headless = False  # a visible window is never pulled to the front
    s.arm("readonly")
    assert "Page.bringToFront" not in calls


def test_processes_come_from_proc_when_ps_is_missing(monkeypatch, tmp_path):
    # Linux launch re-run, python:3.12-slim without procps: the reaper listed no processes and stopped nothing.
    def no_ps(*a, **kw):
        raise FileNotFoundError("ps")

    (tmp_path / "4242").mkdir()
    argv = b"/usr/bin/chromium\0--user-data-dir=/x\0--remote-debugging-port=9333\0"
    (tmp_path / "4242" / "cmdline").write_bytes(argv)
    (tmp_path / "4242" / "environ").write_bytes(b"HOME=/root\0BU_NAME=qajev-77\0")
    (tmp_path / "self").mkdir()
    monkeypatch.setattr(chrome.subprocess, "run", no_ps)
    monkeypatch.setattr(chrome, "PROC", tmp_path)
    assert chrome.processes() == [(4242, "/usr/bin/chromium --user-data-dir=/x --remote-debugging-port=9333")]
    assert chrome.cmdline(4242) == "/usr/bin/chromium --user-data-dir=/x --remote-debugging-port=9333"
    assert chrome._daemon_name(4242) == "qajev-77"
