#!/usr/bin/env python3
"""Each disposable mutation must fail its named behavioral regression."""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST = "tests/test_failure_reasons.py::"
FAULTS = [
    ("reject all-true named parts", "qajev/verdict.py", "return not failures,", "return False,",
     "test_named_js_parts_pass_only_when_every_value_is_literal_true"),
    ("accept truthy named values", "qajev/verdict.py", "if part is True:", "if part:",
     "test_js_object_failure_names_the_non_true_part_and_its_value[1]"),
    ("report only the first failed part", "qajev/verdict.py", '"; ".join(failures)', '"; ".join(failures[:1])',
     "test_every_failed_js_part_is_named_even_beyond_the_value_preview_limit"),
    ("discard stdout failure reasons", "qajev/verdict.py", ' or (result.get("stdout") or "").strip()', "",
     "test_command_failure_falls_back_to_the_last_400_stdout_characters[None]"),
    ("restore the stderr prefix", "qajev/verdict.py", "redact(output)[-400:]", "redact(output)[:200]",
     "test_command_failure_uses_the_last_400_stderr_characters_not_the_prefix"),
    ("redact after clipping secrets", "qajev/verdict.py", "redact(output)[-400:]", "redact(output[-400:])",
     "test_command_failure_redacts_before_selecting_the_tail"),
    ("use client truthiness", "qajev/clients.py", "ok, detail, value = verdict.js_result(page)",
     'ok, detail, value = bool(page.get("value")), None, None',
     "test_across_clients_judge_uses_the_same_named_failure_reasons"),
    ("drop nonserializable named parts", "qajev/session.py",
     'result = "(async () => (" + CHECK_VALUE_JS + ")(await " + awaited(expression, timeout_ms) + "))()"',
     "result = awaited(expression, timeout_ms)",
     "test_page_transport_cannot_drop_or_pass_a_non_true_named_part"),
    ("pass reason strings", "qajev/verdict.py", "return False, text[:300]", "return bool(text), text[:300]",
     "test_boolean_and_string_js_contract_is_preserved"),
    ("expose sensitive part values", "qajev/verdict.py", 'diagnostic_preview("[redacted]" if hidden else part)',
     "diagnostic_preview(part)", "test_named_js_values_and_registered_secrets_are_redacted_before_emission"),
]


def main():
    results = []
    for name, source, before, after, test in FAULTS:
        with tempfile.TemporaryDirectory(prefix="qajev-failure-reason-") as folder:
            target = Path(folder)
            shutil.copytree(ROOT / "qajev", target / "qajev", ignore=shutil.ignore_patterns("__pycache__"))
            (target / "tests").mkdir()
            for filename in ("conftest.py", "test_failure_reasons.py"):
                shutil.copyfile(ROOT / "tests" / filename, target / "tests" / filename)
            path = target / source
            content = path.read_text()
            if content.count(before) != 1:
                raise RuntimeError(f"{name}: mutation anchor is not unique")
            path.write_text(content.replace(before, after, 1))
            run = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=short", TEST + test], cwd=target,
                                 capture_output=True, text=True, timeout=60)
            output = run.stdout + run.stderr
            caught = run.returncode == 1 and f"FAILED {TEST}{test}" in output
            results.append({"fault": name, "caught": caught, "test": test})
            print(json.dumps(results[-1]), flush=True)
            if not caught:
                print(output)
                return 1
    print(json.dumps({"caught": sum(r["caught"] for r in results), "total": len(results)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
