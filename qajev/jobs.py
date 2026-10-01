"""QAJev runs as jobs any agent or terminal on this machine can list, inspect and stop, and the machine-wide lock
that makes browser runs take turns (one at a time across every session, CLI and MCP alike).

A job is a folder under ~/.qajev/jobs/<id>/: job.json (what and who), events.jsonl (the run's progress events),
result.json (the final report JSON) and exit_code. A small shell wrapper runs `python -m qajev ... --json --events`
and records the exit code; stopping a job sends SIGTERM through the wrapper, and the run then closes its own tabs,
Chrome and daemon and keeps the scenarios that finished, exactly like Ctrl-C.
"""

import contextlib
import fcntl
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import HOME

JOBS = HOME / "jobs"
LOCK = HOME / "run.lock"
KEEP_DAYS = 7

# Runs the command with its output in the job folder; forwards SIGTERM/SIGINT to it and waits, then records the
# exit code. (`wait` returns early when a trapped signal arrives, hence the loop.)
WRAPPER = """
"$@" >"$QAJEV_JOB_DIR/result.json" 2>"$QAJEV_JOB_DIR/events.jsonl" &
child=$!
trap 'kill -TERM $child 2>/dev/null' TERM INT
wait $child; code=$?
while kill -0 $child 2>/dev/null; do wait $child; code=$?; done
echo $code >"$QAJEV_JOB_DIR/exit_code"
"""

_children = {}  # job id -> Popen, so a long-lived parent (the MCP server) reaps its finished wrappers


class Busy(RuntimeError):
    """Another QAJev run kept the browser for longer than this run was willing to wait."""


class NoSuchJob(KeyError):
    pass


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # kill(pid, 0) proved it exists; only an explicit zombie state (exited, not yet reaped) counts as gone. An empty
    # answer (ps racing the exit, or failing on a loaded machine) is not proof of death: the next look decides.
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[-1].split()[0]  # Linux, even without procps
    except (OSError, IndexError):
        try:
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                                   timeout=5).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return True  # no ps here: kill(pid, 0) already showed the process exists
    return not state.startswith("Z")


def describe_argv(argv):
    """A short title for a run from its CLI arguments: "check https://…", "run project shop", "smoke …"."""
    argv = [a for a in argv if a not in ("--background", "--json", "--events", "--quiet", "-q")]
    if not argv:
        return "qajev"
    for flag in ("--project", "-p"):
        if flag in argv[:-1]:
            return f"{argv[0]} project {argv[argv.index(flag) + 1]}"
    target = argv[1] if len(argv) > 1 and not argv[1].startswith("-") else ""
    if argv[0] == "play" and target.startswith(("ios:", "android:")):  # a mobile app or page
        return f"play {target[:60]}"
    if argv[0] == "play" and target:  # a game: its folder (Godot projects often live in <game>/godot) and suite
        parts = [x for x in target.rstrip("/").split("/") if x]
        game = parts[-2] if len(parts) > 1 and parts[-1] in ("godot", "game", "project") else parts[-1]
        suite = argv[argv.index("--suite") + 1].rsplit("/", 1)[-1] if "--suite" in argv[:-1] else ""
        return f"play {game}" + (f" {suite.removesuffix('.yaml').removesuffix('.yml')}" if suite else "")
    if "/" in target and "://" not in target:
        target = target.rsplit("/", 1)[-1]  # a suite file: its name is enough
    return f"{argv[0]} {target}".strip()


# ---- the machine-wide lock ----

def holder():
    """Who holds the browser right now: {pid, what, since, job} or None."""
    if not LOCK.exists():
        return None
    with open(LOCK) as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                return json.loads(handle.read() or "{}")
            except json.JSONDecodeError:
                return {}
    return None


def _who(h):
    if not h:
        return "another QAJev run"
    since = time.strftime("%H:%M:%S", time.localtime(h["since"])) if h.get("since") else "?"
    job = f", job {h['job']}" if h.get("job") else ""
    return f"{h.get('what') or 'a QAJev run'} (pid {h.get('pid')}{job}, since {since})"


