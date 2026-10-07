"""How QAJev's Chrome is started: on macOS a window the person did not ask for opens behind the app they are typing
in (LaunchServices `open -g`), and QAJev finds and owns the Chrome it started. No real Chrome runs here."""

import pytest

from qajev import chrome

APP = "/Applications/Google Chrome.app"
EXE = f"{APP}/Contents/MacOS/Google Chrome"


class FakeProc:
    def __init__(self, pid, code=None):
        self.pid, self.returncode = pid, code

    def poll(self):
        return self.returncode


@pytest.fixture
def launch(monkeypatch, tmp_path):
    """Run chrome._launch on a pretend Mac; returns (start, spawned argv list, niced pids)."""
    spawned, niced = [], []
    monkeypatch.setattr(chrome, "STATE", tmp_path / "state")
    monkeypatch.setattr(chrome, "binary", lambda: EXE)
    monkeypatch.setattr(chrome.sys, "platform", "darwin")
    monkeypatch.setattr(chrome, "version", lambda url, timeout=1.0: {"Browser": "Chrome"})
    monkeypatch.setattr(chrome.os, "setpriority", lambda which, pid, n: niced.append((pid, n)))

    def start(*, headless=False, visible=False, procs=None, open_code=0):
        profile_dir = tmp_path / "profile"
        marks = f"--user-data-dir={profile_dir} --remote-debugging-port=9351"
        rows = procs if procs is not None else [
            (100, f"/usr/bin/open -n -g -a {APP} --args {marks}"),  # the launcher: never Chrome itself
            (4242, f"{EXE} {marks} about:blank"),
            (4243, f"{EXE} --type=renderer {marks}")]  # a helper
        monkeypatch.setattr(chrome, "processes", lambda: rows)

        def popen(argv, **_kw):
            spawned.append(argv)
            return FakeProc(100, open_code) if argv[0] == "/usr/bin/open" else FakeProc(555)

        monkeypatch.setattr(chrome.subprocess, "Popen", popen)
        return chrome._launch("default", profile_dir, 9351, headless, visible, False, 2)

    return start, spawned, niced


def test_a_headed_chrome_on_macos_opens_behind_the_app_in_front(launch):
    start, spawned, niced = launch
    record = start()
    argv = spawned[0]
    # -n: never the person's Chrome; -g: not to the front; -j: hidden (else Chrome still took the front ~6 s)
    assert argv[:6] == ["/usr/bin/open", "-n", "-g", "-j", "-a", APP]
    chrome_args = argv[argv.index("--args") + 1:]  # Chrome's own flags, without its binary (open names the app)
    assert chrome_args[0].startswith("--user-data-dir=") and chrome_args[-1] == "about:blank"
    assert "--start-minimized" in chrome_args and EXE not in argv
    assert record["pid"] == 4242  # the browser process, not `open` (100) or a helper (4243)
    assert niced == [(4242, 10)]


def test_a_sign_in_window_and_a_headless_chrome_start_directly(launch):
    start, spawned, _ = launch
    assert start(visible=True)["pid"] == 555  # visible: a window the person asked for comes forward
    assert start(headless=True)["pid"] == 555  # headless: no window to take the keyboard
    assert all(argv[0] == EXE for argv in spawned)


def test_a_chrome_outside_an_app_bundle_starts_directly(launch, monkeypatch):
    start, spawned, _ = launch
    monkeypatch.setattr(chrome, "binary", lambda: "/usr/local/bin/chromium")
    assert start()["pid"] == 555 and spawned[0][0] == "/usr/local/bin/chromium"


def test_macos_refusing_to_start_chrome_is_an_error(launch):
    start, _, _ = launch
    with pytest.raises(chrome.ChromeError, match="open exited with 1"):
        start(procs=[], open_code=1)


def test_a_chrome_that_never_appears_times_out(launch):
    start, _, _ = launch
    with pytest.raises(chrome.ChromeError, match="did not open its debugging port"):
        start(procs=[])
