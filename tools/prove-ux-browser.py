"""Run the opt-in UX fixtures in one isolated box Chrome and close it after the batch.

No models, credentials, user browser profile or live service are used. The load
watchdog stops this tool's test process and Chrome if load1 exceeds 60.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def main():
    os.setpriority(os.PRIO_PROCESS, 0, 19)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--select", default="")
    parser.add_argument("--cloud-ui", type=Path, help="paired Cloud checkout's worker/public for fixture-only UI proof")
    parser.add_argument("--headless", action="store_true", required=True, help="isolated box-only proof")
    args = parser.parse_args()
    if os.uname().sysname == "Darwin":
        raise SystemExit("REFUSED: browser tests and debug ports are forbidden on the Mac")
    if os.getloadavg()[0] > 60:
        raise SystemExit("REFUSED: Mac load1 exceeds 60; no browser started")
    root = Path(__file__).resolve().parents[1]
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    profile = out / "profile"
    chrome = tests = None
    record = {"headed": not args.headless, "model_calls": 0, "profile": str(profile), "max_load1": 0}
    try:
        with (out / "chrome.log").open("wb") as stream:
            headless = ["--headless=new", "--disable-gpu"] if args.headless else []
            chrome = subprocess.Popen([str(args.chrome), *headless,
                                       "--remote-debugging-port=0", f"--user-data-dir={profile}",
                                       "--no-first-run", "--no-default-browser-check",
                                       "--disable-background-networking", "about:blank"], stdout=stream, stderr=stream)
        record["chrome_pid"] = chrome.pid
        deadline = time.monotonic() + 20
        active_port = profile / "DevToolsActivePort"
        while not active_port.exists():
            if chrome.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("Owned headed Chrome did not open its endpoint; see chrome.log")
            if os.getloadavg()[0] > 60:
                raise RuntimeError("STOPPED: load1 exceeds 60")
            time.sleep(0.1)
        endpoint = "http://127.0.0.1:" + active_port.read_text().splitlines()[0]
        with urllib.request.urlopen(endpoint + "/json/version", timeout=5) as response:
            record["browser"] = json.load(response)["Browser"]
        if "Headless" in record["browser"] and not args.headless:
            raise RuntimeError("A headed Chrome is required")
        command = [sys.executable, "-m", "pytest", "tests/test_ux_dom_live.py", "-vv", "--tb=short",
                   f"--basetemp={out / 'fixtures'}", f"--junitxml={out / 'junit.xml'}"]
        if args.select:
            command += ["-k", args.select]
        env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "TMPDIR") if key in os.environ}
        env["QAJEV_UX_CDP"] = endpoint
        if args.cloud_ui:
            env["QAJEV_UX_CLOUD_UI"] = str(args.cloud_ui.resolve())
        if args.headless:
            env["QAJEV_UX_ALLOW_HEADLESS"] = "1"
        with (out / "tests.log").open("wb") as log:
            tests = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
            while tests.poll() is None:
                load = os.getloadavg()[0]
                record["max_load1"] = max(record["max_load1"], load)
                if load > 60:
                    record["stopped_for_load"] = True
                    raise RuntimeError("STOPPED: Mac load1 exceeds 60")
                time.sleep(0.5)
        record["test_exit"] = tests.returncode
        return tests.returncode
    except (OSError, RuntimeError) as error:
        record["error"] = str(error)
        return 2
    finally:
        for process in (tests, chrome):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        record["chrome_closed"] = chrome is None or chrome.poll() is not None
        (out / "RESULT.json").write_text(json.dumps(record, indent=2))
        print(json.dumps(record))


if __name__ == "__main__":
    raise SystemExit(main())
