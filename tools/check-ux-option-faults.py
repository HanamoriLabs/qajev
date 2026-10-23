"""Prove UX option precedence and every public option wire on a private source copy.

Use the project's test interpreter on a Linux test box. No browser is launched.
Each mutation needs a passing control and a named assertion failure; evidence
includes exact edits, logs, JUnit and hashes. The checkout stays unchanged.
"""

import argparse
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

TEST_FILE = "tests/test_ux_options.py"
OWNED = ("qajev/ux_options.py", "qajev/cli.py", "qajev/mcp_server.py", "qajev/suite.py", TEST_FILE)
INVALID = "test_invalid_values_are_refused_by_the_shared_parser_and_suite"
MCP = "test_every_web_mcp_entry_point_forwards_explicit_options_and_preserves_suite_defaults"
DEFAULTS = "test_defaults_and_old_boolean_modes_are_resolved_without_shared_lists"
INHERIT = "test_suite_and_scenario_inherit_explicit_options_before_run_overrides"
FAULTS = [
    ("O01", "qajev/ux_options.py", "**parse(inherited), **parse(opts)", "**parse(opts), **parse(inherited)",
     "test_run_options_beat_inherited_values_and_none_keeps_the_suite_value", 0),
    ("O02", "qajev/ux_options.py", 'key == "ux_keyboard_ratio" and not 0 < value < 1', "False", INVALID, 0),
    ("O03", "qajev/ux_options.py", "for key, value in parse(values).items():",
     "for key, value in resolve(values).items():", MCP, 0),
    *[(f"O04-{name}", "qajev/mcp_server.py", "    args += _ux_args(locals())\n", "", MCP, index)
      for index, name in enumerate(("check", "suite", "smoke", "project"))],
    ("O05", "qajev/mcp_server.py", '"frame_requests", "effect", "ux")',
     '"frame_requests", "effect")', "test_trim_keeps_ux_findings_with_the_scenario", 0),
    ("O06", "qajev/cli.py", "        **ux_options.parse(args),",
     '        **{**ux_options.parse(args), "ux": "off"},',
     "test_every_web_cli_accepts_and_serializes_all_explicit_ux_flags", 0),
    ("O07", "qajev/suite.py", 'ux_options=_ux_options({**suite_ux, **_ux_options(item, where)}, where),',
     'ux_options=_ux_options(item, where),', INHERIT, 0),
    ("O08", "qajev/ux_options.py", "any(check not in CHECKS for check in checks)", "False", INVALID, 0),
    ("O09", "qajev/ux_options.py", 'not isinstance(out["ux_keyboard_press_return"], bool)',
     "False", INVALID, 0),
    ("O10", "qajev/ux_options.py", "not math.isfinite(value)", "False", INVALID, 0),
    ("O11", "qajev/ux_options.py", 'key == "ux_shift_px" and value < 0', "False", INVALID, 0),
    ("O12", "qajev/ux_options.py", 'key == "ux_tap_min_px" and value <= 0', "False", INVALID, 0),
    ("O13", "qajev/ux_options.py", 'return parse({**DEFAULTS, **parse(inherited), **parse(opts)})',
     'return parse({**DEFAULTS, **parse(inherited), **parse(opts), "ux": "advisory"})',
     "test_run_options_beat_inherited_values_and_none_keeps_the_suite_value", 0),
    ("O14", "qajev/ux_options.py", '"ux": "advisory",', '"ux": "off",', DEFAULTS, 0),
    ("O15", "qajev/ux_options.py", '"advisory" if out["ux"] else "off"',
     '"advisory"', DEFAULTS, 0),
    ("O16", "qajev/ux_options.py", 'out["ux_checks"] = list(dict.fromkeys(checks))',
     'out["ux_checks"] = checks', DEFAULTS, 0),
    ("O17", "qajev/ux_options.py",
     'if key in out and (isinstance(out[key], bool) or not isinstance(out[key], int) or out[key] < minimum):',
     'if False:', INVALID, 0),
    ("O18", "qajev/ux_options.py", 'if key in out and out[key] not in choices:',
     'if False:', INVALID, 0),
    ("O19", "qajev/ux_options.py", '"ux_keyboard_press_return": False,',
     '"ux_keyboard_press_return": True,', DEFAULTS, 0),
]


