"""Planted report-display faults, each after a passing control with exact source restoration."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def pytest(source, out, label):
    xml = out / (label + ".xml")
    env = {**os.environ, "PYTHONPATH": str(source) + os.pathsep + str(source / "tests"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    with (out / (label + ".log")).open("w") as log:
        proc = subprocess.run([sys.executable, "-m", "pytest", "tests/test_planted_report_display.py", "-q",
                               "--junitxml=" + str(xml)], cwd=source, env=env,
                              stdout=log, stderr=subprocess.STDOUT, timeout=45)
    cases = ET.parse(xml).getroot().findall(".//testcase")
    return {"exit": proc.returncode, "tests": len(cases),
            "failed": [case.get("name") for case in cases if case.find("failure") is not None],
            "errors": sum(case.find("error") is not None for case in cases),
            "skips": sum(case.find("skipped") is not None for case in cases)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    source, out = Path(args.source).resolve(), Path(args.out).resolve()
    out.mkdir(parents=True)
    path = source / "qajev/report_html.py"
    before = path.read_bytes()
    original = before.decode()
    faults = [
        ("P01-caught-is-raw-failure", '            return "caught"', '            return "fail"',
         "test_caught_plant_is_green_in_scenario_table_and_card"),
        ("P02-count-raw-failures", '    scenarios = data.get("scenarios") or []',
         '    return dict(data["counts"])\n    scenarios = data.get("scenarios") or []',
         "test_caught_plant_has_its_own_count_and_no_product_failure_explanation"),
        ("P03-product-failure-legend-for-caught", 'for o, t in legend.items() if c.get(o)',
         'for o, t in legend.items()', "test_caught_plant_has_its_own_count_and_no_product_failure_explanation"),
        ("P04-missed-is-raw-pass", '            return "missed"', '            return result.get("outcome")',
         "test_missed_plant_stays_red_and_says_the_check_missed_the_fault[changes0]"),
        ("P05-ignore-explicit-plant-marker", '    if result.get("expected") == "fail":',
         '    if result.get("graded"):', "test_real_failure_with_an_unrelated_grade_is_not_a_caught_plant"),
        ("P06-plan-keeps-red-control",
         'state = "pass" if kind == "caught" else "fail" if kind == "missed" else it["state"]',
         'state = it["state"]', "test_caught_plant_plan_and_its_named_check_are_green"),
    ]
    rows = []
    for name, anchor, replacement, expected_test in faults:
        assert path.read_bytes() == before
        assert original.count(anchor) == 1, name + ": exact-once anchor required"
        control = pytest(source, out, name + "-control")
        assert control == {"exit": 0, "tests": 11, "failed": [], "errors": 0, "skips": 0}, control
        try:
            path.write_text(original.replace(anchor, replacement, 1))
            result = pytest(source, out, name + "-fault")
        finally:
            path.write_bytes(before)
        caught = result["exit"] == 1 and expected_test in result["failed"] and not result["errors"]
        row = {"fault": name, "test": expected_test, "control": control, "fault_result": result,
               "caught": caught, "restored_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        rows.append(row)
        (out / "receipt.json").write_text(json.dumps({"faults": rows}, indent=2) + "\n")
        print(json.dumps(row), flush=True)
        assert caught and not result["skips"], name


if __name__ == "__main__":
    main()
