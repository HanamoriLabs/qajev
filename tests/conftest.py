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
