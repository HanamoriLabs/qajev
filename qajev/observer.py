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
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, parse, request

from qajev_upload_privacy import sanitize_request, sanitize_text, sanitize_upload

from .report_files import image_paths, relative

MAX_FILE = 50 * 1024**2
MAX_RUN = 500 * 1024**2
DEFAULT_OUTBOX_BYTES = 512 * 1024**2
ENDED = {"pass", "fail", "stuck", "harness", "incomplete", "stopped"}


class ObserverError(RuntimeError):
    """Safe operator-facing failure. It never includes a credential or response body."""


class OutboxFull(ObserverError):
    def __init__(self, used: int, limit: int, needed: int, blobs: int):
        self.used, self.limit, self.needed, self.blob_bytes = used, limit, needed, blobs
        super().__init__(f"outbox byte cap reached: {used} bytes retained, {blobs} in blobs; "
                         f"the next write needs {needed} bytes with a {limit}-byte cap. "
                         "Work stays pending. Check free disk before increasing --max-outbox-mib.")


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
        body = sanitize_request(method, path, body)
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


def _safe_file(path: Path, root: Path | None) -> bool:
    """Reject every symlink component, including a directory that resolves back inside its allowed root."""
    if root is None or not path.is_relative_to(root) or not path.resolve().is_relative_to(root):
        return False
    current = path
    while current != root:
        if current.is_symlink():
            return False
        current = current.parent
    return not root.is_symlink() and path.is_file()


PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 '._:@+\-/~]{0,199}")  # the Cloud API's project name rule


