"""qajev dashboard: every QAJev run on this machine (any agent's) in one local web page. Filter runs by project,
kind and result, open a run to see each test with its checks, findings, screenshots and the model's decisions, and
stop, rerun or start runs. It serves 127.0.0.1 only, behind a key in its URL (José, 3 Oct: "a dashboard we can
control everything, where the testing can store images as the testing happens, that we can dig into each test.
filter the tests by project")."""

import contextlib
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import shlex
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import jobs, live

PAGE = Path(__file__).with_name("dashboard.html")
STATE = jobs.JOBS.parent / "dashboard.json"  # the running dashboard: pid, port and key (0600)
KEY = jobs.JOBS.parent / "dashboard.key"  # its key, kept across restarts so an open tab keeps working (0600)
# QAJev's own logo files, copied from the repo's brand files (docs/images/logo-dark.svg, site/favicon.svg): never
# redrawn. A test checks the copies still match.
ASSETS = {"/logo.svg": Path(__file__).with_name("assets") / "logo-dark.svg",
          "/favicon.svg": Path(__file__).with_name("assets") / "mark.svg"}
FILE_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".html", ".json", ".md", ".txt", ".mp4", ".webm"}
FINISHED = ("done", "stopped", "failed", "lost")
STEP_KEYS = ("name", "about", "outcome", "reason", "stop", "seconds", "cost_usd", "goal", "checks", "findings",
             "end_url", "page_says", "history", "fps")


class DashboardError(ValueError):
    """A request the dashboard refuses: its message is for the person."""


# ---- the runs ----

_finished = {}  # job id -> (job.json mtime, row): a finished job never changes, so it is read once
_lock = threading.Lock()


def _epoch(stamp):
    try:
        return datetime.fromisoformat(str(stamp)).timestamp()
    except ValueError:
        return 0.0


def _file_url(run_id, rel):
    return f"/files/{run_id}/{rel}" if rel else None


def _job_row(job_id):
    meta_path = jobs.JOBS / job_id / "job.json"
    mtime = meta_path.stat().st_mtime
    with _lock:
        cached = _finished.get(job_id)
    if cached and cached[0] == mtime:
        return cached[1]
    st = jobs.status(job_id, detail=True)
    meta = json.loads(meta_path.read_text())
    report = st.get("report") or {}
    scenarios = report.get("scenarios") or st.get("scenarios") or []
    shot = next((s["shot"] for s in reversed(scenarios) if s.get("shot")), None)
    run_dir = st.get("run_dir")
    row = {
        "folder": str(Path(meta.get("cwd") or ".", run_dir).resolve()) if run_dir else None,
        "id": job_id, "source": "job", "title": st["title"], "subject": jobs.subject(meta.get("argv"), meta.get("cwd")),
        "command": (meta.get("argv") or ["?"])[0], "state": st["state"], "gate": st.get("gate"),
        "started": meta.get("started_at"), "seconds": st.get("seconds"), "cost_usd": st.get("cost_usd"),
        "progress": st.get("progress"), "decider": st.get("decider"), "current": st.get("current"),
        "now": st.get("now"), "waiting": st.get("waiting"), "error": st.get("error"),
        "rerun": meta.get("rerun"), "thumb": _file_url(job_id, shot),
        "failed": sum(1 for s in scenarios if s.get("outcome") in jobs.RERUN_OUTCOMES),
    }
    if st["state"] in FINISHED:
        with _lock:
            _finished[job_id] = (mtime, row)
    return row


def _report_rows(skip, limit):
    """Project runs filed in a project's reports with no job behind them (or whose job was pruned)."""
    from . import project as project_mod

    try:
        index = project_mod.index_rows(None, limit)
    except (OSError, ValueError):
        return []
    rows = []
    for r in index:
        folder = Path(r["report"]).parent.resolve()
        if folder in skip:
            continue
        outcomes = {}
        for o in r.get("objectives") or []:
            outcomes[o["outcome"]] = outcomes.get(o["outcome"], 0) + 1
        smoke = "-smoke-" in folder.name
        rows.append({
            "id": _report_id(folder), "source": "report", "folder": str(folder),
            "title": f"{r['project']} {r['env']}{' smoke' if smoke else ''}", "subject": r["project"],
            "command": "smoke" if smoke else "run", "state": "done", "gate": r.get("gate"),
            "started": _epoch(r.get("when")), "seconds": round(r.get("seconds") or 0), "cost_usd": r.get("cost_usd"),
            "progress": {"done": sum(outcomes.values()), "total": sum(outcomes.values()), "outcomes": outcomes},
            "failed": sum(n for o, n in outcomes.items() if o in jobs.RERUN_OUTCOMES), "thumb": None,
        })
    return rows


