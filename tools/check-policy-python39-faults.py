"""Prove the Python 3.9 regression and restore two planted policy faults on a CPU box."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "qajev_upload_privacy.py"
FUTURE = "from __future__ import annotations\n"


def run_tests(python39, output):
    tests = "qcloud" if (ROOT / "qcloud").is_dir() else "tests"
    env = dict(os.environ, QAJEV_TEST_PYTHON39=str(python39))
    result = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", tests,
                             "-p", "test_policy_python39.py", "-v"],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=90)
    text = result.stdout + result.stderr
    output.write_text(text)
    return result.returncode, text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python39", required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != "linux" or os.environ.get("TWINBOX_BOXTEST") != "1":
        raise SystemExit("Run this fault proof on workload-cpu through boxtest")
    if Path("/srv/boxtest/work") not in ROOT.parents:
        raise SystemExit("Faults require a disposable boxtest source folder")
    out = ROOT / "python39-results"
    out.mkdir(exist_ok=False)
    original = POLICY.read_bytes()
    report = {"gate": "FAIL", "faults": [], "source_sha256": hashlib.sha256(original).hexdigest()}
    try:
        code, text = run_tests(args.python39, out / "control.log")
        assert code == 0 and "skipped=" not in text, "Python 3.9 control did not run and pass"
        report["control"] = "PASS"
        source = original.decode()
        assert source.count(FUTURE) == 1
        faults = [
            ("P39-1", "remove postponed annotations", source.replace(FUTURE, "", 1)),
            ("P39-2", "fix only the first union annotation",
             source.replace(FUTURE, "", 1).replace("keep: int | None", "keep: 'int | None'", 1)),
        ]
        for name, description, mutated in faults:
            try:
                POLICY.write_text(mutated)
                code, text = run_tests(args.python39, out / (name + ".log"))
                names = re.findall(r"^FAIL: (test_[A-Za-z0-9_]+)", text, re.M)
                assert code != 0 and names and "AssertionError" in text, name + " was not caught by a named assertion"
                report["faults"].append({"fault": name, "mutation": description, "exit": code,
                                         "named_tests": names, "caught": True})
            finally:
                POLICY.write_bytes(original)
        code, text = run_tests(args.python39, out / "restored.log")
        assert code == 0 and "skipped=" not in text
        assert POLICY.read_bytes() == original
        report.update(gate="PASS", source_restored=True, restored_control="PASS")
    finally:
        POLICY.write_bytes(original)
        (out / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print("PASS: Python 3.9 compatibility, 2/2 policy faults, exact source restored")


if __name__ == "__main__":
    main()