@contextlib.contextmanager
def machine_lock(what, emit=None, wait=None, job=None):
    """Hold the one browser slot on this machine; queue (and say so) while another run has it."""
    wait = float(os.environ.get("QAJEV_LOCK_WAIT", "3600")) if wait is None else wait
    HOME.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK, "a+")
    try:
        deadline, told = time.monotonic() + wait, None
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                now = time.monotonic()
                if now >= deadline:
                    raise Busy(f"another QAJev run kept the browser for over {wait:.0f}s: {_who(holder())}") from None
                if callable(emit) and (told is None or now - told >= 60):
                    emit({"event": "waiting", "queued": True, "reason": f"queued behind {_who(holder())}"})
                    told = now
                time.sleep(0.5)
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "what": what, "since": time.time(),
                                 "job": job or os.environ.get("QAJEV_JOB")}))
        handle.flush()
        yield
    finally:
        handle.close()  # closing releases the lock, also when the process dies


# ---- jobs ----

def _new_folder():
    JOBS.mkdir(parents=True, exist_ok=True)
    prune()
    folder = JOBS / (time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2))
    folder.mkdir()
    return folder


def register(argv, title=None):
    """A run started in the foreground records itself as a job (no wrapper): the same files, written by the run
    itself, so `qajev jobs`, `qajev top` and `qajev stop` see and stop it like any other. -> the job folder."""
    folder = _new_folder()
    meta = {"id": folder.name, "title": title or describe_argv(argv), "argv": argv, "pid": os.getpid(),
            "started_at": time.time(), "cwd": os.getcwd(), "started_by": os.getppid(), "foreground": True}
    (folder / "job.json").write_text(json.dumps(meta, indent=2))
    return folder


def start(argv, title=None, *, command=None, cwd=None):
    """Start `qajev ARGV` as a detached job; returns its status. `command` replaces the qajev command (tests)."""
    folder = _new_folder()
    job_id = folder.name
    command = command or [sys.executable, "-m", "qajev", *argv, "--json", "--events"]
    env = {**os.environ, "QAJEV_JOB": job_id, "QAJEV_JOB_DIR": str(folder)}
    proc = subprocess.Popen(["/bin/sh", "-c", WRAPPER, "qajev-job", *command], cwd=cwd or os.getcwd(), env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    _children[job_id] = proc
    meta = {"id": job_id, "title": title or describe_argv(argv), "argv": argv, "pid": proc.pid,
            "started_at": time.time(), "cwd": cwd or os.getcwd(), "started_by": os.getpid()}
    (folder / "job.json").write_text(json.dumps(meta, indent=2))
    return status(job_id)


def _reap_children():
    for job_id, proc in list(_children.items()):
        if proc.poll() is not None:
            del _children[job_id]


def _meta(job_id):
    path = JOBS / job_id / "job.json"
    if not path.is_file():
        raise NoSuchJob(job_id)
    return json.loads(path.read_text())


def _events(folder):
    events, noise = [], []
    path = folder / "events.jsonl"
    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                noise.append(line)
    return events, noise[-5:]


def events_since(job_id, seen):
    """New progress events of a job after the first `seen` ones -> (events, new seen)."""
    events, _ = _events(JOBS / job_id)
    return events[seen:], len(events)


def result(job_id):
    """The run's final report JSON (or {"error": ...}), or None while it runs."""
    folder = JOBS / job_id
    if not (folder / "exit_code").exists():
        return None
    text = (folder / "result.json").read_text(errors="replace").strip() if (folder / "result.json").exists() else ""
    try:
        return json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {}


def _ended(folder, state):
    """When the job ended (now, while it is queued or running): the exit code's time, else its last event's."""
    if state in ("queued", "running"):
        return time.time()
    for name in ("exit_code", "events.jsonl", "job.json"):
        if (folder / name).exists():
            return os.path.getmtime(folder / name)
    return time.time()


def status(job_id, detail=False):
    _reap_children()
    meta = _meta(job_id)
    folder = JOBS / job_id
    # Order matters, each look after the one it depends on: liveness, then the exit code (the wrapper writes it
    # before it exits, so a job ending between the two is not "lost"), then the events (all written before the
    # exit code, so a job seen "done" also has its last scenario).
    running = alive(meta["pid"])
    code_file = folder / "exit_code"
    exit_code = int(code_file.read_text().strip() or -1) if code_file.exists() else None
    final = result(job_id)
    events, noise = _events(folder)
    run = next((e for e in events if e.get("event") == "run"), {})
    finished = [e["result"] for e in events if e.get("event") == "scenario"]
    started = [e["scenario"] for e in events if e.get("event") == "start"]
    names = {r["name"] for r in finished}
    current = next((s for s in reversed(started) if s not in names), None)
    last = events[-1] if events else {}
    if exit_code is not None and not (final and "gate" in final):
        # No result.json (a run on QAJev's library reports through its events): its "done" event is the report, and
        # a clean exit without any report finished without proving anything; it did not fail.
        done = next((e for e in reversed(events) if e.get("event") == "done" and e.get("gate")), None)
        if done:
            final = {"gate": done["gate"], "run_dir": done.get("run_dir")}
        elif exit_code == 0:
            final = {"gate": "INCOMPLETE", "error": "finished without a report"}
    if exit_code is not None:
        state = "stopped" if meta.get("stop_requested") else "done" if final and "gate" in final else "failed"
    elif running:
        state = "queued" if not run and last.get("queued") else "running"
    else:
        state = "stopped" if meta.get("stop_requested") else "lost"  # killed without recording an exit code
    out = {
        "id": job_id, "title": meta["title"], "state": state, "pid": meta["pid"],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(meta["started_at"])),
        "seconds": round(_ended(folder, state) - meta["started_at"]),
        "run_dir": run.get("run_dir") or (final or {}).get("run_dir"),
        "progress": {"done": len(finished), "total": run.get("scenarios"),
                     "outcomes": {o: sum(1 for r in finished if r.get("outcome") == o)
                                  for o in {r.get("outcome") for r in finished}}},
    }
    if current and state == "running":
        out["current"] = current
        step = next((e for e in reversed(events) if e.get("event") == "step"), None)
        if step and step.get("scenario") == current:  # what Jev is doing right now
            out["now"] = {k: step.get(k) for k in ("scenario", "n", "doing", "p", "at")}
    spent = [e["spent_usd"] for e in events if isinstance(e.get("spent_usd"), (int, float))]
    if spent or finished:  # money spent so far: the run's ledger as of its last step, never less than its scenarios
        out["cost_usd"] = round(max(spent[-1] if spent else 0, sum(r.get("cost_usd") or 0 for r in finished)), 5)
    if last.get("event") == "waiting" and state in ("queued", "running"):
        out["waiting"] = last.get("reason")
    if final:
        out["gate"] = final.get("gate")
        if final.get("error"):
            out["error"] = final["error"]
    if exit_code is not None:
        out["exit_code"] = exit_code
    if state in ("failed", "lost") and not out.get("error") and noise:
        out["error"] = " | ".join(noise)
    if detail:
        out["argv"] = meta["argv"]
        out["scenarios"] = finished
        if final and "gate" in final:
            out["report"] = final
    return out


