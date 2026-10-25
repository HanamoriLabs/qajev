"""Opt-in Linux proof of real managed-Chrome watchdog recovery, with no model calls.

--prepare (alias --plan) only writes/parses a synthetic suite and calls the CLI's
plan entry point in this process. --execute runs exactly one instrumented CLI
subprocess. Run execution inside a dedicated, externally locked systemd scope;
the launcher must verify that scope is empty after this tool exits.
"""

import argparse
import base64
import contextlib
import functools
import hashlib
import http.server
import importlib.metadata
import io
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.dont_write_bytecode = True

NAMES = ["fixture first", "fixture blocked hook", "fixture independent"]
SHORT_TIMEOUT = 15.0
MAX_LOAD5 = 12.0
MIN_DISK = 20 * 1024**3
SAFE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL")


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def proc_record(pid: int) -> dict | None:
    """Use start ticks, never a name match, as process identity."""
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "state": fields[0], "ppid": int(fields[1]),
                "pgrp": int(fields[2]), "start_ticks": fields[19]}
    except (FileNotFoundError, ProcessLookupError):
        return None


def trace(kind: str, **data: object) -> None:
    path = os.environ.get("WATCHDOG_PROOF_TRACE")
    if not path:
        return
    payload = {"kind": kind, "at": time.monotonic(), "process": proc_record(os.getpid()), **data}
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(payload) + "\n").encode())
    finally:
        os.close(fd)


def fixture_group_child(*args: Any) -> dict:
    """Observe production notifications inside the real spawned worker."""
    from qajev import runner, watchdog

    notify = watchdog.notify

    def observed(kind: str, **data: Any) -> None:
        if kind in {"scenario", "resource", "resource_pending", "resource_resolved"}:
            extra = {}
            if data.get("daemon_pid"):
                extra["daemon_process"] = proc_record(int(data["daemon_pid"]))
            trace("notification", notification=kind, data=data, **extra)
        notify(kind, **data)

    watchdog.notify = observed
    trace("group_start", scenarios=[s.name for s in args[0]], cdp_url=args[2])
    # No browser, daemon, transport, Session, or result is replaced here.
    return runner._group_child(*args)


def suite_data(origin: str, marker: Path) -> dict:
    first_expect = {"text": "Fixture first", "js": "location.pathname === '/first'",
                    "says": {"js": "The first real tab loaded its own loopback fixture."}}
    return {
        "name": "Managed Chrome watchdog recovery fixture",
        "about": "A blocked local command is stopped; an independent URL continues in a fresh session.",
        "base_url": origin, "devices": ["desktop"], "cost_cap_usd": 0, "settle": 0,
        "scenarios": [
            {"name": NAMES[0], "url": "/first", "about": "Keep a completed prefix result through recovery.",
             "expect": first_expect},
            {"name": NAMES[1], "about": "Plant a shell hook that blocks beyond the explicit proof deadline.",
             "before": [{"command": "printf '%s\\n' \"$$\" > " + shlex.quote(str(marker)) + "; exec sleep 120"}],
             "expect": first_expect},
            {"name": NAMES[2], "url": "/fresh", "about": "An independent root passes after verified cleanup.",
             "expect": {"text": "Fixture fresh", "js": "location.pathname === '/fresh'",
                        "says": {"js": "A fresh real tab reached the independent loopback URL."}}},
        ],
    }


