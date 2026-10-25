"""Guarded Linux proof of native capture preparation; no application suites or models."""

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

LEGACY_SESSION_SHA256 = "717f233ebd015d4e300a395a13828ac86dc3c7e16678c9d51f75c2c714da2569"
PREPARATION = (
    "        # An inactive or minimized Linux surface can wait a full CDP timeout for its next frame.\n"
    '        if method == "Page.captureScreenshot" and sys.platform == "linux":\n'
    '            self.browser.call("Page.bringToFront")\n'
)
WRAPPED_CONSOLE = (
    "    for (const key of ['page_console', 'page_console_omitted'])\n"
    "      probe[key] = (probe[key] || []).concat(after[key] || []);\n"
)
LEGACY_CONSOLE = (
    "    for (const key of ['page_console', 'page_console_omitted']) "
    "probe[key] = (probe[key] || []).concat(after[key] || []);\n"
)
UNIT = "tests/test_capture_latency.py"
LIVE = "tests/test_capture_latency_live.py"
CHANGED = ["qajev/session.py", UNIT, LIVE, "tools/check-capture-faults.py", "tools/prove-capture-latency.py"]


def guard(limit):
    if os.getloadavg()[1] > limit:
        raise RuntimeError("STOPPED: five-minute load limit")
    if shutil.disk_usage(Path.cwd()).free < 20 * 1024**3:
        raise RuntimeError("STOPPED: disk free below 20 GiB")


def run(command, root, env, log, limit):
    child = None
    try:
        with log.open("w") as stream:
            child = subprocess.Popen(command, cwd=root, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                     start_new_session=True)
            deadline = time.monotonic() + 1800
            while child.poll() is None:
                guard(limit)
                if time.monotonic() >= deadline:
                    raise RuntimeError("STOPPED: proof deadline")
                time.sleep(1)
        return child.returncode
    finally:
        if child is not None and child.poll() is None:
            os.killpg(child.pid, signal.SIGINT)
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=10)


