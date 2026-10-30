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
            base = CGROUP_ROOT.resolve()
            leaf = (base / group.lstrip("/")).resolve()
            if not leaf.is_relative_to(base):
                return None
            for root in [leaf, *leaf.parents]:
                if not root.is_relative_to(base):
                    break
                try:
                    maximum = (root / "memory.max").read_text().strip()
                except FileNotFoundError:
                    if root != base or root == leaf:
                        raise
                    # A native hierarchy root has no memory.max; finite ancestors still apply.
                    continue
                if maximum == "max":
                    continue
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


def hold_files() -> list[Path]:
    """QAJEV_HOLD_FILES: hold files (separated like PATH) that another job writes while it needs this machine alone."""
    return [Path(p).expanduser() for p in os.environ.get("QAJEV_HOLD_FILES", "").split(os.pathsep) if p.strip()]


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # another user's live process
        return True
    return True


def _ancestors():
    """This process and the ones it runs under: a hold one of them wrote is this run's own."""
    pids, pid = {os.getpid(), os.getppid()}, os.getppid()
    for _ in range(64):
        try:  # Linux; elsewhere only this process and its parent count
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if pid <= 1 or pid in pids:
            break
        pids.add(pid)
    return pids


def held() -> dict | None:
    """The first listed hold another live job owns, as {"file", "owner"}; None when none holds this machine.

    A hold's first line names its owner, "<job> <pid> <time>" (workload-gpu's gpu-solo.hold, 30 Oct). A hold whose
    process is gone is stale, and one this run or a process it runs under wrote is its own: neither waits. A hold
    that names no process, or cannot be read, is honoured."""
    mine = None
    for path in hold_files():
        try:
            line = (path.read_text(errors="replace").splitlines() or [""])[0].strip()[:200]
        except FileNotFoundError:
            continue
        except OSError:
            line = ""
        pid = next((int(word) for word in line.split() if word.isdigit()), None)
        if pid is not None:
            if not _alive(pid):
                continue
            mine = _ancestors() if mine is None else mine
            if pid in mine:
                continue
        return {"file": str(path), "owner": line or "owner unknown"}
    return None


def ram_capacity(requested: int, available: int | None, budget_mib: int, reserve_mib: int) -> int:
    if available is None:
        return min(requested, 1)
    return min(requested, max(0, (available - reserve_mib * MIB) // (budget_mib * MIB)))


def _memory_capacity(requested, available, budget_mib, reserve_mib, active):
    """Free RAM pays for the next process, not again for browsers already resident.

    Keep the initial envelope until the active cohort drains: simultaneous startups must not each spend
    the same free RAM before their browsers have allocated it. Older/unknown reservations stay serial.
    """
    if available is None or any(r.get("ram_envelope_bytes") is None for r in active):
        return 1, None
    envelope = min((r["ram_envelope_bytes"] for r in active), default=available)
    reserved = sum(r["ram_budget_mib"] for r in active) * MIB
    reserve_mib = max([reserve_mib, *(r["ram_reserve_mib"] for r in active)])
    remaining = min(available, max(0, envelope - reserved))
    capacity = len(active) + ram_capacity(requested, remaining, budget_mib, reserve_mib)
    return min(requested, capacity), envelope


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
    waits = lease.get("hold_waits")
    return {"queued_at": lease["queued_at"], "admitted_at": lease.get("admitted_at"),
            "queue_seconds": round((admitted if admitted is not None else now) - lease["queued_mono"], 3),
            "run_seconds": round(now - admitted, 3) if admitted is not None else 0.0,
            **({"hold_seconds": round(sum(w["seconds"] for w in waits), 3),
                "hold_waits": [dict(w) for w in waits]} if waits else {})}


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
             "ram_budget_mib": budget, "ram_reserve_mib": reserve,
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
        holding = None  # (the hold, when it was first seen, its wait record) while one keeps this run out
        while True:
            hold = held()
            if holding and hold != holding[0]:  # it went, or another job's hold took its place
                holding[2]["seconds"] = round(time.monotonic() - holding[1], 3)
                holding = None
            if hold and not holding:
                holding = (hold, time.monotonic(), {**hold, "started_at": time.time(), "seconds": 0.0})
                lease.setdefault("hold_waits", []).append(holding[2])
            with _guard():
                rows = _records()
                active = [row for _, row in rows if row["state"] == "active"]
                first = next((name for name, row in rows if row["state"] == "queued"), None)
                capacity = min([requested, *(r["limit"] for r in active)])
                memory, envelope = (_memory_capacity(requested, available_memory(), budget, reserve, active)
                                    if requested > 1 else (64, None))
                capacity = min(capacity, memory)
                admitted = hold is None and first == path.name and len(active) < capacity
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
                    lease.update(state="active", since=time.time(), admitted_at=time.time(), admitted_mono=now,
                                 ram_envelope_bytes=envelope)
                    _write(ticket, lease)
                    if requested == 1:
                        _write(legacy, lease)  # old clients still see the exclusive holder
                who = ", ".join(r["what"] for r in active) or "an earlier request or legacy QAJev run"
                reason = (f"another job holds this machine alone: {hold['file']} ({hold['owner']})" if hold else
                          f"queued behind {who}; capacity {capacity}/{requested}, memory capacity {memory}")
                if holding:
                    holding[2]["seconds"] = round(now - holding[1], 3)
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