def prepare(output: Path, origin: str) -> dict:
    """CPU-only: no subprocess, browser, listener, model, or dependency install."""
    from qajev import cli
    from qajev.suite import load

    suite = output / "suite.json"
    write_json(suite, suite_data(origin, output / "blocking-command.pid"))
    empty = output / "empty.env"
    fd = os.open(empty, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    info = empty.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size:
        raise RuntimeError("the explicit env file must be an empty regular 0600 file")
    parsed = load(suite)
    if len(parsed.scenarios) != 3 or any(s.goal for s in parsed.scenarios):
        raise RuntimeError("fixture must have three scenarios without goals")
    if parsed.scenarios[1].depends_on != [NAMES[0]] or parsed.scenarios[2].depends_on:
        raise RuntimeError("the blocked hook must follow the prefix; the last URL must be independent")
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(["plan", str(suite), "--json"])
    (output / "plan.stdout").write_text(stdout.getvalue())
    (output / "plan.stderr").write_text(stderr.getvalue())
    plan = json.loads(stdout.getvalue())
    if code != 0 or plan["not_described"] or len(plan["plan"]) != 3:
        raise RuntimeError("CLI plan did not accept all three described scenarios")
    receipt = {"mode": "prepare", "browser_started": False, "subprocesses_started": 0,
               "plan_exit": code, "scenario_names": [s.name for s in parsed.scenarios],
               "env_file_mode": "0600", "env_file_bytes": info.st_size,
               "production_timeout": 300, "proof_timeout": SHORT_TIMEOUT,
               "suite": str(suite), "plan": plan}
    write_json(output / "prepare.json", receipt)
    return receipt


def source_info(source: Path, declared_sha: str) -> dict:
    import qajev
    from qajev import watchdog

    imported = Path(qajev.__file__).resolve()
    if not re.fullmatch(r"[a-f0-9]{40}", declared_sha):
        raise RuntimeError("--source-sha must be the full 40-character source commit")
    if imported.parent != source / "qajev":
        raise RuntimeError(f"wrong source import: {imported}")
    if watchdog.TIMEOUT != 300.0:
        raise RuntimeError("production TIMEOUT changed; proof refused")
    paths = [*sorted((source / "qajev").rglob("*.py")), source / "pyproject.toml", source / "uv.lock"]
    hashes = {str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    return {"source_root": str(source), "declared_source_sha": declared_sha, "qajev_import": str(imported),
            "python": sys.version, "executable": sys.executable, "production_timeout": watchdog.TIMEOUT,
            "proof_timeout": SHORT_TIMEOUT, "source_files_sha256": hashes,
            "proof_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def dependency_info(source: Path) -> dict:
    result = {}
    for name in ("qajev", "jev-ultrafast", "browser-harness", "websockets"):
        dist = importlib.metadata.distribution(name)
        direct = dist.read_text("direct_url.json")
        result[name] = {"version": dist.version, "direct_url": json.loads(direct) if direct else None}
    if result["browser-harness"]["version"] != "0.1.13":
        raise RuntimeError("proof requires the source lockfile's browser-harness 0.1.13")
    pin = re.search(r"jev-ultrafast @ git\+[^\s\"]+@([a-f0-9]{40})", (source / "pyproject.toml").read_text())
    actual = (result["jev-ultrafast"]["direct_url"] or {}).get("vcs_info", {}).get("commit_id")
    if pin is None or actual != pin.group(1):
        raise RuntimeError("installed jev-ultrafast does not match the source's exact commit pin")
    return result


def attest_module(name: str, distribution_name: str) -> dict:
    """Require imported bytes to be the installed distribution's recorded module."""
    dist = importlib.metadata.distribution(distribution_name)
    direct = dist.read_text("direct_url.json")
    if direct and json.loads(direct).get("dir_info", {}).get("editable"):
        raise RuntimeError(f"editable dependency refused: {distribution_name}")
    relative = name.replace(".", "/")
    entries = [entry for entry in dist.files or []
               if str(entry) in {relative + ".py", relative + "/__init__.py"}]
    if len(entries) != 1:
        raise RuntimeError(f"distribution does not identify exactly one module file: {name}")
    entry = entries[0]
    expected = Path(dist.locate_file(entry)).resolve()
    module = importlib.import_module(name)
    filename = getattr(module, "__file__", None)
    if not filename or Path(filename).resolve() != expected:
        raise RuntimeError(f"dependency module shadowed: {name}")
    content = expected.read_bytes()
    record = entry.hash
    record_hash = None
    if record is not None:
        actual = base64.urlsafe_b64encode(hashlib.new(record.mode, content).digest()).decode().rstrip("=")
        if actual != record.value:
            raise RuntimeError(f"installed module differs from its RECORD hash: {name}")
        record_hash = {"algorithm": record.mode, "value": record.value, "verified": True}
    return {"module": name, "distribution": distribution_name, "version": dist.version,
            "imported_path": str(Path(filename).resolve()), "distribution_path": str(expected),
            "sha256": hashlib.sha256(content).hexdigest(), "record_hash": record_hash}


def runtime_attestation(source: Path, declared_sha: str) -> dict:
    modules = [attest_module(name, "browser-harness") for name in (
        "browser_harness.daemon", "browser_harness.helpers", "browser_harness.admin", "browser_harness._ipc")]
    modules.append(attest_module("jev_ultrafast.browser", "jev-ultrafast"))
    return {"source": source_info(source, declared_sha), "dependencies": dependency_info(source),
            "imported_modules": modules}


def cli_child(argv: list[str]) -> int:
    from qajev import chrome, cli, runner, watchdog
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    if os.environ.get("BU_BROWSER_ID"):
        raise RuntimeError("BU_BROWSER_ID would bypass the local daemon ownership path")
    declared = json.loads(os.environ["WATCHDOG_PROOF_SOURCE"])
    attestation = runtime_attestation(Path(declared["root"]).resolve(), declared["sha"])
    trace("runtime_attestation", attestation=attestation)
    write_json(Path(os.environ["WATCHDOG_PROOF_TRACE"]).with_name("inner-runtime.json"), attestation)
    # Fixture-only short on-disk temporary-root override; production constant stays unchanged.
    chrome.TEMP_ROOT = Path(os.environ["TMPDIR"])
    scope = own_scope(os.environ["WATCHDOG_PROOF_SCOPE"])

    def scoped_processes() -> list[tuple[int, str]]:
        # Product reap() otherwise discovers stale resources from unrelated runs.
        return [(pid, chrome.cmdline(pid)) for pid in scope_processes(scope)]

    chrome.processes = scoped_processes
    trace("configuration", production_timeout=watchdog.TIMEOUT, proof_timeout=SHORT_TIMEOUT,
          chrome_temp_root=str(chrome.TEMP_ROOT), bu_browser_id_present=False,
          stale_resource_discovery_scope=str(scope))
    watchdog.supervise = functools.partial(watchdog.supervise, timeout=SHORT_TIMEOUT)
    runner._group_child = fixture_group_child
    normal_start, normal_stop = chrome.start, chrome.stop

    def observed_start(*args: Any, **kwargs: Any) -> dict:
        record = normal_start(*args, **kwargs)
        trace("chrome_start", record=record, chrome_process=proc_record(record["pid"]),
              version=chrome.version(record["cdp_url"]))
        return record

    def flushing_stop(profile: str = "default") -> bool:
        try:
            path = chrome._state_file(profile)
            if path.exists():
                record = json.loads(path.read_text())
                if chrome.owns(record):
                    trace("chrome_stop_owned", chrome_process=proc_record(record["pid"]))
                    endpoint = (chrome.version(record["cdp_url"]) or {}).get("webSocketDebuggerUrl")
                    if endpoint:
                        with connect(endpoint, open_timeout=5) as socket:
                            socket.send(json.dumps({"id": 1, "method": "Browser.close"}))
                            with contextlib.suppress(ConnectionClosed):
                                socket.recv(timeout=5)
                    for _ in range(50):
                        if chrome.reaped(record["pid"]):
                            break
                        time.sleep(0.1)
        except (OSError, TimeoutError, ConnectionClosed) as error:
            trace("netlog_flush_error", detail=str(error))
        finally:
            stopped = normal_stop(profile)
        return stopped

    chrome.start, chrome.stop = observed_start, flushing_stop
    return cli.main(argv)


def own_scope(name: str) -> Path:
    if not name.endswith(".scope") or "/" in name:
        raise RuntimeError("pass the launcher's unique .scope basename")
    rows = Path("/proc/self/cgroup").read_text().splitlines()
    relative = next((row.split(":", 2)[2] for row in rows if row.startswith("0::")), None)
    if relative is None or Path(relative).name != name:
        raise RuntimeError("this process is not in the requested dedicated cgroup v2 scope")
    scope = Path("/sys/fs/cgroup") / relative.lstrip("/")
    if (scope / "memory.max").read_text().strip() != str(8 * 1024**3):
        raise RuntimeError("scope memory.max must be exactly 8 GiB")
    if (scope / "memory.swap.max").read_text().strip() != "0":
        raise RuntimeError("scope memory.swap.max must be 0")
    return scope


def scope_processes(scope: Path) -> dict[int, dict]:
    found = {}
    paths = list(scope.rglob("cgroup.procs"))
    if scope / "cgroup.procs" not in paths:
        raise RuntimeError("dedicated scope process list is missing or unreadable")
    for path in paths:
        for value in path.read_text().split():
            record = proc_record(int(value))
            if record:
                found[record["pid"]] = record
    if os.getpid() not in found:
        raise RuntimeError("dedicated scope process list did not include this process")
    return found


def guard(output: Path) -> dict:
    sample = {"at": time.monotonic(), "load5": os.getloadavg()[1], "disk_free": shutil.disk_usage(output).free}
    if sample["load5"] >= MAX_LOAD5 or sample["disk_free"] < MIN_DISK:
        raise RuntimeError(f"resource guard refused: {sample}")
    return sample


def signal_owned(record: dict, sig: signal.Signals) -> None:
    current = proc_record(record["pid"])
    if current and current["start_ticks"] == record["start_ticks"]:
        with contextlib.suppress(ProcessLookupError):
            os.kill(record["pid"], sig)


def verify(output: Path, code: int, trace_rows: list[dict], aftermath: dict) -> dict:
    report = json.loads((output / "cli.stdout").read_text())
    write_json(output / "report.json", report)
    rows = report["scenarios"]
    assert code == 2 and report["gate"] == "INCOMPLETE", (code, report.get("gate"))
    assert [r["name"] for r in rows] == NAMES
    assert [r["outcome"] for r in rows] == ["pass", "harness", "pass"]
    blocked = rows[1]
    assert blocked["stop"] == "no_progress" and blocked["watchdog"]["cleanup_complete"] is True
    assert blocked["watchdog"]["identity"] == "before hook 1 (command)"
    assert blocked["watchdog"]["resource_ownership_uncertain"] is False
    assert blocked["watchdog"]["cost_uncertain"] is False
    assert report["cost"]["calls"] == {"typesafe": 0, "text": 0}
    assert not any(r.get("screens") for r in rows)
    chrome_rows = [r for r in trace_rows if r["kind"] == "chrome_start"]
    assert len(chrome_rows) == 1
    attestations = [r for r in trace_rows if r["kind"] == "runtime_attestation"]
    assert len(attestations) == 1
    assert attestations[0]["at"] < chrome_rows[0]["at"]
    assert len(attestations[0]["attestation"]["imported_modules"]) == 5
    assert attestations[0]["attestation"] == json.loads((output / "outer-runtime.json").read_text())
    assert chrome_rows[0]["record"]["headless"] and chrome_rows[0]["record"]["ephemeral"]
    groups = [r for r in trace_rows if r["kind"] == "group_start"]
    assert len(groups) == 2
    assert groups[0]["scenarios"] == NAMES and groups[1]["scenarios"] == [NAMES[2]]
    workers = [g["process"]["pid"] for g in groups]
    assert workers[0] != workers[1]
    identities = []
    for n, pid in enumerate(workers):
        observed = [r for r in trace_rows if r["kind"] == "notification" and r["process"]["pid"] == pid]
        assert [r["data"]["name"] for r in observed if r["notification"] == "scenario"] == (
            NAMES[:2] if n == 0 else NAMES[2:])
        resources = [r for r in observed if r["notification"] == "resource"]
        daemons = [r for r in resources if r.get("daemon_process")]
        assert daemons and all(r["daemon_process"]["start_ticks"] for r in daemons)
        daemon = daemons[0]
        targets = sorted({target for r in resources for target in r["data"].get("targets", [])})
        # Includes both the pinned daemon's dedicated tab and the scenario session's tab.
        assert len(targets) >= 2
        pending = {r["data"]["request_id"] for r in observed if r["notification"] == "resource_pending"}
        resolved = {r["data"]["request_id"] for r in observed if r["notification"] == "resource_resolved"}
        assert pending <= resolved
        identities.append({"generation": n + 1, "worker": groups[n]["process"],
                           "daemon_name": daemon["data"]["daemon"], "daemon": daemon["daemon_process"],
                           "targets": targets, "cdp_url": groups[n]["cdp_url"]})
    assert identities[0]["daemon_name"] != identities[1]["daemon_name"]
    assert identities[0]["daemon"]["pid"] != identities[1]["daemon"]["pid"]
    assert set(identities[0]["targets"]).isdisjoint(identities[1]["targets"])
    assert identities[0]["cdp_url"] == identities[1]["cdp_url"] == chrome_rows[0]["record"]["cdp_url"]
    netlog = json.loads((output / "chrome-netlog.json").read_text())
    request_type = netlog["constants"]["logEventTypes"]["URL_REQUEST_START_JOB"]
    starts = [e for e in netlog["events"] if e.get("type") == request_type and e.get("phase") != 2]
    hosts = sorted({urlsplit(e["params"]["url"]).hostname for e in starts
                    if urlsplit(e["params"]["url"]).hostname})
    rejected = [host for host in hosts if host not in {"127.0.0.1", "localhost"}
                and not host.endswith(".localhost")]
    write_json(output / "request-hosts.json", {"request_hosts": hosts, "nonlocal_hosts": rejected,
                                               "request_starts": len(starts)})
    assert "127.0.0.1" in hosts and not rejected, hosts
    assert aftermath["blocking_command_observed"] and not aftermath["remaining_owned"]
    assert not aftermath["intervention"]
    # Recorded workers/daemons/Chrome must themselves be gone, including zombies.
    recorded = [chrome_rows[0]["chrome_process"], *[g["process"] for g in groups],
                *[identity["daemon"] for identity in identities]]
    assert all(not (current := proc_record(r["pid"])) or current["start_ticks"] != r["start_ticks"]
               for r in recorded)
    return {"gate": "PASS", "cli_exit": code, "report_gate": report["gate"],
            "outcomes": [r["outcome"] for r in rows], "cost_calls": report["cost"]["calls"],
            "generations": identities, "production_timeout": 300, "proof_timeout": SHORT_TIMEOUT,
            "request_hosts": hosts, "scope_external_verification_required": True}


def execute(args: argparse.Namespace, output: Path) -> dict:
    if sys.platform != "linux" or os.getpriority(os.PRIO_PROCESS, 0) != 19 or sys.flags.optimize:
        raise RuntimeError("execution requires Linux at nice 19 under the external GPU lock")
    scope = own_scope(args.scope_name)
    runtime, temporary = args.runtime_dir.resolve(), args.temp_dir.resolve()
    for path in (runtime, temporary):
        if str(path) == "/tmp" or Path("/tmp") in path.parents or len(str(path).encode()) > 48:
            raise RuntimeError("runtime and temp paths must be short, on-disk paths outside /tmp")
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
        filesystem = subprocess.run(["stat", "-f", "-c", "%T", str(path)],
                                    check=True, capture_output=True, text=True).stdout.strip()
        if filesystem in {"tmpfs", "ramfs"}:
            raise RuntimeError("runtime and temporary directories must live on disk")
    # The stat subprocesses above are preflight only; exactly one CLI is launched below.
    baseline = scope_processes(scope)
    guard_samples = [guard(output)]
    attestation = runtime_attestation(args.source_root, args.source_sha)
    write_json(output / "outer-runtime.json", attestation)
    write_json(output / "dependencies.json", attestation["dependencies"])

    class Fixture(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_GET(self) -> None:
            body = ("<!doctype html><meta charset=utf-8><title>Loopback fixture</title><p>Fixture "
                    + ("fresh" if self.path == "/fresh" else "first") + "</p>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    process = None
    seen, intervention, blocking, failure = {}, [], None, None
    code = None
    flags = ["--disable-gpu", "--proxy-server=127.0.0.1:9",
             "--proxy-bypass-list=<-loopback>;127.0.0.1;localhost;*.localhost",
             "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
             f"--log-net-log={output / 'chrome-netlog.json'}", "--net-log-capture-mode=Default"]
    env = {key: os.environ[key] for key in SAFE_ENV if key in os.environ}
    env.update(QAJEV_HOME=str(output / "state"),
               TMPDIR=str(temporary), BH_RUNTIME_DIR=str(runtime),
               QAJEV_CHROME=str(args.chrome.resolve()), QAJEV_CHROME_FLAGS=shlex.join(flags),
               PYTHONPATH=str(args.source_root), PYTHONDONTWRITEBYTECODE="1", BH_UPDATE_CHECK="0",
               QAJEV_ENV_FILE=str(output / "empty.env"), QAJEV_RUNNER="local",
               WATCHDOG_PROOF_SCOPE=args.scope_name,
               WATCHDOG_PROOF_SOURCE=json.dumps({"root": str(args.source_root), "sha": args.source_sha}),
               WATCHDOG_PROOF_TRACE=str(output / "identities.jsonl"))
    try:
        prepare(output, f"http://127.0.0.1:{server.server_port}")
        command = [sys.executable, "-P", str(Path(__file__).resolve()), "--_cli", "run", str(output / "suite.json"),
                   "--runner", "local", "--headless", "--ephemeral", "--devices", "desktop", "--jobs", "1",
                   "--ux", "off", "--cost-cap", "0", "--env-file", str(output / "empty.env"),
                   "--out", str(output / "reports"), "--no-shots", "--json", "--quiet", "--allow-commands"]
        write_json(output / "invocation.json", {"argv": command, "environment_names": sorted(env),
                   "chrome_flags": flags, "scope": str(scope), "memory_max": 8 * 1024**3, "memory_swap_max": 0,
                   "baseline_processes": list(baseline.values()), "chrome_temp_root_override": str(temporary)})
        with (output / "cli.stdout").open("w") as stdout, (output / "cli.stderr").open("w") as stderr:
            guard_samples.append(guard(output))
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr, env=env,
                                       cwd=output, start_new_session=True)
            started = time.monotonic()
            while process.poll() is None:
                for pid, record in scope_processes(scope).items():
                    if pid not in baseline:
                        seen[(pid, record["start_ticks"])] = record
                marker = output / "blocking-command.pid"
                if blocking is None and marker.exists():
                    blocking = proc_record(int(marker.read_text().strip()))
                    if blocking:
                        seen[(blocking["pid"], blocking["start_ticks"])] = blocking
                guard_samples.append(guard(output))
                if time.monotonic() - started > args.max_seconds:
                    raise RuntimeError("proof wall-clock limit reached")
                time.sleep(0.25)
            code = process.wait()
    except (OSError, RuntimeError, ValueError) as error:
        failure = f"{type(error).__name__}: {error}"
    finally:
        if process is not None and process.poll() is None:
            intervention.append("interrupt CLI after proof failure")
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                for pid, record in scope_processes(scope).items():
                    if pid not in baseline:
                        seen[(pid, record["start_ticks"])] = record
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    for record in seen.values():
                        signal_owned(record, sig)
                    time.sleep(1)
                process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    # Give ordinary child reaping a bounded settling period; never hide intervention.
    remaining = []
    for _ in range(40):
        remaining = [record for pid, record in scope_processes(scope).items() if pid not in baseline]
        if not remaining:
            break
        time.sleep(0.25)
    aftermath = {"scope": str(scope), "recorded_owned": list(seen.values()), "remaining_owned": remaining,
                 "blocking_command_observed": blocking, "intervention": intervention,
                 "scope_external_verification_required": True}
    write_json(output / "aftermath.json", aftermath)
    write_json(output / "resource-guard.json", guard_samples)
    if failure:
        raise RuntimeError(failure)
    if code is None:
        raise RuntimeError("the single CLI subprocess did not return an exit status")
    trace_rows = [json.loads(line) for line in (output / "identities.jsonl").read_text().splitlines()]
    return verify(output, code, trace_rows, aftermath)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", "--plan", dest="prepare", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--source-sha", required=True, help="exact archive source commit, recorded by the launcher")
    parser.add_argument("--out", required=True, type=Path, help="new artifact directory; never overwritten")
    parser.add_argument("--port", type=int, default=0, help="loopback port; 0 selects a free port for execution")
    parser.add_argument("--chrome", type=Path)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--temp-dir", type=Path)
    parser.add_argument("--scope-name")
    parser.add_argument("--max-seconds", type=float, default=180)
    args = parser.parse_args(argv)
    args.source_root = args.source_root.resolve()
    sys.path.insert(0, str(args.source_root))
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=False)
    try:
        write_json(output / "source.json", source_info(args.source_root, args.source_sha))
        if args.prepare:
            result = prepare(output, f"http://127.0.0.1:{args.port or 8877}")
        else:
            if not all((args.chrome, args.runtime_dir, args.temp_dir, args.scope_name)):
                raise RuntimeError("--execute needs --chrome, --runtime-dir, --temp-dir, --scope-name")
            if not 30 <= args.max_seconds <= 180:
                raise RuntimeError("--max-seconds must be between 30 and 180")
            result = execute(args, output)
        write_json(output / "receipt.json", result)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (AssertionError, OSError, RuntimeError, ValueError, KeyError,
            importlib.metadata.PackageNotFoundError) as error:
        receipt = {"gate": "FAIL", "error": f"{type(error).__name__}: {error}"}
        write_json(output / "receipt.json", receipt)
        print(json.dumps(receipt), file=sys.stderr)
        return 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--_cli":
        raise SystemExit(cli_child(sys.argv[2:]))
    raise SystemExit(main())
