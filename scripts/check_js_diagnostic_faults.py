#!/usr/bin/env python3
"""Mutate disposable copies only. Every fault must fail its named behavioral test."""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/test_js_diagnostics.py::"
FAULTS = [
    ("omit failure values", "qajev/verdict.py", 'detail = f"{detail}; observed {preview}"',
     "detail = detail", "test_failed_js_shows_declared_actual_and_expected_values"),
    ("let diagnostic truthiness pass", "qajev/verdict.py", 'preview = diagnostic_preview(result["diagnostic"])',
     'ok = bool(result["diagnostic"]); preview = diagnostic_preview(result["diagnostic"])',
     "test_diagnostic_cannot_turn_false_into_a_pass_and_large_data_stays_a_preview"),
    ("keep sensitive fields", "qajev/verdict.py", '"[redacted]" if sensitive.search(str(k)) else safe(item, depth + 1)',
     "safe(item, depth + 1)", "test_diagnostic_values_are_redacted_before_they_enter_a_check"),
    ("keep remembered secret values", "qajev/verdict.py", "text = redact(v)", "text = v",
     "test_diagnostic_values_are_redacted_before_they_enter_a_check"),
    ("explain a passing check", "qajev/session.py", " || __qajevCheckResult.value === true", "",
     "test_the_assertion_runs_once_and_its_explanation_runs_only_after_failure"),
    ("read accessors", "qajev/session.py",
     "descriptor && 'value' in descriptor ? bounded(descriptor.value, depth + 1) : '[accessor not read]'",
     "bounded(value[key], depth + 1)", "test_page_side_diagnostics_bound_data_and_never_invoke_accessors"),
    ("drop page-side string bound", "qajev/session.py", "value.length > 128 ? '[string too long]' : value", "value",
     "test_page_side_diagnostics_bound_data_and_never_invoke_accessors"),
    ("explain an assertion error", "qajev/session.py", "'error' in __qajevCheckResult || ", "",
     "test_an_assertion_error_does_not_run_failure_diagnostics"),
]


def main():
    results = []
    for name, source, before, after, test in FAULTS:
        with tempfile.TemporaryDirectory(prefix="qajev-js-fault-") as folder:
            target = Path(folder)
            shutil.copytree(ROOT / "qajev", target / "qajev", ignore=shutil.ignore_patterns("__pycache__"))
            (target / "tests").mkdir()
            for filename in ("conftest.py", "test_js_diagnostics.py"):
                shutil.copyfile(ROOT / "tests" / filename, target / "tests" / filename)
            path = target / source
            content = path.read_text()
            if content.count(before) != 1:
                raise RuntimeError(f"{name}: mutation anchor is not unique")
            path.write_text(content.replace(before, after, 1))
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", TEST + test], cwd=target,
                                    capture_output=True, text=True, timeout=60)
            output = result.stdout + result.stderr
            caught = result.returncode == 1 and f"FAILED {TEST}{test}" in output
            results.append({"fault": name, "caught": caught, "test": test})
            print(json.dumps(results[-1]), flush=True)
            if not caught:
                print(output)
                return 1
    print(json.dumps({"caught": sum(r["caught"] for r in results), "total": len(results)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
