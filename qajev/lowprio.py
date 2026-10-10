"""QAJev and what it starts (Chrome, Electron, Godot, the Android emulator) run at nice 10 or lower priority.

The rule (Orchestrator, 10 Oct): go to nice 10 only when the process is above that priority (nice below 10). At 10 or
more, change nothing. Never fail a run because of nice.

Before, QAJev lowered itself by 10 and then ran each child through a `preexec_fn` calling os.nice(10). Inside
machine-lane (nice 10) that reached 20, and on macOS every os.nice() call at 20 raises EPERM, so every Chrome start
failed as "Exception occurred in preexec_fn" (verse-games, 10 Oct). os.getpriority works at 20, and a child started at
10 or more simply inherits the priority. A preexec_fn is also unsafe in a process with threads, and QAJev has several.
"""

import os
import sys

TARGET = 10
NICE = "/usr/bin/nice"


def current():
    """This process's nice value, or None where it cannot be read."""
    try:
        return os.getpriority(os.PRIO_PROCESS, 0)
    except (OSError, AttributeError):
        return None


def lower_self():
    """Move this process to nice 10 if it runs above that priority. A failure is a warning, never an error."""
    now = current()
    if now is None or now >= TARGET:
        return
    try:
        os.setpriority(os.PRIO_PROCESS, 0, TARGET)
    except OSError as e:
        print(f"qajev: could not lower its priority ({e}); it runs at nice {now}", file=sys.stderr)


def niced(args):
    """`args`, started at nice 10 when this process runs above that priority; unchanged at 10 or more."""
    now = current()
    if sys.platform == "win32" or now is None or now >= TARGET or not os.path.exists(NICE):
        return list(args)
    return [NICE, "-n", str(TARGET - now), *args]
