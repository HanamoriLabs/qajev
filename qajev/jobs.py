"""QAJev runs as jobs any agent or terminal on this machine can list, inspect and stop, and the machine-wide lock
that makes browser runs take turns (one at a time across every session, CLI and MCP alike).

A job is a folder under ~/.qajev/jobs/<id>/: job.json (what and who), events.jsonl (the run's progress events),
result.json (the final report JSON) and exit_code. A small shell wrapper runs `python -m qajev ... --json --events`
and records the exit code; stopping a job sends SIGTERM through the wrapper, and the run then closes its own tabs,
Chrome and daemon and keeps the scenarios that finished, exactly like Ctrl-C.
"""

import contextlib
import fcntl
import functools
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import yaml

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


# Folders named for their role, not the game: "<game>/godot", "<repo>/desktop", "out/ImHim-darwin-arm64"...
_GENERIC = {"godot", "game", "games", "project", "desktop", "app", "apps", "electron", "src", "client", "out", "build",
            "dist", "release", "web", "packages"}


def _flag(argv, *names):
    for name in names:
        if name in argv[:-1]:
            return argv[argv.index(name) + 1]
        for a in argv:
            if a.startswith(name + "="):
                return a.split("=", 1)[1]
    return None


@functools.lru_cache(maxsize=256)
def _suite_meta(path, _mtime):
    try:
        data = yaml.safe_load(Path(path).read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _suite(path):
    """A suite file's name: and adapter:, or {} (cached while the file is unchanged)."""
    try:
        return _suite_meta(str(path), Path(path).stat().st_mtime)
    except OSError:
        return {}


@functools.lru_cache(maxsize=8)
def _projects(_minute):
    from . import project

    try:
        return [(urlsplit(url).netloc, p["name"]) for p in project.listing() if not p.get("error")
                for url in (p.get("envs") or {}).values() if url]
    except Exception:  # a broken project file must not break `qajev jobs`
        return []


def _site(url):
    """A website as a person names it: the QAJev project it belongs to, else its host; plus a non-root path."""
    parts = urlsplit(url if "://" in url else f"https://{url}")
    host = parts.netloc or url
    name = next((n for h, n in _projects(int(time.time() // 60)) if h == host), host.removeprefix("www."))
    path = parts.path.rstrip("/")
    return f"{name} {path[:30]}" if path else name


def _game(target):
    """A game's own name from its path: an .app's name, else the nearest folder that is not a role or a worktree."""
    parts = [x for x in target.rstrip("/").split("/") if x]
    if ".claude" in parts:  # <repo>/.claude/worktrees/<branch>/...: the repo is the game
        parts = parts[:parts.index(".claude")] + [p for p in parts[parts.index(".claude") + 3:]]
    for part in reversed(parts):
        if part.endswith(".app"):
            return part.removesuffix(".app")
        if part.lower() not in _GENERIC and not re.search(r"-(darwin|linux|win32)-(arm64|x64)$", part):
            return part
    return parts[-1] if parts else "game"


def _goal(goal):
    """The goal's intention, without its "Stop when ..." ending."""
    first = re.split(r"(?<=[.!?])\s+|\s+Stop when\b", goal.strip(), maxsplit=1)[0].rstrip(".")
    return first[:48]


def describe_argv(argv, cwd=None):
    """A short title for a run that says what it is about: the project, site or game, then the test.
    "check foley /pricing · Find the Pro price", "play imhim · quit sends session_end", "run project shop · core"."""
    argv = [a for a in argv if a not in ("--background", "--json", "--events", "--quiet", "-q")]
    if not argv:
        return "qajev"
    cmd = argv[0]
    project = _flag(argv, "--project", "-p")
    if project:
        suite = _flag(argv, "--suite", "-s")
        return f"{cmd} project {project}" + (f" · {suite}" if suite else "")
    target = argv[1] if len(argv) > 1 and not argv[1].startswith("-") else ""
    suite_path = _flag(argv, "--suite")
    if suite_path and cwd:
        suite_path = str(Path(cwd, suite_path))  # relative to where the run started
    meta = _suite(suite_path) if suite_path else {}
    label = _flag(argv, "--name") or meta.get("name") or (
        Path(suite_path).stem if suite_path else None)
    if cmd == "play" and target:
        adapter = str(_flag(argv, "--adapter") or meta.get("adapter") or "")
        bundled = adapter and "/" not in adapter and not adapter.endswith((".gd", ".js"))  # suho, imhim...
        game = target[:60] if target.startswith(("ios:", "android:")) else adapter if bundled else _game(target)
        squash = lambda s: re.sub(r"[^a-z0-9]", "", str(s).lower())  # noqa: E731
        if label and squash(game) in squash(label):
            return f"play {label}"
        return f"play {game}" + (f" · {label}" if label else "")
    if cmd in ("check", "smoke") and target:
        goal = _flag(argv, "--goal", "-g")
        return f"{cmd} {_site(target)}" + (f" · {_goal(goal)}" if goal else "")
    if cmd == "run" and target:
        meta = _suite(Path(cwd, target) if cwd else target)
        return f"run {meta.get('name') or Path(target).stem}"
    return f"{cmd} {target}".strip()


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


def _title(meta):
    """An automatic title follows describe_argv, so older jobs get today's names too; a given title is kept."""
    argv = meta.get("argv") or []
    rerun = meta.get("rerun") or {}
    suffix = f" · rerun of {rerun['of']}{', failed only' if rerun.get('failed') else ''}" if rerun.get("of") else ""
    auto = meta.get("auto_title", bool(argv) and argv[0] in ("play", "check", "smoke")
                    and str(meta.get("title", "")).startswith(argv[0] + " "))
    if not auto:
        return meta["title"] + suffix
    try:
        return describe_argv(argv, cwd=meta.get("cwd")) + suffix
    except Exception:  # an odd old argv must not break `qajev jobs`
        return meta["title"] + suffix


def register(argv, title=None, *, rerun=None):
    """A run started in the foreground records itself as a job (no wrapper): the same files, written by the run
    itself, so `qajev jobs`, `qajev top` and `qajev stop` see and stop it like any other. -> the job folder.
    rerun: {"of": job id, "failed": bool} when it reruns a job (its title says so)."""
    folder = _new_folder()
    meta = {"id": folder.name, "title": title or describe_argv(argv), "auto_title": title is None, "argv": argv,
            "pid": os.getpid(),
            "started_at": time.time(), "cwd": os.getcwd(), "started_by": os.getppid(), "foreground": True,
            **({"rerun": rerun} if rerun else {})}
    (folder / "job.json").write_text(json.dumps(meta, indent=2))
    return folder


def start(argv, title=None, *, command=None, cwd=None, rerun=None):
    """Start `qajev ARGV` as a detached job; returns its status. `command` replaces the qajev command (tests);
    rerun as for register()."""
    folder = _new_folder()
    job_id = folder.name
    command = command or [sys.executable, "-m", "qajev", *argv, "--json", "--events"]
    env = {**os.environ, "QAJEV_JOB": job_id, "QAJEV_JOB_DIR": str(folder)}
    proc = subprocess.Popen(["/bin/sh", "-c", WRAPPER, "qajev-job", *command], cwd=cwd or os.getcwd(), env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    _children[job_id] = proc
    meta = {"id": job_id, "title": title or describe_argv(argv), "auto_title": title is None, "argv": argv,
            "pid": proc.pid,
            "started_at": time.time(), "cwd": cwd or os.getcwd(), "started_by": os.getpid(),
            **({"rerun": rerun} if rerun else {})}
    (folder / "job.json").write_text(json.dumps(meta, indent=2))
    return status(job_id)


def _reap_children():
    for job_id, proc in list(_children.items()):
        if proc.poll() is not None:
            del _children[job_id]


JOB_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")  # _new_folder's names: a job id is never a path


def _folder(job_id):
    """A job's folder. The id must be one of QAJev's own (an MCP client passes it): never a path elsewhere, whose
    job.json a rerun would replay."""
    if not JOB_ID.fullmatch(str(job_id)):
        raise NoSuchJob(job_id)
    return JOBS / job_id


def _meta(job_id):
    path = _folder(job_id) / "job.json"
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
    events, _ = _events(_folder(job_id))
    return events[seen:], len(events)


RERUN_OUTCOMES = ("fail", "stuck", "harness")


class NothingToRerun(ValueError):
    pass


def _without_only(argv):
    out, skip = [], False
    for item in argv:
        if skip:
            skip = False
        elif item == "--only":
            skip = True
        elif not item.startswith("--only="):
            out.append(item)
    return out


def rerun_argv(job_id, failed=False):
    """The command that runs a finished job again -> (argv, what it reruns, in words): the same command, or with
    `failed` only its tests that failed, got stuck or hit a harness limit (--only each; a test still brings what it
    depends on, and a game session its setup steps). Raises NoSuchJob; ValueError when the job is still going or
    is not a check, run or play; NothingToRerun when every test passed."""
    st = status(job_id, detail=True)
    if st["state"] in ("queued", "running"):
        raise ValueError(f"job {job_id} is still {st['state']}: rerun it once it has finished")
    argv = list(st.get("argv") or [])
    if not argv or argv[0] not in ("check", "run", "play"):
        raise ValueError(f"job {job_id} ran `qajev {' '.join(argv[:1])}`: only check, run and play jobs can be rerun")
    if not failed:
        return argv, "all of it"
    scenarios = (result(job_id) or {}).get("scenarios") or []
    names = [s["name"] for s in scenarios if s.get("outcome") in RERUN_OUTCOMES]
    if not names:
        raise NothingToRerun(f"nothing to rerun: job {job_id} has no failed, stuck or harness tests"
                             + ("" if scenarios else " (it has no results: rerun it without --failed)"))
    if argv[0] == "check":  # one test (and its device copies): it runs again as it was
        return argv, "its test that did not pass"
    return _without_only(argv) + [x for name in names for x in ("--only", name)], \
        f"its {len(names)} test(s) that did not pass"


def rerun_cwd(job_id):
    """The folder a job ran in: its rerun runs there too, so the command's relative paths (a game, a suite) mean the
    same files. None for a job that did not record one. ValueError when that folder no longer exists."""
    cwd = _meta(job_id).get("cwd")
    if cwd and not Path(cwd).is_dir():
        raise ValueError(f"job {job_id} ran in {cwd}, which no longer exists: start the run again by hand")
    return cwd


def decisions(job_id, limit=500):
    """The model's decisions in a job so far, oldest first: what it chose, how sure, the runner-up (qajev top `d`)."""
    events, _noise = _events(_folder(job_id))
    return [e for e in events if e.get("event") == "decision"][-limit:]


def result(job_id):
    """The run's final report JSON (or {"error": ...}), or None while it runs."""
    folder = _folder(job_id)
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
    folder = _folder(job_id)
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
        elif exit_code == 130:  # stopped (qajev stop, Ctrl-C) before its first report: nothing was judged
            final = {"gate": "INCOMPLETE", "error": "stopped before a report"}
    if exit_code is not None:
        state = "stopped" if meta.get("stop_requested") else "done" if final and "gate" in final else "failed"
    elif running:
        state = "queued" if not run and last.get("queued") else "running"
    else:
        state = "stopped" if meta.get("stop_requested") else "lost"  # killed without recording an exit code
    out = {
        "id": job_id, "title": _title(meta), "state": state, "pid": meta["pid"],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(meta["started_at"])),
        "seconds": round(_ended(folder, state) - meta["started_at"]),
        "run_dir": run.get("run_dir") or (final or {}).get("run_dir"),
        "progress": {"done": len(finished), "total": run.get("scenarios"),
                     "outcomes": {o: sum(1 for r in finished if r.get("outcome") == o)
                                  for o in {r.get("outcome") for r in finished}}},
    }
    if run.get("decider"):  # the model making the decisions: Jev, Clef or Clef-flash
        out["decider"] = run["decider"]
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
    (_folder(job_id) / "job.json").write_text(json.dumps(meta, indent=2))
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
