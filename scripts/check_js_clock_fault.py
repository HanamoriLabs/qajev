#!/usr/bin/env python3
"""Prove the clock regression catches a genuinely frozen fixture; do not change the real page clock."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/test_js_runtime_live.py::test_unrounded_clock_and_timers_advance_inside_one_async_js_hook"


def main():
    with tempfile.TemporaryDirectory(prefix="qajev-clock-fault-") as folder:
        target = Path(folder)
        shutil.copytree(ROOT / "qajev", target / "qajev", ignore=shutil.ignore_patterns("__pycache__"))
        (target / "tests").mkdir()
        for filename in ("conftest.py", "test_js_runtime_live.py"):
            shutil.copyfile(ROOT / "tests" / filename, target / "tests" / filename)
        path = target / "tests/test_js_runtime_live.py"
        source = path.read_text()
        anchor = "window.total = 17;"
        assert source.count(anchor) == 1
        path.write_text(source.replace(anchor, anchor + "\nperformance.now = () => 2870;", 1))
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", TEST], cwd=target,
                                env={**os.environ, "QAJEV_LIVE": "1"}, capture_output=True, text=True, timeout=120)
        output = result.stdout + result.stderr
        caught = result.returncode == 1 and f"FAILED {TEST}" in output and "AssertionError" in output
        print(json.dumps({"fault": "frozen fixture clock", "caught": caught, "test": TEST}))
        if not caught:
            print(output)
        return 0 if caught else 1


if __name__ == "__main__":
    raise SystemExit(main())
