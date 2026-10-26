"""Restored worker-bootstrap faults; run in a network-isolated Linux CPU job."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REGRESSION = "tests/test_worker_routing.py::test_each_worker_uses_the_parent_provider_route[4]"
FAULTS = [
    ("W01-skip-bootstrap", '        provider_bootstrap()', '        pass', REGRESSION),
    ("W02-drop-bootstrap-argument", 'initargs=(shared, providers.WORKER_BOOTSTRAP)', 'initargs=(shared,)', REGRESSION),
    ("W03-drop-shared-cap", '    _shared_cost = shared', '    _shared_cost = None',
     "tests/test_worker_routing.py::test_default_worker_bootstrap_keeps_the_shared_cost"),
    ("W04-swallow-bootstrap-error", '        provider_bootstrap()',
     '        try:\n            provider_bootstrap()\n        except RuntimeError:\n            pass',
     "tests/test_worker_routing.py::test_worker_bootstrap_failure_is_not_ignored"),
]


def guard():
    assert sys.platform == "linux" and os.getpriority(os.PRIO_PROCESS, 0) == 19
    assert os.getloadavg()[1] <= 16 and shutil.disk_usage(Path.cwd()).free >= 20 * 1024**3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    guard()
    root = Path(__file__).resolve().parents[1]
    args.out.mkdir(parents=True, exist_ok=False)
    stage = args.out / "source"
    shutil.copytree(root, stage, ignore=shutil.ignore_patterns(
        ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"))
    target = stage / "qajev/runner.py"
    original = target.read_bytes()
    rows = []

    def run(name, test):
        guard()
        xml = args.out / (name + ".xml")
        env = {**os.environ, "PYTHONPATH": str(stage) + os.pathsep + str(stage / "tests")}
        with (args.out / (name + ".log")).open("w") as output:
            result = subprocess.run([sys.executable, "-m", "pytest", test, "-q", "--junitxml=" + str(xml)],
                                    cwd=stage, env=env, stdout=output, stderr=subprocess.STDOUT, timeout=120)
        cases = list(ET.parse(xml).iter("testcase"))
        failures = [c.find("failure") for c in cases if c.find("failure") is not None]
        assert cases and not any(c.find("error") is not None or c.find("skipped") is not None for c in cases)
        return {"exit": result.returncode, "failures": len(failures),
                "assertion": any("AssertionError" in (f.text or "") or "DID NOT RAISE" in (f.text or "")
                                 for f in failures)}

    try:
        for name, before, after, test in FAULTS:
            assert original.decode().count(before) == 1, name
            control = run(name + "-control", test)
            assert control["exit"] == 0 and control["failures"] == 0, control
            try:
                target.write_text(original.decode().replace(before, after, 1))
                fault = run(name + "-fault", test)
                assert fault["exit"] == 1 and fault["assertion"], fault
                rows.append({"fault": name, "test": test, "control": control, "fault_result": fault})
            finally:
                target.write_bytes(original)
    finally:
        target.write_bytes(original)
        (args.out / "RESULT.json").write_text(json.dumps({"caught": len(rows), "total": len(FAULTS),
            "sources_restored": target.read_bytes() == original, "matrix": rows,
            "source_sha256": hashlib.sha256(original).hexdigest()}, indent=2) + "\n")


if __name__ == "__main__":
    main()
