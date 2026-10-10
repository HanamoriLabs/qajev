"""Run what QAJev starts (Chrome, Electron, Godot, the Android emulator) below interactive work.

`/usr/bin/nice -n 10` execs the program, so the process id is the program's own. It replaces a `preexec_fn` that
called os.nice in the forked child. At nice 20 (machine-lane at 19, then QAJev's own +10) macOS refuses every
os.nice() call, so each Chrome start failed as "Exception occurred in preexec_fn" (verse-games, 10 Oct); nice(1) starts
the program anyway. A preexec_fn is also unsafe in a process with threads, and QAJev has several.
"""

import os
import sys

NICE = "/usr/bin/nice"


def niced(args):
    """`args` with `nice -n 10` in front, where the system has it."""
    if sys.platform == "win32" or not os.path.exists(NICE):
        return list(args)
    return [NICE, "-n", "10", *args]
