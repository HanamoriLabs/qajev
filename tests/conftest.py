import atexit
import os
import shutil
import tempfile

# Keep tests away from the person's ~/.qajev (profiles, Chrome state, MCP suites). Must run before qajev imports.
if "QAJEV_HOME" not in os.environ:
    _home = tempfile.mkdtemp(prefix="qajev-test-home-")
    os.environ["QAJEV_HOME"] = _home
    atexit.register(shutil.rmtree, _home, ignore_errors=True)

# Most tests check other contracts on one device; tests/test_devices.py clears this to test the desktop+phone default.
os.environ.setdefault("QAJEV_DEVICES", "desktop")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_parent_watch(monkeypatch):
    """A test is never orphaned. Without this, each `cli.main` run left a "qajev-parent-watch" thread looping on
    time.sleep for the rest of the session; a test that swapped time.sleep for a fake clock made those threads spin and
    move its clock, and CI hung 120 s (run 38284007154, test_idle_window). tests/test_parent_watch.py turns it on
    again."""
    monkeypatch.setenv("QAJEV_OUTLIVE_PARENT", "1")


@pytest.fixture(autouse=True)
def _no_cached_projects():
    """Each test reads the projects afresh. jobs._projects keeps them for the minute, so a test's own "shop" project
    on shop.example renamed a later test's run "shop" when both ran in the same minute (CI run 38431014320,
    macOS 3.12)."""
    from qajev import jobs

    jobs._projects.cache_clear()
