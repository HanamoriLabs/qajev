"""Explicit registered-machine report observation, with a durable local outbox. Never executes a suite."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, parse, request

from qajev_upload_privacy import sanitize_text, sanitize_upload

from .report_files import image_paths, relative

MAX_FILE = 50 * 1024**2
MAX_RUN = 500 * 1024**2
ENDED = {"pass", "fail", "stuck", "harness", "incomplete", "stopped"}


class ObserverError(RuntimeError):
    """Safe operator-facing failure. It never includes a credential or response body."""


def read_credential(path: Path) -> str:
    """Only the installed client opens the operator-placed file; no shell/profile credential discovery."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ObserverError("The machine credential must be an owned regular file with mode 0600.")
            value = handle.read(8193).strip()
    except OSError:
        raise ObserverError("The machine credential must be an owned regular file, never a symlink.") from None
    if not re.fullmatch(r"qjm_[A-Za-z0-9_-]{32,256}", value):
        raise ObserverError("The machine credential file is invalid.")
    return value


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ObserverError("The observation endpoint redirected; no credential was forwarded.")


class Client:
    """Authenticated observation only, with fixed-origin requests and no redirects or model route."""

    def __init__(self, base: str, credential_file: Path):
        url = parse.urlsplit(base)
        if (url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}
                or url.scheme not in {"http", "https"} or not url.hostname
                or url.scheme != "https" and url.hostname not in {"localhost", "127.0.0.1", "::1"}):
            raise ObserverError("Use an HTTPS control-plane origin without credentials, query or path.")
        self.base = base.rstrip("/")
        self._credential = read_credential(credential_file)
        self._opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())

    def _send(self, method: str, path: str, body: bytes, headers=None) -> dict:
        req = request.Request(self.base + path, data=body, method=method, headers={
            "authorization": "Bearer " + self._credential, **(headers or {}),
        })
        try:
            with self._opener.open(req, timeout=30) as response:
                raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ObserverError("The observation reply is too large.")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
            return data
        except error.HTTPError as failure:
            raise ObserverError(f"The observation endpoint refused HTTP {failure.code}; work stays pending.") from None
        except (OSError, ValueError):
            raise ObserverError("The observation endpoint could not confirm the request; work stays pending.") from None

    def heartbeat(self) -> dict:
        return self._send("POST", "/api/v1/observer/heartbeat", b"{}", {"content-type": "application/json"})

    def observe(self, record: dict) -> dict:
        return self._send("POST", "/api/v1/observer/runs", json.dumps({"runs": [record]}).encode(),
                          {"content-type": "application/json"})

    def upload(self, run: str, name: str, content: bytes, sha: str) -> None:
        path = "/api/v1/runs/" + parse.quote(run, safe="") + "/files/" + parse.quote(name, safe="/")
        self._send("PUT", path, content, {"x-content-sha256": sha, "x-content-size": str(len(content))})


def _json(path: Path) -> dict:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE:
            raise ObserverError("Report metadata must be a bounded regular file.")
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (OSError, ValueError):
        raise ObserverError("Report metadata is unreadable or incomplete; it was not acknowledged.") from None


def _iso(value: object) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    raise ObserverError("Run start time is missing.")


def _safe_file(path: Path, root: Path) -> bool:
    """Reject every symlink component, including a directory that resolves back inside its allowed root."""
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root):
        return False
    current = path
    while current != root:
        if current.is_symlink():
            return False
        current = current.parent
    return not root.is_symlink() and path.is_file()


def _tests(data: dict) -> list[dict]:
    out = []
    for scenario in data.get("scenarios", []):
        outcome = scenario.get("outcome", "harness")
        # Unknown/non-product outcomes stay explicit; never manufacture a passing test.
        row = {"name": sanitize_text(str(scenario.get("name") or "test")),
               "about": sanitize_text(str(scenario.get("about") or "")),
               "outcome": outcome if outcome in {"pass", "fail", "stuck", "harness"} else "harness",
               "seconds": scenario.get("seconds"),
               "wrong": [sanitize_text(str(scenario.get("reason") or outcome))] if outcome != "pass" else [],
               "checks": [{"check": sanitize_text(str(check.get("check", ""))), "ok": check["ok"]}
                          for check in scenario.get("checks", []) if isinstance(check.get("ok"), bool)]}
        if scenario.get("shot") and relative(scenario["shot"]):
            row["screenshots"] = [{"path": scenario["shot"], "caption": "Last screen"}]
        out.append(row)
    return out