def _report_id(folder):
    return "r-" + hashlib.sha1(str(folder).encode()).hexdigest()[:12]


def runs(limit=400):
    """Every run, newest first: QAJev's jobs (any agent's, queued, running or finished) and the project runs filed
    without one."""
    rows = []
    if jobs.JOBS.exists():
        for folder in jobs.JOBS.iterdir():
            if jobs.JOB_ID.fullmatch(folder.name) and (folder / "job.json").exists():
                with contextlib.suppress(OSError, ValueError, KeyError, jobs.NoSuchJob):
                    rows.append(_job_row(folder.name))
    rows.sort(key=lambda r: r.get("started") or 0, reverse=True)
    rows = rows[:limit]
    rows += _report_rows({Path(r["folder"]) for r in rows if r.get("folder")}, limit)
    rows.sort(key=lambda r: r.get("started") or 0, reverse=True)
    return rows[:limit]


def state(limit=400):
    rows = runs(limit)
    today = time.strftime("%Y-%m-%d")
    todays = [r for r in rows if time.strftime("%Y-%m-%d", time.localtime(r.get("started") or 0)) == today]
    subjects = {}
    for r in rows:
        if r.get("subject"):
            subjects[r["subject"]] = subjects.get(r["subject"], 0) + 1
    return {
        "at": time.time(), "load": round(os.getloadavg()[0], 1), "holder": jobs.holder(), "runs": rows,
        "subjects": sorted(subjects.items(), key=lambda kv: (-kv[1], kv[0])),
        "today": {"runs": len(todays), "cost_usd": round(sum(r.get("cost_usd") or 0 for r in todays), 4),
                  "gates": {g: sum(1 for r in todays if r.get("gate") == g) for g in ("PASS", "FAIL", "INCOMPLETE")}},
    }


def _folder(run_id):
    """A run's folder (where its report and screenshots are), or None before the run has one."""
    if run_id.startswith("r-"):
        row = next((r for r in _report_rows(set(), 1000) if r["id"] == run_id), None)
        if row is None:
            raise jobs.NoSuchJob(run_id)
        return Path(row["folder"])
    return jobs.run_folder(run_id)


def _step(run_id, s):
    out = {k: s.get(k) for k in STEP_KEYS if s.get(k) not in (None, [], {}, "")}
    if out.get("history"):
        out["history"] = out["history"][-300:]
    if out.get("page_says"):
        out["page_says"] = str(out["page_says"])[:2000]
    out["shot"] = _file_url(run_id, s.get("shot"))
    return out


def detail(run_id):
    """One run: what it ran, how it went, and each test with its checks, findings, screenshot and decisions."""
    folder = _folder(run_id)
    report = None
    if folder and (folder / "report.json").is_file():
        with contextlib.suppress(OSError, json.JSONDecodeError):
            report = json.loads((folder / "report.json").read_text())
    out = {"id": run_id, "folder": str(folder) if folder else None,
           "report_html": _file_url(run_id, "report.html") if folder and (folder / "report.html").is_file() else None}
    if run_id.startswith("r-"):
        row = next(r for r in _report_rows(set(), 1000) if r["id"] == run_id)
        out.update(row)
        scenarios, decisions = (report or {}).get("scenarios") or [], []
    else:
        st = jobs.status(run_id, detail=True)
        meta = jobs._meta(run_id)
        out.update(_job_row(run_id))
        out.update({"command_line": "qajev " + shlex.join(meta.get("argv") or []), "cwd": meta.get("cwd")})
        scenarios = (report or {}).get("scenarios") or st.get("scenarios") or []
        decisions = jobs.decisions(run_id, limit=5000)
        if st["state"] not in FINISHED:
            out.update(_live(run_id, folder, st.get("current")))
    if report:
        out["models"] = report.get("models")
        if report.get("about"):
            out["about"] = report["about"]
    by_step = {}
    for d in decisions:
        by_step.setdefault(d.get("scenario"), []).append(
            {k: d.get(k) for k in ("at", "screen", "chose", "operation", "p", "runner_up", "runner_up_p", "ms",
                                   "stale")})
    out["steps"] = [{**_step(run_id, s), "decisions": by_step.get(s.get("name"), [])} for s in scenarios]
    current = out.get("current")
    if current and not any(s["name"] == current for s in out["steps"]):  # the test running now
        out["steps"].append({"name": current, "outcome": "running", "reason": (out.get("now") or {}).get("doing"),
                             "decisions": by_step.get(current, [])})
    return out