def legacy(root, out):
    """The production module must equal the pre-change file byte for byte, not a guessed baseline."""
    stage = out / "legacy"
    stage.mkdir()
    for name in ("qajev", "tests"):
        shutil.copytree(root / name, stage / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("qajev_upload_privacy.py", "pyproject.toml"):
        shutil.copy2(root / name, stage / name)
    path = stage / "qajev/session.py"
    text = path.read_text()
    assert text.count(PREPARATION) == 1, "capture preparation anchor changed"
    text = text.replace(PREPARATION, "", 1).replace("import sys\n", "", 1)
    assert text.count(WRAPPED_CONSOLE) == 1, "formatting anchor changed"
    text = text.replace(WRAPPED_CONSOLE, LEGACY_CONSOLE, 1)
    assert hashlib.sha256(text.encode()).hexdigest() == LEGACY_SESSION_SHA256, "baseline differs from main"
    path.write_text(text)
    return stage


def cases(xml):
    return ET.parse(xml).findall(".//testcase")


def diagnostics(log, root):
    data = json.loads(log.read_text())
    return data, Counter((str(Path(d["file"]).relative_to(root)), d["severity"], d["message"], d.get("rule"))
                         for d in data["generalDiagnostics"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("red-unit", "red-live", "unit", "live", "faults", "lint", "types", "full"))
    parser.add_argument("--max-load", type=float, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    assert sys.platform == "linux", "Linux fixture job only"
    assert args.max_load > 0
    os.setpriority(os.PRIO_PROCESS, 0, 19)
    root, out = Path.cwd().resolve(), args.out.resolve()
    guard(args.max_load)
    out.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ)
    for key in ("QAJEV_LIVE", "QAJEV_UX_CDP", "QAJEV_UX_ALLOW_HEADLESS", "QAJEV_CHROME_PASSWORD_LIVE",
                "QAJEV_BROWSER_POOL_LIVE", "QAJEV_CHROME_FLAGS", "QAJEV_CHROME_NETWORK_LIVE", "QAJEV_CAPTURE_LIVE"):
        env.pop(key, None)
    env["PYTHONPATH"] = str(root)
    receipt = {"mode": args.mode, "max_load5": args.max_load, "nice": 19, "gpu": False,
               "source_session_sha256": hashlib.sha256((root / "qajev/session.py").read_bytes()).hexdigest(),
               "gate": "FAIL"}
    result = 1
    try:
        work = legacy(root, out) if args.mode.startswith("red-") else root
        env["PYTHONPATH"] = str(work)
        if args.mode in {"live", "red-live"}:
            env["QAJEV_CAPTURE_LIVE"] = "1"
        pytest = [sys.executable, "-m", "pytest", "-q", "--tb=short", f"--junitxml={out / 'junit.xml'}",
                  f"--basetemp={out / 'fixtures'}"]
        commands = {
            "red-unit": [*pytest, UNIT],
            "red-live": [*pytest, LIVE, "-k", "(minimized and react-jpeg) or cli_retains"],
            "unit": [*pytest, UNIT, "tests/test_keys.py", "tests/test_linux.py", "tests/test_vision_claims.py"],
            "live": [*pytest, LIVE],
            "faults": [sys.executable, "tools/check-capture-faults.py", "--source", str(root),
                       "--out", str(out / "faults")],
            "lint": [sys.executable, "-m", "ruff", "check", *CHANGED],
            "full": pytest,
        }
        if args.mode == "types":
            baseline = legacy(root, out)
            command = [sys.executable, "-m", "basedpyright", "qajev", "--pythonpath", sys.executable, "--outputjson"]
            baseline_exit = run(command, baseline, {**env, "PYTHONPATH": str(baseline)},
                                out / "baseline-types.json", args.max_load)
            candidate_exit = run(command, root, env, out / "candidate-types.json", args.max_load)
            before, old = diagnostics(out / "baseline-types.json", baseline)
            after, new = diagnostics(out / "candidate-types.json", root)
            added = list((new - old).elements())
            receipt.update(baseline_exit=baseline_exit, candidate_exit=candidate_exit,
                           baseline_summary=before["summary"], candidate_summary=after["summary"],
                           new_diagnostics=added)
            result = 0 if baseline_exit in {0, 1} and candidate_exit in {0, 1} and not added else 1
            receipt["gate"] = ("UNCHANGED_BASELINE_DIAGNOSTICS" if result == 0 and candidate_exit else
                               "PASS" if result == 0 else "FAIL")
        else:
            actual_exit = run(commands[args.mode], work, env, out / "output.log", args.max_load)
            receipt["command_exit"] = actual_exit
            result = actual_exit
            if args.mode.startswith("red-") or args.mode == "live":
                tests = cases(out / "junit.xml")
                failures = [failure for test in tests for failure in test.findall("failure")]
                skipped = sum(test.find("skipped") is not None for test in tests)
                errors = sum(test.find("error") is not None for test in tests)
                receipt.update(tests=len(tests), failures=len(failures), skipped=skipped, errors=errors)
                if args.mode.startswith("red-"):
                    slow_cli = False
                    if args.mode == "red-live":
                        for log in (out / "fixtures").glob("test_cli*/cli.stdout"):
                            if log.parent.name.endswith("current"):
                                continue
                            data = json.loads(log.read_text())
                            frames = [frame for scenario in data.get("scenarios", [])
                                      for frame in scenario.get("react", []) if "took_s" in frame]
                            maximum = max((frame["took_s"] for frame in frames), default=0)
                            described = all(item.get("described") is True for item in data["plan"])
                            hook_failed = any(scenario.get("stop") == "hook_failed"
                                              for scenario in data["scenarios"])
                            slow_cli = (maximum >= 5 and described and hook_failed and
                                        data["cost"]["calls"] == {"typesafe": 0, "text": 0})
                            receipt["baseline_cli"] = {
                                "gate": data["gate"], "cost_calls": data["cost"]["calls"],
                                "frames": frames, "seconds": data["seconds"], "timing": data["timing"],
                                "max_capture_seconds": maximum, "plan_described": described,
                                "hook_failed": hook_failed}
                    assertion = any("assert " in (f.text or "") or "DID NOT RAISE" in (f.text or "") for f in failures)
                    reproduced = args.mode == "red-unit" or slow_cli or any(
                        "capture must retain a frame" in (f.text or "") or "capture took" in (f.text or "")
                        for f in failures)
                    red = actual_exit == 1 and tests and not skipped and not errors and assertion and reproduced
                    result = 0 if red else 1
                    receipt["baseline_session_sha256"] = LEGACY_SESSION_SHA256
                elif not tests or skipped or errors:
                    result = 1
            receipt["gate"] = "PASS" if result == 0 else "FAIL"
    except BaseException as error:
        receipt["stopped"] = type(error).__name__
        raise
    finally:
        receipt["exit"] = result
        (out / "RESULT.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt), flush=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