def listing(limit=20):
    if not JOBS.exists():
        return []
    metas = []
    for folder in JOBS.iterdir():
        try:
            metas.append(json.loads((folder / "job.json").read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    metas.sort(key=lambda m: (m.get("started_at", 0), m["id"]), reverse=True)  # ids share a second's timestamp
    return [status(m["id"]) for m in metas[:limit]]


def stop(job_id, wait=20.0):
    """Ask a job to stop (SIGTERM to the run, through its wrapper for background jobs; the run cleans up after
    itself as for Ctrl-C), then force it if needed."""
    meta = _meta(job_id)
    if not alive(meta["pid"]):
        return status(job_id)
    meta["stop_requested"] = time.time()
    (JOBS / job_id / "job.json").write_text(json.dumps(meta, indent=2))
    with contextlib.suppress(ProcessLookupError):
        os.kill(meta["pid"], signal.SIGTERM)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        _reap_children()
        if not alive(meta["pid"]):
            break
        time.sleep(0.25)
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if meta.get("foreground"):
                os.kill(meta["pid"], signal.SIGKILL)  # the run itself; the next run's reaper takes its Chrome
            else:
                os.killpg(meta["pid"], signal.SIGKILL)  # the job's own session: wrapper and run, not its Chrome
        time.sleep(0.5)
        _reap_children()
    return status(job_id)


def prune(days=KEEP_DAYS):
    """Forget finished jobs older than `days` (their run folders and reports stay where they are)."""
    if not JOBS.exists():
        return
    cutoff = time.time() - days * 86400
    for folder in JOBS.iterdir():
        try:
            meta = json.loads((folder / "job.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("started_at", 0) < cutoff and not alive(meta.get("pid")):
            shutil.rmtree(folder, ignore_errors=True)
