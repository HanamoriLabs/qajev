"""Opt-in real CLI overlap, local HTTP fixtures, no models or outside network.

Run only inside the approved Linux network-disabled fixture job, with
QAJEV_BROWSER_POOL_LIVE=1. Own and close every process, server and profile.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("QAJEV_BROWSER_POOL_LIVE") != "1",
                                reason="opt-in network-disabled Linux CLI fixture")


@pytest.mark.parametrize("capacity", [2, 4])
def test_real_cli_runs_overlap_with_isolated_chromes_and_excess_runs_queue(tmp_path, capacity):
    assert sys.platform == "linux"
    binary = shutil.which("chromium") or shutil.which("google-chrome")
    assert binary, "owned fixture Chrome required"
    arrived = []
    gate = threading.Barrier(capacity)
    lock = threading.Lock()

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/fixture"):
                with lock:
                    arrived.append(time.time())
                gate.wait(timeout=45)
                time.sleep(3)  # prove a nonzero overlap after both browsers have reached the fixture
            body = b"<!doctype html><title>Pool fixture</title><h1>Isolated browser fixture</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = Path(__file__).resolve().parents[1]
    processes, outputs = [], []
    try:
        for lane in range(2 * capacity):
            # N=2 proves admission across homes; N=4 proves resource isolation with one shared home/profile name.
            home = tmp_path / f"home-{lane if capacity == 2 else 0}"
            home.mkdir(mode=0o700, exist_ok=True)
            env_file = home / "empty.env"
            env_file.touch(mode=0o600)
            env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG") if key in os.environ}
            env.update(PYTHONPATH=str(root), QAJEV_HOME=str(home), QAJEV_CHROME=binary,
                       QAJEV_RUNNER="local", QAJEV_OUTLIVE_PARENT="1", QAJEV_LOCK_WAIT="120",
                       QAJEV_BROWSER_RAM_MIB="128", QAJEV_BROWSER_RESERVE_MIB="128",
                       QAJEV_CHROME_FLAGS="--disable-gpu --disable-background-networking "
                                          "--proxy-server=http://127.0.0.1:9 "
                                          "--proxy-bypass-list=127.0.0.1;localhost;*.localhost")
            stdout = (tmp_path / f"lane-{lane}.json").open("w")
            stderr = (tmp_path / f"lane-{lane}.events").open("w")
            outputs.extend([stdout, stderr])
            argv = [sys.executable, "-m", "qajev", "check",
                    f"http://127.0.0.1:{server.server_port}/fixture?lane={lane}",
                    "--runner", "local", "--browser-runs", str(capacity), "--headless", "--env-file", str(env_file),
                    "--out", str(tmp_path / "reports"), "--devices", "desktop", "--settle", "1",
                    "--expect-text", "Isolated browser fixture", "--about", "A local browser reaches its fixture",
                    "--ux", "off", "--load-high", "0", "--no-shots", "--json", "--events"]
            processes.append(subprocess.Popen(argv, env=env, cwd=root, stdout=stdout, stderr=stderr))
        deadline = time.monotonic() + 180
        for lane, process in enumerate(processes):
            code = process.wait(timeout=max(1, deadline - time.monotonic()))
            assert code == 0, (tmp_path / f"lane-{lane}.json").read_text()
        reports = [json.loads((tmp_path / f"lane-{n}.json").read_text()) for n in range(2 * capacity)]
        assert all(r["gate"] == "PASS" and r["cost"]["usd"] == 0 for r in reports)
        assert len(arrived) == 2 * capacity
        intervals = [(r["timing"]["admitted_at"],
                      r["timing"]["admitted_at"] + r["timing"]["run_seconds"]) for r in reports]
        simultaneous = sorted(intervals)[:capacity]
        assert min(end for _, end in simultaneous) - max(start for start, _ in simultaneous) >= 3
        assert max(sum(start <= point < end for start, end in intervals) for point, _ in intervals) == capacity
        assert sorted(r["timing"]["queue_seconds"] for r in reports)[capacity] >= 3
        for key in ("port", "state_key", "profile_dir", "temp_dir"):
            assert len({r["browser"][key] for r in reports}) == 2 * capacity, key
        assert len({r["run_dir"] for r in reports}) == 2 * capacity
        for report in reports:
            assert report["browser"]["ephemeral"] is True
            assert not Path(report["browser"]["profile_dir"]).exists()
            assert not Path(report["browser"]["temp_dir"]).exists()
            assert report["timing"]["run_seconds"] >= 3
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for handle in outputs:
            handle.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