def _live(run_id, folder, current):
    """A running job's screen right now (while someone watches it: live.py) and what its test did last."""
    out = {}
    shot = live.frame(folder) if folder else None
    if shot:
        out["live"] = {"shot": f"{_file_url(run_id, 'live/frame.jpg')}?t={shot[1]:.3f}", "at": shot[1]}
    events, _ = jobs.events_since(run_id, 0)
    steps = [{"at": e.get("at"), "doing": e.get("doing")} for e in events
             if e.get("event") == "step" and e.get("scenario") == current and e.get("doing") and not e.get("pulse")]
    out["activity"] = steps[-8:]
    return out


def _signature(run_id, folder):
    """What changes when a running job does something: its events, its frame, its end."""
    base = jobs.JOBS / run_id
    parts = []
    for path in (base / "events.jsonl", base / "result.json", base / "exit_code",
                 *([live.folder(folder) / "frame.jpg"] if folder else [])):
        try:
            st = path.stat()
            parts.append((st.st_size, st.st_mtime_ns))
        except OSError:
            parts.append(None)
    return tuple(parts)


def run_file(run_id, rel):
    """A file of a run (a screenshot, its report): only from inside that run's own folder."""
    folder = _folder(run_id)
    if not folder:
        raise jobs.NoSuchJob(run_id)
    base = folder.resolve()
    path = (base / unquote(rel)).resolve()
    if not path.is_relative_to(base) or path.suffix.lower() not in FILE_TYPES or not path.is_file():
        raise jobs.NoSuchJob(rel)
    return path


# ---- what a person can do from the page ----

def stop(run_id):
    return jobs.stop(run_id, wait=5)


def rerun(run_id, failed):
    argv, what = jobs.rerun_argv(run_id, failed=failed)
    if "--allow-commands" in argv:  # the person's permission for that terminal run, not this page's
        raise DashboardError(f"job {run_id} ran shell commands (--allow-commands): rerun it in a terminal")
    job = jobs.start(argv, cwd=jobs.rerun_cwd(run_id), rerun={"of": run_id, "failed": failed})
    return {"job": job["id"], "title": job["title"], "rerun": what}


def projects():
    from . import project as project_mod

    out = []
    for p in project_mod.listing():
        if p.get("error"):
            continue
        with contextlib.suppress(project_mod.ProjectError, OSError):
            full = project_mod.load(Path(p["config"]))
            out.append({"name": p["name"], "envs": list(p.get("envs") or {}), "default_env": full.default_env,
                        "objectives": [o.get("name") for o in full.objectives if o.get("name")]})
    return out


def start_project(name, env=None, objective=None):
    known = {p["name"]: p for p in projects()}
    if name not in known:
        raise DashboardError(f"no project {name!r}")
    if env and env not in known[name]["envs"]:
        raise DashboardError(f"project {name} has no env {env!r}")
    if objective and objective not in known[name]["objectives"]:
        raise DashboardError(f"project {name} has no objective {objective!r}")
    argv = ["run", "--project", name, *(["--env", env] if env else []), *(["--objective", objective] if objective
                                                                           else [])]
    job = jobs.start(argv)
    return {"job": job["id"], "title": job["title"]}


# ---- the server ----

