"""Import the shared policy on the Python minor used by the launch agents."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

PYTHON39 = os.environ.get("QAJEV_TEST_PYTHON39") or shutil.which("python3.9")
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = r"""
import importlib
import importlib.util
from pathlib import Path
import sys

assert sys.version_info[:2] == (3, 9), "This regression must use Python 3.9"
policy = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("qajev_upload_privacy", policy)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
case = sys.argv[2]
if case == "import":
    assert callable(module.sanitize_text)
    assert module.request_mode("POST", "/api/v1/a-new-metadata-route") is None
elif case == "diagnostic":
    clean = module.sanitize_text("open /home/compat_user/work/qa.yaml is not a git root", keep=400, tail=True)
    assert "compat_user" not in clean
    assert clean.endswith("is not a git root")
elif case == "request":
    args = ["--expect-url", "/dashboard", "--expect-js", "location.pathname === '/done'"]
    files = [{"path": "in/qa.yaml", "size": 1, "sha256": "a" * 64}]
    cleaned = module.sanitize_request("POST", "/api/v1/runs", {
        "name": "source /home/compat_user/work/qa.yaml", "args": args, "files": files})
    assert cleaned["args"] == args
    assert cleaned["files"] == files
    assert "compat_user" not in cleaned["name"]
    metadata = module.sanitize_request("POST", "/api/v1/a-new-metadata-route", {
        "error": "open /home/compat_user/work/qa.yaml failed"})
    assert "compat_user" not in metadata["error"]
    assert metadata["error"].endswith("failed")
elif case == "cloud-imports":
    sys.path.insert(0, str(policy.parent))
    importlib.import_module("qcloud.dispatcher")
    importlib.import_module("qcloud.sync")
else:
    raise AssertionError("Unknown regression case")
print("POLICY_PYTHON39_PASS")
"""


@unittest.skipUnless(PYTHON39, "Python 3.9 executable is required; set QAJEV_TEST_PYTHON39")
class PolicyPython39Test(unittest.TestCase):
    def check_case(self, case):
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
        result = subprocess.run(
            [PYTHON39, "-I", "-B", "-c", SCRIPT, str(ROOT / "qajev_upload_privacy.py"), case],
            env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "POLICY_PYTHON39_PASS")

    def test_shared_policy_imports_on_python39(self):
        self.check_case("import")

    def test_python39_diagnostic_keeps_the_reason_without_the_home_path(self):
        self.check_case("diagnostic")

    def test_python39_request_policy_cleans_metadata_and_preserves_inputs(self):
        self.check_case("request")
