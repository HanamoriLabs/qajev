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
