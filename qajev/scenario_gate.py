"""Cooperative tab admission inside one run, including its supervising processes.

An exclusive owner keeps the turnstile while existing readers drain. File locks
belong to the supervisor, so killing a scenario cannot admit another tab before
its daemon and pending browser work have been cleaned up.
"""

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path


class AdmissionError(RuntimeError):
    pass


class Admission:
    """A nonblocking acquisition. Keep the same instance while waiting for a room."""

    def __init__(self, run_dir, exclusive=False):
        self.directory = Path(run_dir) / ".scenario-admission"
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.exclusive = exclusive
        self.turn: int | None = None
        self.room: int | None = None
        self.has_turn = self.has_room = False
        self.started = time.monotonic()
        try:
            self.turn = os.open(self.directory / "turn", os.O_CREAT | os.O_RDWR, 0o600)
            self.room = os.open(self.directory / "room", os.O_CREAT | os.O_RDWR, 0o600)
        except OSError:
            self.close()
            raise

    def _healthy(self):
        if (self.directory / "uncertain").exists():
            raise AdmissionError("scenario admission refused: earlier browser cleanup is uncertain")

    def poll(self):
        self._healthy()
        if self.has_room:
            return True
        if self.turn is None or self.room is None:
            raise AdmissionError("scenario admission is already closed")
        try:
            if not self.has_turn:
                fcntl.flock(self.turn, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.has_turn = True
            fcntl.flock(self.room, (fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            self.has_room = True
            # A failed reader writes the marker before releasing its room lock.
            self._healthy()
            if not self.exclusive:
                fcntl.flock(self.turn, fcntl.LOCK_UN)
                self.has_turn = False
            return True
        except BlockingIOError:
            return False

    def close(self):
        for name in ("room", "turn"):
            descriptor = getattr(self, name)
            if descriptor is not None:
                os.close(descriptor)  # Closing also releases locks after a cancelled admission.
                setattr(self, name, None)
        self.has_room = self.has_turn = False


def poison(run_dir):
    """Refuse new work after unconfirmed cleanup; never guess that the browser is idle."""
    directory = Path(run_dir) / ".scenario-admission"
    directory.mkdir(mode=0o700, exist_ok=True)
    descriptor = os.open(directory / "uncertain", os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)


@contextmanager
def admit(run_dir, exclusive=False):
    if run_dir is None:
        yield 0.0
        return
    admission = Admission(run_dir, exclusive)
    try:
        while not admission.poll():
            time.sleep(0.02)
        yield time.monotonic() - admission.started
    finally:
        admission.close()
