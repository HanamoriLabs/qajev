"""Restored Chrome-environment faults; run in a network-isolated Linux CPU job."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

UNIT = "tests/test_chrome_environment.py::"
REGRESSION = UNIT + "test_local_jobs_keep_credentials_out_of_the_launched_chrome"
RUNTIME = UNIT + "test_runtime_allowlist_is_exact_and_does_not_mutate_the_parent"
C, S = "qajev/chrome.py", "qajev/session.py"
FAULTS = [
    ("E01-inherit-default", C, '    env = child_env(temporary=temporary)',
     '    env = None if temporary is None else child_env(temporary=temporary)', REGRESSION + '[False-2]'),
    ("E02-inherit-private-temp", C, '    env = child_env(temporary=temporary)',
     '    env = dict(os.environ) if temporary is not None else child_env(temporary=temporary)',
     REGRESSION + '[True-2]'),
    ("E03-allow-model-key", C, '"PATH", "HOME", "USER", "LOGNAME",',
     '"TYPESAFE_API_KEY", "PATH", "HOME", "USER", "LOGNAME",', REGRESSION + '[False-1]'),
    ("E04-allow-runtime-prefixes", C, 'if name in CHROME_ENV_NAMES}',
     'if name in CHROME_ENV_NAMES or name.startswith(("LC_", "XDG_", "MESA_"))}', RUNTIME),
    ("E05-drop-home", C, 'if name in CHROME_ENV_NAMES}',
     'if name in CHROME_ENV_NAMES and name != "HOME"}', RUNTIME),
    ("E06-drop-owned-temp", C, '        env.update(TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary))',
     '        pass', RUNTIME),
    ("E07-mutate-parent", C, '    return env\n',
     '    os.environ.pop("TYPESAFE_API_KEY", None)\n    return env\n', RUNTIME),
    ("E08-reuse-legacy", C,
     '            if running[0].get("environment_isolated") is not True:\n',
     '            if False:\n',
     UNIT + 'test_legacy_browser_requires_restart_without_spawning_or_reading_its_environment'),
    ("E09-omit-state-marker", C, '        "environment_isolated": True,\n', '', REGRESSION + '[False-1]'),
    ("E10-filter-command-hooks", S,
     '        env = {k: v for k, v in os.environ.items() if v != providers.PLACEHOLDER}',
     '        env = chrome.child_env()', UNIT + 'test_command_hooks_keep_their_existing_authorized_environment'),
    ("E11-omit-stop-command", C,
     '                raise ChromeError(f"Restart this QAJev Chrome profile '
     'to isolate its environment: run {command}")',
     '                raise ChromeError("Restart this QAJev Chrome profile to isolate its environment")',
     UNIT + 'test_legacy_browser_names_a_shell_safe_stop_command[fixture]'),
    ("E12-unquoted-profile", C,
     '                command = shlex.join(["qajev", "browser", "stop", "--profile", profile])',
     '                command = "qajev browser stop --profile " + profile',
     UNIT + "test_legacy_browser_names_a_shell_safe_stop_command[fixture's browser]"),
]


def guard():
    assert sys.platform == "linux" and os.getpriority(os.PRIO_PROCESS, 0) == 19
    assert os.getloadavg()[1] <= 16 and shutil.disk_usage(Path.cwd()).free >= 20 * 1024**3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    guard()
    root = Path(__file__).resolve().parents[1]
    args.out.mkdir(parents=True, exist_ok=False)
    stage = args.out / "source"
    shutil.copytree(root, stage, ignore=shutil.ignore_patterns(
        ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"))
    originals = {path: (stage / path).read_bytes() for _, path, *_ in FAULTS}
    rows = []

    def run(name, test):
        guard()
        xml = args.out / (name + ".xml")
        env = {**os.environ, "PYTHONPATH": str(stage) + os.pathsep + str(stage / "tests")}
        with (args.out / (name + ".log")).open("w") as output:
            result = subprocess.run([sys.executable, "-m", "pytest", test, "-q", "--junitxml=" + str(xml)],
                                    cwd=stage, env=env, stdout=output, stderr=subprocess.STDOUT, timeout=120)
        cases = list(ET.parse(xml).iter("testcase"))
        failures = [c.find("failure") for c in cases if c.find("failure") is not None]
        assert cases and not any(c.find("error") is not None or c.find("skipped") is not None for c in cases)
        return {"exit": result.returncode, "failures": len(failures),
                "assertion": any("AssertionError" in (f.text or "") or "DID NOT RAISE" in (f.text or "")
                                 for f in failures)}

    try:
        for name, path, before, after, test in FAULTS:
            target = stage / path
            original = originals[path]
            assert original.decode().count(before) == 1, name
            control = run(name + "-control", test)
            assert control["exit"] == 0 and control["failures"] == 0, control
            try:
                target.write_text(original.decode().replace(before, after, 1))
                fault = run(name + "-fault", test)
                assert fault["exit"] == 1 and fault["assertion"], fault
                rows.append({"fault": name, "test": test, "control": control, "fault_result": fault})
            finally:
                target.write_bytes(original)
    finally:
        for path, data in originals.items():
            (stage / path).write_bytes(data)
        (args.out / "RESULT.json").write_text(json.dumps({"caught": len(rows), "total": len(FAULTS),
            "sources_restored": all((stage / path).read_bytes() == data for path, data in originals.items()),
            "matrix": rows,
            "source_sha256": {path: hashlib.sha256(data).hexdigest() for path, data in originals.items()},
        }, indent=2) + "\n")


if __name__ == "__main__":
    main()