class Observer:
    """Own one configured outbox. Its durable blobs survive source/report pruning and interrupted uploads."""

    def __init__(self, home: Path, report_roots: list[Path], state: Path, project: str, endpoint: str | None = None):
        self.home = Path(home).absolute()
        self.roots = [Path(root).absolute() for root in report_roots]
        if any(root.is_symlink() for root in [self.home, *self.roots]):
            raise ObserverError("Observation roots must not be symlinks.")
        self.state = Path(state).absolute()
        if self.state.is_symlink():
            raise ObserverError("The observation state directory must not be a symlink.")
        self.state.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.state.stat().st_uid != os.getuid() or stat.S_IMODE(self.state.stat().st_mode) != 0o700:
            raise ObserverError("The observation state directory must be owned and mode 0700.")
        self.project = project
        self.lock = (self.state / "observer.lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise ObserverError("An observer already owns this state directory.") from None
        self.db = sqlite3.connect(self.state / "outbox.sqlite")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                             "CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, digest TEXT NOT NULL, "
                             "record TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0);")
        self.blob_dir = self.state / "blobs"
        self.blob_dir.mkdir(mode=0o700, exist_ok=True)
        config = json.dumps([str(self.home), [str(root) for root in self.roots], project, endpoint])
        if self._setting("configuration") not in {None, config}:
            self.close()
            raise ObserverError("This outbox belongs to another observation configuration; use a new state directory.")
        self._set("configuration", config)

    def _setting(self, key: str):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", [key]).fetchone()
        return row[0] if row else None

    def _set(self, key: str, value: str):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", [key, value])

    def close(self):
        self.db.close()
        self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def pending(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM outbox WHERE acknowledged=0").fetchone()[0]

    def blobs(self):
        return (file.read_bytes() for file in self.blob_dir.iterdir())

    def _blob(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        path = self.blob_dir / sha
        if not path.exists():
            with path.open("xb") as output:
                os.chmod(path, 0o600)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            directory_fd = os.open(self.blob_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return sha

    def _enqueue(self, local: str, data: dict, run: Path | None, job: dict) -> int:
        missing, files = [], []
        names = [*image_paths(data), "report.html", "report.md", "report.json"] if run else []
        allowed = next((root for root in self.roots if run and run.is_relative_to(root)
                        and run.resolve().is_relative_to(root)), None)
        total = 0
        if run and not allowed:
            missing.append("report root outside approved roots")
        for name in names if allowed else []:
            path = run / name
            if not _safe_file(path, allowed) or path.stat().st_size > MAX_FILE:
                missing.append(name)
                continue
            content = sanitize_upload(path.read_bytes())
            total += len(content)
            if total > MAX_RUN:
                raise ObserverError("Run evidence exceeds the upload budget; it was not acknowledged.")
            files.append({"path": name, "sha256": self._blob(content), "size": len(content)})
        gate = str(data.get("gate", "harness")).lower()
        rec = {"id": local, "kind": "run", "project": self.project,
               "title": sanitize_text(str(data.get("suite") or job.get("title") or local), 160),
               "gate": gate if gate in ENDED | {"queued", "running"} else "harness",
               "started_at": _iso(data.get("started_at") or job.get("started_at")),
               "seconds": data.get("seconds", 0), "tests": _tests(data), "cost_usd": data.get("cost", {}).get("usd", 0),
               "files": files, "report": {"qajev_version": data.get("qajev"), "missing_evidence": missing}}
        raw = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        previous = self.db.execute("SELECT record FROM outbox WHERE id=?", [local]).fetchone()
        if previous and json.loads(previous[0])["gate"] in ENDED and rec["gate"] not in ENDED:
            return len(missing)
        with self.db:
            self.db.execute("INSERT INTO outbox VALUES (?, ?, ?, 0) ON CONFLICT(id) DO UPDATE "
                            "SET digest=excluded.digest, record=excluded.record, acknowledged=0 "
                            "WHERE outbox.digest!=excluded.digest", [local, digest, raw])
        return len(missing)

    def inventory(self) -> dict[str, int]:
        counts = {"jobs": 0, "historical_reports": 0, "missing_evidence": 0}
        seen = set()
        jobs = self.home / "jobs"
        for folder in sorted(jobs.iterdir()) if jobs.is_dir() else []:
            if not folder.is_dir() or not _safe_file(folder / "job.json", jobs):
                continue
            job = _json(folder / "job.json")
            if (folder / "result.json").exists():
                data = _json(folder / "result.json")
            else:
                events = []
                path = folder / "events.jsonl"
                if _safe_file(path, jobs) and path.stat().st_size <= MAX_FILE:
                    for line in path.read_text().splitlines():
                        with contextlib.suppress(ValueError):
                            events.append(json.loads(line))
                started = any(e.get("event") == "run" for e in events)
                scenarios = [e["result"] for e in events
                             if e.get("event") == "scenario" and isinstance(e.get("result"), dict)]
                data = {"gate": "running" if started else "queued", "started_at": job.get("started_at"),
                        "scenarios": scenarios}
            run = Path(data["run_dir"]).absolute() if isinstance(data.get("run_dir"), str) else None
            if run:
                seen.add(run)
            counts["missing_evidence"] += self._enqueue(folder.name, data, run, job)
            counts["jobs"] += 1
        for root in self.roots:
            for path in sorted(root.rglob("report.json")) if root.is_dir() else []:
                if path.parent in seen or not _safe_file(path, root):
                    continue
                data = _json(path)
                local = "report-" + hashlib.sha256(str(path).encode()).hexdigest()[:40]
                counts["missing_evidence"] += self._enqueue(local, data, path.parent, {})
                counts["historical_reports"] += 1
        counts["pending"] = self.pending()
        return counts

    def flush(self, client) -> dict[str, int]:
        identity = client.heartbeat().get("runner_id")
        if not isinstance(identity, str) or not identity.startswith("m_"):
            raise ObserverError("The endpoint did not confirm the registered machine identity.")
        if self._setting("machine") not in {None, identity}:
            raise ObserverError("This outbox belongs to another registered machine.")
        self._set("machine", identity)
        acknowledged = 0
        pending = self.db.execute("SELECT id,digest,record FROM outbox WHERE acknowledged=0 ORDER BY id").fetchall()
        for local, digest, raw in pending:
            rec = json.loads(raw)
            reply = client.observe(rec)
            matches = reply.get("synced", [])
            if len(matches) != 1 or matches[0].get("local_id") != local:
                raise ObserverError("The endpoint did not acknowledge this local run.")
            cloud = matches[0].get("id")
            if not isinstance(cloud, str) or not re.fullmatch(r"qms_[A-Za-z0-9_.-]+", cloud):
                raise ObserverError("The endpoint returned an invalid run identity.")
            files = {file["path"]: file for file in rec["files"]}
            for item in reply.get("missing", []):
                if item.get("run") != cloud or item.get("path") not in files:
                    raise ObserverError("The endpoint requested foreign evidence.")
                file = files[item["path"]]
                content = (self.blob_dir / file["sha256"]).read_bytes()
                if hashlib.sha256(content).hexdigest() != file["sha256"]:
                    raise ObserverError("Durable evidence changed; the run remains pending.")
                client.upload(cloud, item["path"], content, file["sha256"])
            confirm = client.observe(rec)
            if confirm.get("synced") != matches or confirm.get("missing") != []:
                raise ObserverError("The endpoint still reports missing evidence; work stays pending.")
            with self.db:
                self.db.execute("UPDATE outbox SET acknowledged=1 WHERE id=? AND digest=?", [local, digest])
            acknowledged += 1
        return {"acknowledged": acknowledged, "pending": self.pending()}