def hashes(root):
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in OWNED}


def check(stage, out, label, test=""):
    command = [sys.executable, "-B", "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider",
               "--junitxml=" + str(out / (label + ".xml")), TEST_FILE + ("::" + test if test else "")]
    env = dict(os.environ, PYTHONPATH=str(stage), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(command, cwd=stage, env=env, capture_output=True, text=True, timeout=90)
    (out / (label + ".log")).write_text(result.stdout + result.stderr)
    cases = list(ET.parse(out / (label + ".xml")).iter("testcase"))
    failures = [case for case in cases if case.find("failure") is not None]
    errors = [case.get("name") for case in cases if case.find("error") is not None]
    skipped = [case.get("name") for case in cases if case.find("skipped") is not None]
    assertions = [case.get("name") for case in failures
                  if (case.get("name") == test or case.get("name", "").startswith(test + "["))
                  and any(word in ET.tostring(case, encoding="unicode")
                          for word in ("AssertionError", "assert ", "DID NOT RAISE"))]
    return {"exit": result.returncode, "tests": len(cases), "errors": errors, "skipped": skipped,
            "passed": result.returncode == 0 and bool(cases) and not (failures or errors or skipped),
            "caught": result.returncode == 1 and bool(assertions) and len(assertions) == len(failures)
            and not (errors or skipped), "named_assertions": assertions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("run on the approved Linux test box")
    os.setpriority(os.PRIO_PROCESS, 0, 19)
    source, out = args.source.resolve(), args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    before = hashes(source)
    record = {"gate": "FAIL", "source_hashes": before, "faults": [], "model_calls": 0,
              "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    try:
        with tempfile.TemporaryDirectory(prefix="ux-option-faults-") as folder:
            stage = Path(folder)
            for name in ("qajev", "tests"):
                shutil.copytree(source / name, stage / name, ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy2(source / "qajev_upload_privacy.py", stage / "qajev_upload_privacy.py")
            (stage / "pytest.ini").write_text("[pytest]\n")
            record["control"] = check(stage, out, "control")
            if not record["control"]["passed"]:
                raise RuntimeError("unmodified control failed; see control.log")
            for ident, name, original, replacement, test, occurrence in FAULTS:
                control = check(stage, out, ident + "-control", test)
                if not control["passed"]:
                    raise RuntimeError(ident + " control failed; see its control log")
                path = stage / name
                text = path.read_text()
                expected = 4 if ident.startswith("O04-") else 1
                if text.count(original) != expected:
                    raise RuntimeError(ident + " mutation anchor count changed")
                start = 0
                for _ in range(occurrence + 1):
                    at = text.index(original, start)
                    start = at + len(original)
                mutated = text[:at] + replacement + text[at + len(original):]
                diff = "".join(difflib.unified_diff(text.splitlines(True), mutated.splitlines(True),
                                                   fromfile=name, tofile=name + ".fault"))
                try:
                    path.write_text(mutated)
                    fault = check(stage, out, ident + "-fault", test)
                finally:
                    path.write_text(text)
                record["faults"].append({"id": ident, "test": test, "diff": diff,
                                        "control": control, "fault": fault})
                if not fault["caught"] or hashes(stage) != before:
                    raise RuntimeError(ident + " missed or sources not restored")
                print(ident + ": CAUGHT by " + test, flush=True)
            record["restored_control"] = check(stage, out, "restored-control")
            if not record["restored_control"]["passed"]:
                raise RuntimeError("restored control failed")
        record["gate"] = "PASS"
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired, ET.ParseError) as error:
        record["error"] = str(error)
    finally:
        record["source_unchanged"] = hashes(source) == before
        if not record["source_unchanged"]:
            record["gate"] = "FAIL"
        record["caught"] = sum(item["fault"]["caught"] for item in record["faults"])
        record["total"] = len(FAULTS)
        (out / "RESULT.json").write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps({key: record.get(key) for key in ("gate", "caught", "total", "source_unchanged", "error")}))
    return 0 if record["gate"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
