"""Offline source fault for the Cloud planning warning. Run through machine-lane."""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEST = "test_cloud_planning_warns_when_one_suite_mixes_local_and_public_cases"
BLOCK = '''    if (serve_root or tunnel or tunnel_port) and any(config.is_loopback(u) for u in case_targets) \\
            and any(not config.is_loopback(u) for u in case_targets):
        warnings.append("this suite mixes local served/tunnel cases with public URLs; Cloud keeps the network lock. "
                        "Split public cases into a separate run.")
'''


def main():
    source = (ROOT / "qajev/cloud.py").read_text()
    assert source.count(BLOCK) == 1
    results = []
    for fault in (False, True):
        with tempfile.TemporaryDirectory(prefix="qajev-mixed-fault-") as temp:
            for name in ("qajev", "tests"):
                shutil.copytree(ROOT / name, Path(temp, name), ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy2(ROOT / "pyproject.toml", Path(temp, "pyproject.toml"))
            if fault:
                Path(temp, "qajev/cloud.py").write_text(source.replace(BLOCK, "", 1))
            done = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_cloud.py", "-k", TEST],
                                  cwd=temp, capture_output=True, text=True, timeout=60, check=False)
            output = done.stdout + done.stderr
            ok = (done.returncode == 1 and "AssertionError" in output and "2 failed" in output and TEST in output) \
                if fault else done.returncode == 0 and "2 passed" in output
            results.append({"fault": "warning_removed" if fault else "baseline", "test": TEST,
                            "exit_code": done.returncode, "ok": ok})
            if not ok:
                print(output, file=sys.stderr)
    proof = {"results": results, "pass": all(r["ok"] for r in results), "live_calls": 0}
    print(json.dumps(proof, indent=2))
    raise SystemExit(0 if proof["pass"] else 1)


if __name__ == "__main__":
    main()
