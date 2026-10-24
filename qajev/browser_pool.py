"""Per-user, machine-wide browser admission. A live flock is the lease, never a PID.

All QAJEV_HOME values in one /tmp namespace share this pool. FIFO tickets prevent a stream of parallel
requests starving exclusive desktop work. RAM limits new admissions only.
"""

import contextlib
import contextvars
import fcntl
import json
import os
import secrets
import stat
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(f"/tmp/qajev-browser-pool-{os.getuid()}")
_current: contextvars.ContextVar[dict | None] = contextvars.ContextVar("qajev_browser_lease", default=None)
MIB = 1024**2
MEMINFO = Path("/proc/meminfo")
CGROUP = Path("/proc/self/cgroup")
CGROUP_ROOT = Path("/sys/fs/cgroup")


class PoolBusy(RuntimeError):
    pass


def _integer(value, name, maximum):
    if isinstance(value, bool) or not str(value).isdigit() or not 1 <= int(value) <= maximum:
        raise ValueError(f"{name} must be an integer from 1 to {maximum}")
    return int(value)


def limit(value: int | str | None = None) -> int:
    return _integer(os.environ.get("QAJEV_BROWSER_RUNS", "1") if value is None else value,
                    "browser runs", 64)


def available_memory() -> int | None:
    """Available bytes, bounded by a Linux cgroup when present. Unknown never means unlimited."""
    if sys.platform == "linux":
        try:
            fields = dict(line.split(":", 1) for line in MEMINFO.read_text().splitlines())
            available = int(fields["MemAvailable"].split()[0]) * 1024
            # cgroup v2 limits can be much smaller than the host's MemAvailable.
            group = next(line.split("::", 1)[1] for line in CGROUP.read_text().splitlines()
                         if line.startswith("0::"))
            root = CGROUP_ROOT / group.lstrip("/")
            maximum = (root / "memory.max").read_text().strip()
            if maximum != "max":
                available = min(available, max(0, int(maximum) - int((root / "memory.current").read_text())))
            return available
        except (OSError, ValueError, KeyError, StopIteration):
            # Do not ignore an unreadable container limit and admit using the host's RAM.
            return None
    if sys.platform == "darwin":
        try:
            import re

            output = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=3, check=True).stdout
            match = re.search(r"page size of (\d+) bytes", output)
            if match is None:
                return None
            page_size = int(match[1])
            pages = dict(re.findall(r"^(Pages [\w ]+):\s+(\d+)\.", output, re.M))
            return sum(int(pages[k]) for k in ("Pages free", "Pages inactive", "Pages speculative")) * page_size
        except (OSError, subprocess.SubprocessError, TypeError, KeyError, ValueError):
            return None
    return None


def ram_capacity(requested: int, available: int | None, budget_mib: int, reserve_mib: int) -> int:
    if available is None:
        return min(requested, 1)
    return min(requested, max(0, (available - reserve_mib * MIB) // (budget_mib * MIB)))


def _directory():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = ROOT.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("browser pool must be an owned directory, never a link")
    ROOT.chmod(0o700)


def _open(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        os.close(fd)
        raise ValueError("browser pool files must be owned regular files")
    return os.fdopen(fd, "r+")


@contextlib.contextmanager
def _guard():
    _directory()
    with _open(ROOT / "admission.lock") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _records():
    """Called under the admission lock. Unlocked tickets are dead even if their PID was reused."""
    records = []
    for path in sorted(ROOT.glob("*.json")):
        with _open(path) as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                row = json.load(handle)
                records.append((path.name, row))
            else:
                path.unlink()
    return records


def holders() -> list[dict]:
    with _guard():
        return [row for _, row in _records() if row["state"] == "active"]


def timing() -> dict | None:
    lease = _current.get()
    if lease is None:
        return None
    now = time.monotonic()
    admitted = lease.get("admitted_mono")
    return {"queued_at": lease["queued_at"], "admitted_at": lease.get("admitted_at"),
            "queue_seconds": round((admitted if admitted is not None else now) - lease["queued_mono"], 3),
            "run_seconds": round(now - admitted, 3) if admitted is not None else 0.0}


def parallel() -> bool:
    lease = _current.get()
    return bool(lease and lease["limit"] > 1)


def _write(handle, row):
    handle.seek(0)
    handle.truncate()
    json.dump(row, handle)
    handle.flush()


@contextlib.contextmanager
def acquire(what, *, browser_runs=None, emit=None, wait: float = 3600, job=None, legacy_lock):
    requested = limit(browser_runs) if browser_runs is not None else 1  # native callers are exclusive
    budget = _integer(os.environ.get("QAJEV_BROWSER_RAM_MIB", "1024"), "browser RAM MiB", 1048576)
    reserve = _integer(os.environ.get("QAJEV_BROWSER_RESERVE_MIB", "2048"), "browser reserve MiB", 1048576)
    lease = {"pid": os.getpid(), "what": what, "job": job, "limit": requested, "state": "queued",
             "inherit_children": browser_runs is None,
             "queued_at": time.time(), "queued_mono": time.monotonic()}
    token = _current.set(lease)
    ticket = None
    legacy = None
    path = ROOT / f"{time.monotonic_ns():020d}-{secrets.token_hex(8)}.json"
    try:
        with _guard():
            ticket = _open(path)
            fcntl.flock(ticket, fcntl.LOCK_EX)
            _write(ticket, lease)
        last_notice = None
        while True:
            with _guard():
                rows = _records()
                active = [row for _, row in rows if row["state"] == "active"]
                first = next((name for name, row in rows if row["state"] == "queued"), None)
                capacity = min([requested, *(r["limit"] for r in active)])
                memory = ram_capacity(requested, available_memory(), budget, reserve) if browser_runs else 64
                capacity = min(capacity, memory)
                admitted = first == path.name and len(active) < capacity
                if admitted:
                    legacy_lock.parent.mkdir(parents=True, exist_ok=True)
                    legacy = open(legacy_lock, "a+")
                    try:
                        fcntl.flock(legacy, (fcntl.LOCK_SH if requested > 1 else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                    except BlockingIOError:
                        legacy.close()
                        legacy = None
                        admitted = False
                now = time.monotonic()
                if admitted:
                    lease.update(state="active", since=time.time(), admitted_at=time.time(), admitted_mono=now)
                    _write(ticket, lease)
                    if requested == 1:
                        _write(legacy, lease)  # old clients still see the exclusive holder
                who = ", ".join(r["what"] for r in active) or "an earlier request or legacy QAJev run"
                reason = f"queued behind {who}; capacity {capacity}/{requested}, memory capacity {memory}"
            if admitted:
                if callable(emit):
                    emit({"event": "admitted", "at": lease["admitted_at"], "capacity": capacity,
                          "browser_runs": requested, "timing": timing()})
                break
            if callable(emit) and (last_notice is None or now - last_notice >= 10):
                emit({"event": "waiting", "queued": True, "reason": reason,
                      "at": lease["queued_at"], "timing": timing()})
                last_notice = now
            if now - lease["queued_mono"] >= wait:
                raise PoolBusy(f"browser queue waited over {wait:.0f}s: {reason}")
            time.sleep(min(0.2, max(0, wait - (now - lease["queued_mono"]))))
        yield lease
    finally:
        measured = timing()
        try:
            if ticket is not None:
                with _guard():
                    path.unlink(missing_ok=True)
                    ticket.close()
            if legacy is not None:
                legacy.close()
        finally:
            _current.reset(token)
        if callable(emit):
            emit({"event": "released", "at": time.time(), "timing": measured})