class Handler(BaseHTTPRequestHandler):
    server_version = "qajev-dashboard"
    key = ""
    port = 0

    def log_message(self, format, *args):  # noqa: A002 (the base class's name)
        pass

    def _host_ok(self):
        # A page elsewhere that points its own name at 127.0.0.1 (DNS rebinding) still sends that name as Host.
        return self.headers.get("Host", "") in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def _cookie_ok(self):
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "qajev_dashboard" and hmac.compare_digest(value, self.key):
                return True
        return False

    def _send(self, code, body, kind="application/json", headers=()):
        data = body if isinstance(body, bytes) else (
            json.dumps(body, default=str).encode() if kind == "application/json" else body.encode())
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        if not any(name == "Cache-Control" for name, _ in headers):
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code, message):
        self._send(code, {"error": message})

    def do_GET(self):  # noqa: N802 (the base class's name)
        if not self._host_ok():
            return self._error(403, "wrong host")
        url = urlsplit(self.path)
        if url.path == "/" and hmac.compare_digest(parse_qs(url.query).get("k", [""])[0], self.key):
            # The key from the printed URL becomes a cookie, and leaves the address bar.
            return self._send(303, b"", "text/plain", [
                ("Location", "/"), ("Set-Cookie", f"qajev_dashboard={self.key}; HttpOnly; SameSite=Strict; Path=/")])
        if not self._cookie_ok():
            return self._send(401, "Open the address `qajev dashboard` printed (it carries the key).", "text/plain")
        try:
            if url.path == "/":
                return self._send(200, PAGE.read_text().replace("__QAJEV_KEY__", self.key), "text/html; charset=utf-8",
                                  [("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; "
                                    "style-src 'unsafe-inline' https://fonts.googleapis.com; "
                                    "font-src https://fonts.gstatic.com; script-src 'unsafe-inline'; "
                                    "frame-ancestors 'none'")])
            if url.path in ASSETS:
                return self._send(200, ASSETS[url.path].read_bytes(), "image/svg+xml",
                                  [("Cache-Control", "private, max-age=86400")])
            if url.path == "/api/state":
                return self._send(200, state())
            if url.path == "/api/projects":
                return self._send(200, projects())
            if url.path.startswith("/api/run/"):
                return self._send(200, detail(url.path.removeprefix("/api/run/")))
            if url.path.startswith("/api/stream/"):
                return self._stream(url.path.removeprefix("/api/stream/"))
            if url.path.startswith("/files/"):
                run_id, _, rel = url.path.removeprefix("/files/").partition("/")
                path = run_file(run_id, rel)
                kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                extra = [("Content-Security-Policy", "sandbox; default-src 'none'; img-src 'self' data:; "
                          "style-src 'unsafe-inline'")] if kind == "text/html" else []  # a report never runs scripts
                # A run's files do not change once written: the page refreshes every few seconds without fetching them
                return self._send(200, path.read_bytes(), kind, [*extra, ("Cache-Control", "private, max-age=3600")])
        except (jobs.NoSuchJob, StopIteration):
            return self._error(404, "no such run or file")
        except DashboardError as e:
            return self._error(400, str(e))
        self._error(404, "not found")

    def _stream(self, run_id):
        """Server-sent events for one running job: "change" whenever it does something (a step, a test, a new frame
        of its screen), "done" when it ends. While the stream is open the run counts as watched, so it saves frames."""
        # A job id, never a path: checked before it is joined to the jobs folder, as jobs._folder does.
        if not jobs.JOB_ID.fullmatch(run_id) or not (jobs.JOBS / run_id / "job.json").is_file():
            raise jobs.NoSuchJob(run_id)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        last, touched, quiet, checked, folder = None, 0.0, time.monotonic(), time.monotonic(), None
        try:
            while True:
                done = (jobs.JOBS / run_id / "exit_code").is_file()
                if not done and time.monotonic() - checked > 2:  # a run whose process died leaves no exit code
                    done, checked = jobs.status(run_id)["state"] in FINISHED, time.monotonic()
                folder = folder or (None if done else jobs.run_folder(run_id))  # found once: it does not move
                if folder and not done and time.monotonic() - touched > live.INTERVAL:
                    live.touch(folder)
                    touched = time.monotonic()
                sig = _signature(run_id, folder)
                if sig != last or done:
                    self.wfile.write(b"data: done\n\n" if done else b"data: change\n\n")
                    self.wfile.flush()
                    last, quiet = sig, time.monotonic()
                    if done:
                        return
                elif time.monotonic() - quiet > 15:  # a comment keeps proxies and the browser from closing it
                    self.wfile.write(b": still here\n\n")
                    self.wfile.flush()
                    quiet = time.monotonic()
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError):
            return  # the page went away

    def do_POST(self):  # noqa: N802
        # An action needs the key the page carries in a header: a page elsewhere has the cookie sent, never the key.
        if not (self._host_ok() and self._cookie_ok()
                and hmac.compare_digest(self.headers.get("X-QAJev-Key", ""), self.key)):
            return self._error(403, "forbidden")
        try:  # a negative length would make read() wait for the socket to close, holding a thread
            length = max(0, min(int(self.headers.get("Content-Length") or 0), 10_000))
        except ValueError:
            return self._error(400, "bad Content-Length")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._error(400, "bad JSON")
        if not isinstance(body, dict):
            return self._error(400, "the body must be a JSON object")
        parts = urlsplit(self.path).path.strip("/").split("/")
        try:
            if parts[:2] == ["api", "run"] and len(parts) == 4 and parts[3] == "stop":
                return self._send(200, stop(parts[2]))
            if parts[:2] == ["api", "run"] and len(parts) == 4 and parts[3] == "rerun":
                return self._send(200, rerun(parts[2], bool(body.get("failed"))))
            if parts[:2] == ["api", "projects"] and len(parts) == 4 and parts[3] == "run":
                return self._send(200, start_project(unquote(parts[2]), body.get("env") or None,
                                                     body.get("objective") or None))
        except jobs.NoSuchJob:
            return self._error(404, "no such run")
        except jobs.NothingToRerun as e:
            return self._error(409, str(e))
        except ValueError as e:  # DashboardError, and a job still running or of a kind that cannot rerun
            return self._error(400, str(e))
        self._error(404, "not found")