def _check_project(name: object) -> None:
    if not isinstance(name, str) or not PROJECT.fullmatch(name):
        raise ObserverError(f"Invalid project name {name!r}: start with a letter or digit, then use letters, digits, "
                            "spaces and ' . _ : @ + - / ~ (at most 200 characters).")


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

    def __init__(self, home: Path, report_roots: list[Path], state: Path, project: str | None,
                 endpoint: str | None = None, *, max_outbox_bytes: int = DEFAULT_OUTBOX_BYTES,
                 project_for: list[tuple[Path, str]] | None = None):
        if type(max_outbox_bytes) is not int or max_outbox_bytes < 256 * 1024:
            raise ObserverError("The outbox byte cap must be an integer of at least 256 KiB.")
        self.project = project
        # A machine that serves several projects names them by folder. Only the operator's map decides; a job's own
        # metadata never picks its project, and no folder path leaves the machine.
        self.project_for = sorted(((Path(os.path.normpath(Path(prefix).expanduser().absolute())), name)
                                   for prefix, name in project_for or []), key=lambda item: -len(item[0].parts))
        for name in [project] if project is not None else []:
            _check_project(name)
        named: dict[Path, str] = {}
        for prefix, name in self.project_for:
            _check_project(name)
            if named.setdefault(prefix, name) != name:
                raise ObserverError(f"The folder {prefix} is mapped to two projects: {named[prefix]!r} and {name!r}.")
        if project is None and not self.project_for:
            raise ObserverError("Name a project, or map at least one folder to a project.")
        self.max_outbox_bytes = max_outbox_bytes
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
        self.blob_dir = self.state / "blobs"
        self.blob_dir.mkdir(mode=0o700, exist_ok=True)
        self._blob_bytes = 0
        self._other_bytes = 0
        self._database_files = {"outbox.sqlite", "outbox.sqlite-journal", "outbox.sqlite-wal", "outbox.sqlite-shm"}
        for path in self.state.rglob("*"):
            if path.is_symlink():
                raise ObserverError("The outbox must not contain symlinks.")
            if path.is_file():
                if path.is_relative_to(self.blob_dir):
                    self._blob_bytes += path.stat().st_size
                elif path.parent != self.state or path.name not in self._database_files:
                    self._other_bytes += path.stat().st_size
        self._reserve(0)
        self.lock = (self.state / "observer.lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise ObserverError("An observer already owns this state directory.") from None
        self.db = sqlite3.connect(self.state / "outbox.sqlite")
        self.db.execute("PRAGMA synchronous=FULL")
        try:
            self._limit_database()
            self.db.executescript("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                                 "CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, digest TEXT NOT NULL, "
                                 "record TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0);")
            config = [str(self.home), [str(root) for root in self.roots], project, endpoint]
            if self.project_for:  # an outbox made before folder maps keeps its configuration identity
                config.append(sorted([str(prefix), name] for prefix, name in self.project_for))
            config = json.dumps(config)
            if self._setting("configuration") not in {None, config}:
                raise ObserverError("This outbox belongs to another observation configuration; "
                                    "use a new state directory.")
            self._set("configuration", config)
            self._prune_confirmed()
        except BaseException:
            self.close()
            raise

    def outbox_bytes(self) -> int:
        return self._blob_bytes + self._other_bytes + sum(
            path.stat().st_size for name in self._database_files if (path := self.state / name).exists())

    def _reserve(self, needed: int) -> None:
        used = self.outbox_bytes()
        if used + needed > self.max_outbox_bytes:
            raise OutboxFull(used, self.max_outbox_bytes, needed, self._blob_bytes)

    def _limit_database(self) -> None:
        page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
        current = (self.state / "outbox.sqlite").stat().st_size
        # Reserve a rollback journal, including page headers and sector padding, before any DB write.
        journal = current + (current // page_size + 1) * 8 + 64 * 1024
        remaining = self.max_outbox_bytes - (self.outbox_bytes() - current) - journal
        pages = remaining // page_size
        if pages < max(1, current // page_size):
            raise OutboxFull(self.outbox_bytes(), self.max_outbox_bytes, journal, self._blob_bytes)
        actual = self.db.execute(f"PRAGMA max_page_count={pages}").fetchone()[0]
        if actual > pages:
            raise OutboxFull(self.outbox_bytes(), self.max_outbox_bytes, journal, self._blob_bytes)

    @contextlib.contextmanager
    def _write(self):
        self._limit_database()
        try:
            with self.db:
                yield
        except sqlite3.OperationalError as failure:
            if getattr(failure, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
                raise OutboxFull(self.outbox_bytes(), self.max_outbox_bytes, 0, self._blob_bytes) from None
            raise

    def _setting(self, key: str):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", [key]).fetchone()
        return row[0] if row else None

    def _set(self, key: str, value: str):
        with self._write():
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

    def _prune_confirmed(self) -> None:
        """Under the outbox lock, release confirmed and unreferenced blobs; pending references always win."""
        confirmed, pending = set(), set()
        for acknowledged, raw in self.db.execute("SELECT acknowledged,record FROM outbox"):
            hashes = {file["sha256"] for file in json.loads(raw)["files"]}
            (confirmed if acknowledged else pending).update(hashes)
        stored = {path.name for path in self.blob_dir.iterdir() if re.fullmatch(r"[0-9a-f]{64}", path.name)}
        removed = False
        for sha in (confirmed | stored) - pending:
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise ObserverError("The outbox contains an invalid evidence identity.")
            path = self.blob_dir / sha
            if path.is_symlink():
                raise ObserverError("The outbox must not contain symlinks.")
            try:
                size = path.stat().st_size
                path.unlink()
            except FileNotFoundError:
                continue
            self._blob_bytes -= size
            removed = True
        if removed:
            self._sync_blobs()

    def _sync_blobs(self) -> None:
        directory_fd = os.open(self.blob_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _blob(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        path = self.blob_dir / sha
        if not path.exists():
            self._reserve(len(data))
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.blob_dir, prefix=".staged-", delete=False) as output:
                    temporary = Path(output.name)
                    os.chmod(temporary, 0o600)
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
                self._blob_bytes += len(data)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            self._sync_blobs()
        return sha

    def project_of(self, *places: object) -> str | None:
        """The deepest mapped folder that holds the first known place (a job's start folder; only when that is
        unknown, its report folder), else the default project, else None: such a run is skipped, never guessed.
        A known but unmapped start folder never falls through to its report folder's project."""
        place = next((p for p in places if isinstance(p, (str, Path)) and Path(p).is_absolute()), None)
        if place is not None:
            path = Path(os.path.normpath(place))
            for prefix, name in self.project_for:
                if path == prefix or prefix in path.parents:
                    return name
        return self.project

    def _enqueue(self, local: str, data: dict, run: Path | None, job: dict, project: str) -> int:
        missing, files = [], []
        sources: dict[str, Path | bytes] = {}
        try:
            images = image_paths(data)
        except ValueError:
            images = []
            missing.append("invalid image evidence metadata")
        names = [*images, "report.html", "report.md", "report.json"] if run else []
        allowed = next((root for root in self.roots if run and run.is_relative_to(root)
                        and run.resolve().is_relative_to(root)), None)
        total = 0
        if run and not allowed:
            missing.append("report root outside approved roots")
        for name in names if allowed and run is not None else []:
            assert run is not None
            path = run / name
            if not _safe_file(path, allowed) or path.stat().st_size > MAX_FILE:
                missing.append(name)
                continue
            content = sanitize_upload(path.read_bytes())
            total += len(content)
            if total > MAX_RUN:
                raise ObserverError("Run evidence exceeds the upload budget; it was not acknowledged.")
            files.append({"path": name, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)})
            sources[name] = path
        gate = str(data.get("gate", "harness")).lower()
        if gate in ENDED and not any(file["path"] == "report.json" for file in files):
            # A foreground configuration/harness error can have a job result but no output directory.
            # Preserve that full selected-home result instead of acknowledging only a shortened summary.
            content = sanitize_upload(json.dumps(data, ensure_ascii=False).encode())
            if len(content) > MAX_FILE:
                raise ObserverError("The source result exceeds the report upload budget.")
            files.append({"path": "report.json", "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)})
            sources["report.json"] = content
        rec = {"id": local, "kind": "run", "project": project,
               "title": sanitize_text(str(data.get("suite") or job.get("title") or local), 160),
               "gate": gate if gate in ENDED | {"queued", "running"} else "harness",
               "started_at": _iso(data.get("started_at") or job.get("started_at")),
               "seconds": data.get("seconds", 0), "tests": _tests(data), "cost_usd": data.get("cost", {}).get("usd", 0),
               "files": files, "report": {"qajev_version": data.get("qajev"), "missing_evidence": missing,
                   "reason": sanitize_text(str(data.get("error") or data.get("reason") or ""), 4000)}}
        raw = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        previous = self.db.execute("SELECT record,digest,acknowledged FROM outbox WHERE id=?", [local]).fetchone()
        if previous and json.loads(previous[0])["gate"] in ENDED and rec["gate"] not in ENDED:
            return len(missing)
        if previous and previous[1] == digest:
            if previous[2] or all((self.blob_dir / file["sha256"]).is_file() for file in files):
                return len(missing)
            retained = list(self.blob_dir.rglob("*"))
            if any(path.is_symlink() for path in retained):
                raise ObserverError("The outbox must not contain symlinks.")
            self._blob_bytes = sum(path.stat().st_size for path in retained if path.is_file())
        # Compare the full snapshot before writing bytes. An unchanged acknowledged report must
        # not recreate its pruned blobs on every poll. Only one file is held in memory at a time.
        for file in files:
            source = sources[file["path"]]
            if isinstance(source, Path):
                if not _safe_file(source, allowed) or source.stat().st_size > MAX_FILE:
                    raise ObserverError("Run evidence changed during inventory; work stays pending.")
                try:
                    content = sanitize_upload(source.read_bytes())
                except OSError:
                    raise ObserverError("Run evidence changed during inventory; work stays pending.") from None
            else:
                content = source
            if hashlib.sha256(content).hexdigest() != file["sha256"]:
                raise ObserverError("Run evidence changed during inventory; work stays pending.")
            self._blob(content)
        with self._write():
            self.db.execute("INSERT INTO outbox VALUES (?, ?, ?, 0) ON CONFLICT(id) DO UPDATE "
                            "SET digest=excluded.digest, record=excluded.record, acknowledged=0 "
                            "WHERE outbox.digest!=excluded.digest", [local, digest, raw])
        return len(missing)

    def inventory(self) -> dict[str, int]:
        counts = {"jobs": 0, "historical_reports": 0, "missing_evidence": 0, "no_project": 0}
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
            project = self.project_of(job.get("cwd"), run)
            if project is None:
                counts["no_project"] += 1
                continue
            counts["missing_evidence"] += self._enqueue(folder.name, data, run, job, project)
            counts["jobs"] += 1
        for root in self.roots:
            for path in sorted(root.rglob("report.json")) if root.is_dir() else []:
                if path.parent in seen or not _safe_file(path, root):
                    continue
                data = _json(path)
                project = self.project_of(path.parent)
                if project is None:
                    counts["no_project"] += 1
                    continue
                local = "report-" + hashlib.sha256(str(path).encode()).hexdigest()[:40]
                counts["missing_evidence"] += self._enqueue(local, data, path.parent, {}, project)
                counts["historical_reports"] += 1
        counts["pending"] = self.pending()
        counts["blob_bytes"] = self._blob_bytes
        counts["outbox_bytes"] = self.outbox_bytes()
        counts["max_outbox_bytes"] = self.max_outbox_bytes
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
        try:
            for local, digest, raw in pending:
                try:
                    self._flush_record(client, local, digest, raw)
                except ObserverError as failure:
                    raise ObserverError(f"Local run {local!r}: {failure}") from None
                acknowledged += 1
        except BaseException:
            # Cleanup must still run after a failed upload, without hiding that run's original error.
            with contextlib.suppress(Exception):
                self._prune_confirmed()
            raise
        self._prune_confirmed()
        return {"acknowledged": acknowledged, "pending": self.pending()}

    def _flush_record(self, client, local: str, digest: str, raw: str) -> None:
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
            try:
                content = (self.blob_dir / file["sha256"]).read_bytes()
            except OSError:
                raise ObserverError("Durable evidence is unreadable or missing; the run remains pending.") from None
            if hashlib.sha256(content).hexdigest() != file["sha256"]:
                raise ObserverError("Durable evidence changed; the run remains pending.")
            client.upload(cloud, item["path"], content, file["sha256"])
        confirm = client.observe(rec)
        if confirm.get("synced") != matches or confirm.get("missing") != []:
            raise ObserverError("The endpoint still reports missing evidence; work stays pending.")
        with self._write():
            self.db.execute("UPDATE outbox SET acknowledged=1 WHERE id=? AND digest=?", [local, digest])
