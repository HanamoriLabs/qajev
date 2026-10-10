"""Run what QAJev starts (Chrome, Electron, Godot, the Android emulator) below interactive work.

`/usr/bin/nice -n 10` execs the program, so the process id is the program's own. It replaces a `preexec_fn` that
called os.nice in the forked child: Python warns that a preexec_fn is unsafe in a process with threads, and QAJev has
several, so it failed now and then as "Exception occurred in preexec_fn" (verse-games, 10 Oct).
"""

import os
import sys

NICE = "/usr/bin/nice"


def niced(args):
    """`args` with `nice -n 10` in front, where the system has it."""
    if sys.platform == "win32" or not os.path.exists(NICE):
        return list(args)
    return [NICE, "-n", "10", *args]
