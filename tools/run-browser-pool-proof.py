"""Bounded Linux-only fixture proof. No GPU lock, model calls or external network needed."""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


def guard(limit):
    if os.getloadavg()[1] > limit:
        raise RuntimeError(f"STOPPED: five-minute load exceeds {limit}")
    if shutil.disk_usage(Path.cwd()).free < 20 * 1024**3:
        raise RuntimeError("STOPPED: disk free below 20 GiB")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["unit", "live", "full", "types", "lint", "faults"])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--box", choices=["workload-cpu", "workload-gpu"], default="workload-cpu")
    args = parser.parse_args()
    assert sys.platform == "linux", "Linux fixture proof only"
    os.setpriority(os.PRIO_PROCESS, 0, 19)
    limit = 7 if args.box == "workload-gpu" else 16
    guard(limit)
    args.out.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path.cwd())
    for key in ("QAJEV_LIVE", "QAJEV_UX_CDP", "QAJEV_UX_ALLOW_HEADLESS", "QAJEV_BROWSER_POOL_LIVE"):
        env.pop(key, None)
    pytest = [sys.executable, "-m", "pytest", "-q", "--tb=short", f"--junitxml={args.out / 'junit.xml'}",
              f"--basetemp={args.out / 'fixtures'}"]
    commands = {
        "unit": [*pytest, "tests/test_browser_pool.py", "tests/test_jobs.py", "tests/test_chrome_privacy.py",
                 "tests/test_cleanup.py", "tests/test_local_runs_off.py", "tests/test_openurl.py",
                 "tests/test_smoke_live.py", "tests/test_mcp_tools.py"],
        "live": [*pytest, "tests/test_browser_pool_live.py"],
        "full": pytest,
        "types": [sys.executable, "-m", "basedpyright", "qajev", "tests/test_browser_pool.py",
                  "tests/test_browser_pool_live.py", "--pythonpath", sys.executable],
        "lint": [sys.executable, "-m", "ruff", "check", "qajev/browser_pool.py", "qajev/jobs.py", "qajev/cli.py",
                 "qajev/chrome.py", "qajev/runner.py", "qajev/smoke.py", "qajev/mcp_server.py",
                 "tests/test_browser_pool.py", "tests/test_browser_pool_live.py", "tools/run-browser-pool-proof.py",
                 "tools/check-browser-pool-faults.py"],
        "faults": [sys.executable, "tools/check-browser-pool-faults.py", "--source", str(Path.cwd()),
                   "--out", str(args.out / "faults")],
    }
    if args.mode == "live":
        env["QAJEV_BROWSER_POOL_LIVE"] = "1"
    child = None
    stopped = None
    try:
        with (args.out / "output.log").open("w") as log:
            child = subprocess.Popen(commands[args.mode], env=env, stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + (1800 if args.mode in {"full", "faults"} else 600)
            while child.poll() is None:
                guard(limit)
                if time.monotonic() >= deadline:
                    raise RuntimeError("STOPPED: fixture proof deadline")
                time.sleep(2)
    except BaseException as error:
        stopped = type(error).__name__
        raise
    finally:
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGINT)
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        receipt = {"mode": args.mode, "exit": child.returncode if child else None, "stopped": stopped,
                   "box": args.box, "nice": 19, "gpu": False, "model_calls": 0}
        (args.out / "RESULT.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt))
    return child.returncode


if __name__ == "__main__":
    raise SystemExit(main())
