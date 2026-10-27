"""Plant UX wait regressions in a disposable checkout; prove controls and restore."""
import argparse
import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

CHECKS = "qajev/ux_checks.py"
OPTIONS = "qajev/ux_options.py"
FAULTS = [
    ("U01-ignore-cap", CHECKS,
     'timeout = min(timeout, self.config["ux_settle_timeout_ms"] / 1000)', 'timeout = timeout'),
    ("U02-omit-unsettled-note", CHECKS, 'self.note("page did not settle")', 'pass'),
    ("U03-skip-start-scan", CHECKS,
     'self.session.evaluate(SOURCE)\n        self.settle()\n        self.scan()',
     'self.session.evaluate(SOURCE)\n        self.settle()'),
    ("U04-skip-after-scan", CHECKS,
     '            self.scan()\n        finally:\n            self.in_action = False',
     '        finally:\n            self.in_action = False'),
    ("U05-skip-final-scan", CHECKS,
     'def finish(self):\n        self.settle()\n        self.scan()', 'def finish(self):\n        self.settle()'),
    ("U06-lengthen-internal-wait", CHECKS,
     'min(timeout, self.config["ux_settle_timeout_ms"] / 1000)', 'self.config["ux_settle_timeout_ms"] / 1000'),
    ("U07-change-default", OPTIONS, '"ux_settle_timeout_ms": 10000,', '"ux_settle_timeout_ms": 1000,'),
    ("U08-allow-over-limit", OPTIONS, 'if out.get("ux_settle_timeout_ms", 0) > 10000:', 'if False:'),
    ("U09-drop-run-option", "qajev/runner.py", '    ux_settle_timeout_ms: int | None = None\n', ''),
    ("U10-drop-mcp-option", "qajev/mcp_server.py", '    ux_settle_timeout_ms: int | None = None,\n', ''),
    ("U11-false-quiet", CHECKS, 'self.note("page did not settle")\n                return False',
     'self.note("page did not settle")\n                return True'),
]


def run(root, out, label):
    xml = out / (label + ".xml")
    with (out / (label + ".log")).open("w") as log:
        process = subprocess.run([sys.executable, "-m", "pytest", "tests/test_ux_wait.py",
                                  "tests/test_ux_options.py", "-q", "--junitxml=" + str(xml)],
                                 cwd=root, stdout=log, stderr=subprocess.STDOUT, timeout=120)
    cases = ET.parse(xml).getroot().findall(".//testcase")
    return {"exit": process.returncode, "tests": len(cases),
            "failed": [case.get("name") for case in cases if case.find("failure") is not None],
            "errors": sum(case.find("error") is not None for case in cases),
            "skips": sum(case.find("skipped") is not None for case in cases)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root, out = args.source.resolve(), args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    rows = []
    for name, filename, anchor, replacement in FAULTS:
        path = root / filename
        original = path.read_bytes()
        control = run(root, out, name + "-control")
        assert control["exit"] == 0 and not control["errors"] and not control["skips"], control
        source = original.decode()
        expected = 4 if name == "U10-drop-mcp-option" else 1
        assert source.count(anchor) == expected, (name, source.count(anchor))
        try:
            path.write_text(source.replace(anchor, replacement))
            fault = run(root, out, name + "-fault")
            assert fault["exit"] == 1 and fault["failed"] and not fault["errors"] and not fault["skips"], fault
        finally:
            path.write_bytes(original)
        restored = hashlib.sha256(path.read_bytes()).hexdigest()
        assert restored == hashlib.sha256(original).hexdigest()
        rows.append({"fault": name, "file": filename, "control": control, "fault_result": fault,
                     "caught": True, "restored_sha256": restored})
        (out / "receipt.json").write_text(json.dumps({"faults": rows}, indent=2) + "\n")
        print(name + " caught and restored", flush=True)


if __name__ == "__main__":
    main()
