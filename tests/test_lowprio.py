import subprocess
import sys
from pathlib import Path

import pytest

from qajev import lowprio

ROOT = Path(lowprio.__file__).parent.parent
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="no nice on Windows")

# Started from a QAJev process at nice N: what nice its own process ends at, and what nice a program it starts runs at.
PROBE = ("import subprocess; from qajev import lowprio; lowprio.lower_self(); "
         "kid = subprocess.run(lowprio.niced(['/bin/sh', '-c', 'ps -o nice= -p $$']), capture_output=True, text=True); "
         "print(lowprio.current(), kid.returncode, kid.stdout.strip())")


@pytest.mark.parametrize("start, expect", [(0, 10), (10, 10), (19, 19), (20, 20)])
def test_qajev_goes_to_nice_10_only_from_above_and_always_starts_its_programs(start, expect):
    # 20 is the 10 Oct failure: machine-lane at 10, then QAJev's own +10. On macOS os.nice() raises EPERM there.
    base = lowprio.current()
    if start < base:
        pytest.skip(f"the tests run at nice {base}, so nice {start} cannot be reached without root")
    out = subprocess.run(["/usr/bin/nice", "-n", str(start - base), sys.executable, "-c", PROBE], capture_output=True,
                         text=True, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    me, kid_code, kid_nice = out.stdout.split()
    assert (int(me), int(kid_code), int(kid_nice)) == (expect, 0, expect)


def test_nothing_qajev_starts_uses_a_preexec_fn():
    # A preexec_fn runs Python in the forked child: os.nice there failed at nice 20, and it is unsafe with threads.
    users = [p.name for p in Path(lowprio.__file__).parent.glob("*.py") if "preexec_fn" in p.read_text()]
    assert users == ["lowprio.py"]  # only its own docstring names it