def claim():
    """The one-dashboard lock, held for this process's life (the OS frees it when the process ends, however it ends):
    its file descriptor, or None while another dashboard holds it. Two starts at once no longer make two."""
    import fcntl

    STATE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(STATE.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def wait_running(timeout=15.0, starting=None):
    """The dashboard once it serves (another process is starting it), or None after `timeout` seconds, or as soon
    as `starting` (the Popen starting it) has exited."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        there = running()
        if there:
            return there
        if starting is not None and starting.poll() is not None:
            return None
        time.sleep(0.2)
    return None


def running():
    """The dashboard already serving on this machine: {pid, port, key, url}, or None."""
    try:
        info = json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not jobs.alive(info.get("pid", 0)):
        return None
    return {**info, "url": f"http://127.0.0.1:{info['port']}/?k={info['key']}"}


def stored_key(new=False):
    """The dashboard's key: the one kept from before (a restart keeps the address, and an open tab works on), or a
    new one with `new` (qajev dashboard --new-key: every old address stops working)."""
    if not new:
        with contextlib.suppress(OSError):
            kept = KEY.read_text().strip()
            if len(kept) >= 32:
                return kept
    key = secrets.token_urlsafe(24)
    KEY.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(KEY, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    return key


def make_server(port=8790, key=None):
    """The dashboard's server on 127.0.0.1 (the first free port from `port`), not yet serving."""
    handler = type("DashboardHandler", (Handler,), {"key": key or secrets.token_urlsafe(24)})
    for candidate in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), handler)
        except OSError:
            continue
        handler.port = server.server_address[1]
        return server
    raise OSError(f"no free port from {port} to {port + 19}")


def serve(server):
    """Serve until interrupted, recorded in STATE so a second `qajev dashboard` points at this one."""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    # Written whole, then renamed into place: a record that exists but is still empty reads as no dashboard, and a
    # second `qajev dashboard` then started serving itself.
    partial = STATE.with_name(f"{STATE.name}.{os.getpid()}.tmp")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"pid": os.getpid(), "port": server.server_address[1], "key": server.RequestHandlerClass.key}, f)
    os.replace(partial, STATE)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        with contextlib.suppress(OSError):
            if json.loads(STATE.read_text()).get("pid") == os.getpid():
                STATE.unlink()
