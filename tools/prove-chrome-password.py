"""Prove password entry on a synthetic local fixture; Linux GPU lock required, no models or app account."""

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--chrome", default="/usr/bin/chromium")
    parser.add_argument("--select", default="")
    args = parser.parse_args()
    if sys.platform != "linux" or os.readlink("/proc/self/fd/7") != "/tmp/gpu-heavy.lock":
        raise SystemExit("REFUSED: use the Linux GPU fixture lock")
    if os.getpriority(os.PRIO_PROCESS, 0) != 19 or os.getloadavg()[0] > 60:
        raise SystemExit("REFUSED: nice 19 and load1 <= 60 required")
    os.umask(0o077)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    flags = [
        "--disable-background-networking", "--disable-component-update", "--disable-sync", "--disable-quic",
        "--disable-default-apps", "--disable-domain-reliability", "--disable-client-side-phishing-detection",
        "--disable-component-extensions-with-background-pages", "--disable-extensions", "--no-pings", "--no-first-run",
        "--metrics-recording-only", "--safebrowsing-disable-auto-update",
        "--disable-features=Translate,MediaRouter,OptimizationHints,AutofillServerCommunication,"
        "OptimizationGuideModelDownloading,NetworkTimeServiceQuerying,AimServerEligibilityEnabled,"
        "AimServerRequestOnStartupEnabled",
        "--proxy-server=127.0.0.1:9", "--proxy-bypass-list=<-loopback>;127.0.0.1;localhost;*.localhost",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
    ]
    for service in ("gaia-url", "google-apis-url", "google-url", "lso-url", "sync-url", "gcm-checkin-url",
                    "gcm-registration-url", "variations-server-url", "variations-insecure-server-url"):
        flags.append(f"--{service}=http://127.0.0.1:9/")
    flags.append("--component-updater=url-source=http://127.0.0.1:9/")
    env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "TMPDIR") if key in os.environ}
    env.update(PYTHONPATH=str(root), QAJEV_CHROME_PASSWORD_LIVE="1", QAJEV_CHROME=args.chrome,
               QAJEV_CHROME_FLAGS=shlex.join(flags))
    command = [sys.executable, "-P", "-m", "pytest",
               "tests/test_chrome_privacy.py", "tests/test_chrome_privacy_live.py",
               "-vv", "--tb=short", f"--basetemp={out / 'fixtures'}", f"--junitxml={out / 'junit.xml'}"]
    if args.select:
        command += ["-k", args.select]
    with (out / "tests.log").open("wb") as log:
        process = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, pass_fds=(7,))
        try:
            while process.poll() is None:
                if os.getloadavg()[0] > 60:
                    raise RuntimeError("STOPPED: load1 exceeds 60")
                time.sleep(0.5)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    receipt = {"test_exit": process.returncode, "runner": "local", "model_calls": 0,
               "scope": "synthetic local password fixture, no app account", "fixtures": str(out / "fixtures")}
    (out / "RESULT.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
