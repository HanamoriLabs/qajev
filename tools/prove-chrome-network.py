"""Linux-only local-fixture proof. No GPU or model calls; command network is isolated by the runner."""

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
        raise RuntimeError("STOPPED: five-minute load limit")
    if shutil.disk_usage(Path.cwd()).free < 20 * 1024**3:
        raise RuntimeError("STOPPED: disk free below 20 GiB")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["unit", "live", "full", "lint", "types", "faults"])
    parser.add_argument("--max-load", type=float, required=True, help="operator-approved five-minute load ceiling")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    assert sys.platform == "linux", "Linux local fixture only"
    os.setpriority(os.PRIO_PROCESS, 0, 19)
    if args.max_load <= 0:
        parser.error("--max-load must be positive")
    limit = args.max_load
    guard(limit)
    args.out.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ)
    for key in ("QAJEV_LIVE", "QAJEV_UX_CDP", "QAJEV_UX_ALLOW_HEADLESS", "QAJEV_CHROME_PASSWORD_LIVE",
                "QAJEV_BROWSER_POOL_LIVE", "QAJEV_CHROME_FLAGS", "QAJEV_CHROME_NETWORK_LIVE"):
        env.pop(key, None)
    env["PYTHONPATH"] = str(Path.cwd())
    pytest = [sys.executable, "-m", "pytest", "-q", "--tb=short", f"--junitxml={args.out / 'junit.xml'}",
              f"--basetemp={args.out / 'fixtures'}"]
    paths = ["qajev/chrome.py", "tests/test_chrome_privacy.py", "tests/test_chrome_network_live.py",
             "tests/test_chrome_network_audit.py", "tools/chrome-netlog-audit.py", "tools/prove-chrome-network.py",
             "tools/check-chrome-network-faults.py"]
    commands = {
        "unit": [*pytest, "tests/test_chrome_privacy.py", "tests/test_chrome_network_audit.py"],
        "live": [*pytest, "tests/test_chrome_network_live.py"],
        "full": pytest,
        "lint": [sys.executable, "-m", "ruff", "check", *(p for p in paths if Path(p).exists())],
        "types": [sys.executable, "-m", "basedpyright", "qajev", *(p for p in paths if Path(p).exists()),
                  "--pythonpath", sys.executable],
        "faults": [sys.executable, "tools/check-chrome-network-faults.py", "--source", str(Path.cwd()),
                   "--out", str(args.out / "faults")],
    }
    if args.mode == "live":
        env["QAJEV_CHROME_NETWORK_LIVE"] = "1"
    child = None
    stopped = None
    try:
        with (args.out / "output.log").open("w") as log:
            child = subprocess.Popen(commands[args.mode], env=env, stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 1800
            while child.poll() is None:
                guard(limit)
                if time.monotonic() >= deadline:
                    raise RuntimeError("STOPPED: proof deadline")
                time.sleep(1)
    except BaseException as error:
        stopped = type(error).__name__
        raise
    finally:
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGINT)
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
        receipt = {"mode": args.mode, "exit": child.returncode if child else None, "stopped": stopped,
                   "max_load": limit, "nice": 19, "gpu": False, "model_calls": 0}
        (args.out / "RESULT.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt))
    return child.returncode


if __name__ == "__main__":
    raise SystemExit(main())
