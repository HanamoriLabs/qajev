import os
import subprocess
import sys
from pathlib import Path

import pytest

from qajev import lowprio


@pytest.mark.skipif(sys.platform == "win32", reason="no nice on Windows")
def test_a_started_program_runs_10_below_qajev():
    probe = lowprio.niced(["/bin/sh", "-c", "ps -o nice= -p $$"])
    out = subprocess.run(probe, capture_output=True, text=True, check=True)
    assert int(out.stdout.strip()) == min(20, os.nice(0) + 10)  # macOS caps at 20


def test_nothing_qajev_starts_uses_a_preexec_fn():
    # A preexec_fn runs Python in the forked child, which is unsafe with threads (the 10 Oct launch failures).
    users = [p.name for p in Path(lowprio.__file__).parent.glob("*.py") if "preexec_fn" in p.read_text()]
    assert users == ["lowprio.py"]  # only its own docstring names it


@pytest.mark.skipif(sys.platform == "win32", reason="no nice on Windows")
def test_a_launch_works_when_qajev_already_runs_at_the_lowest_priority():
    # The 10 Oct failure: machine-lane runs at nice 19, QAJev lowers itself to 20, and on macOS every os.nice() call
    # at 20 raises EPERM, so a preexec_fn os.nice(10) failed every Chrome start ("Exception occurred in preexec_fn").
    code = ("import subprocess; from qajev.lowprio import niced; "
            "print(subprocess.run(niced(['/usr/bin/true'])).returncode)")
    out = subprocess.run(["/usr/bin/nice", "-n", "20", sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=Path(lowprio.__file__).parent.parent)
    assert out.stdout.strip() == "0", out.stderr
