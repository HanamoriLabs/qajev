"""Retain controls and named assertion failures for the deterministic UX report fault matrix.

Run with the project's test interpreter at nice 19. The source checkout is read-only: mutations happen in a
temporary copy. --output is a new proof directory and contains the exact mutations, logs, JUnit and RESULT.json.
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
import time
import xml.etree.ElementTree as ET
from pathlib import Path

OWNED = ("qajev/report.py", "qajev/report_html.py", "qajev/report_files.py", "tests/test_ux_report.py")
ADVISORY = "test_advisory_report_keeps_pass_and_full_json_evidence_with_crop_below_verdict"
STRICT = "test_strict_report_retains_fail_and_reports_ux_count_without_advisory_label"
NOTES = "test_skips_and_notes_preserve_legacy_note_dicts_and_plain_notes"
ESCAPING = "test_ux_fields_are_escaped_in_markdown_html_and_crop_attributes"
CROPS = "test_ux_crop_collection_keeps_all_safe_unique_paths_and_missing_evidence_fails_report"
INCOMPLETE = "test_incomplete_scan_never_looks_clean_and_lists_each_unchecked_field_escaped"

# Each tuple is (id, source file, exact original, planted replacement, named regression).
# UR14's match follows the incomplete-scan list addition; it still removes only the skipped-check text.
# UR04 and UR06 use existing direct-assert tests, so a missing DOM substring's ValueError cannot count as proof.
FAULTS = (
    ("UR01", "qajev/report.py", "**{report_html.ux_summary(r)}**", '**{r["outcome"]}**', ADVISORY),
    ("UR02", "qajev/report.py", "— {report_html.ux_summary(r)}", '— {r["outcome"]}', ADVISORY),
    ("UR03", "qajev/report_html.py",
     '+ (f"<br>{_e(ux_summary(r))}" if ux_details(r) is not None else "")', '+ ""', ADVISORY),
    ("UR04", "qajev/report_html.py", 'parts.append(f\'<p><strong>{_e(ux_summary(r))}</strong></p>\')',
     'parts.append("")', INCOMPLETE),
    ("UR05", "qajev/report.py", "lines += _scenario_ux_md(r)", "lines += []", ADVISORY),
    ("UR06", "qajev/report_html.py", "parts.append(_scenario_ux(r, ux))", 'parts.append("")', NOTES),
    ("UR07", "qajev/report_files.py", 'paths.extend(ux_crop(finding) for finding in _objects(ux.get("findings")))',
     "paths.extend([])", CROPS),
    ("UR08", "qajev/report_files.py", ' or value.get("mode") == "off"', "",
     "test_off_writes_no_ux_section_summary_or_evidence"),
    ("UR09", "qajev/report_html.py", 'suffix = " advisory" if ux.get("mode") != "strict" else ""',
     'suffix = " advisory"', STRICT),
    ("UR10", "qajev/report.py",
     'text = escape(" ".join(str(text if text is not None else "").split()), quote=False)',
     'text = " ".join(str(text if text is not None else "").split())', ESCAPING),
    ("UR11", "qajev/report_html.py", 'message = _e(finding.get("message"))',
     'message = finding.get("message")', ESCAPING),
    ("UR12", "qajev/report_files.py", 'return path if relative(path) and path.startswith("ux/") else None',
     "return path if isinstance(path, str) and path else None", CROPS),
    ("UR13", "qajev/report.py",
     'out.append(f"- Skipped {_ux_cell(skipped.get(\'check\'))}: {_ux_cell(skipped.get(\'reason\'))}")',
     'out.append("")', NOTES),
    ("UR14", "qajev/report_html.py",
     'f"<li>Skipped {_e(skipped.get(\'check\'))}: {_e(skipped.get(\'reason\'))}"', '""', NOTES),
    ("UR15", "qajev/report.py", "out += _ux_note_md(note)", "out += []", NOTES),
    ("UR16", "qajev/report_html.py", 'notes += [_ux_note(note) for note in ux.get("notes") or []]',
     "notes += []", NOTES),
    ("UR17", "qajev/report.py", 'message += f" Repeated {finding[\'repeats\']} times."',
     'message += ""', ADVISORY),
    ("UR18", "qajev/report_html.py", 'message += f" Repeated {_e(finding[\'repeats\'])} times."',
     'message += ""', ADVISORY),
    ("UR19", "qajev/report.py", 'crop = f"[![{check} crop]({href})]({href})" if href else "Not saved"',
     'crop = "Not saved"', ADVISORY),
    ("UR20", "qajev/report_html.py", "path = ux_crop(finding)", "path = None", ADVISORY),
    ("UI01", "qajev/report_html.py",
     '    if ux.get("status") == "incomplete":\n        return f"{outcome.upper()} · UX scan incomplete"\n',
     "", INCOMPLETE),
    ("UI02", "qajev/report.py", 'out += ["Scan incomplete.", ""]', "out += []", INCOMPLETE),
    ("UI03", "qajev/report_html.py", 'out.append("<p><strong>Scan incomplete.</strong></p>")',
     'out.append("")', INCOMPLETE),
    ("UI04", "qajev/report.py", 'elif ux.get("status") != "incomplete":', "else:", INCOMPLETE),
    ("UI05", "qajev/report_html.py", 'elif ux.get("status") != "incomplete":', "else:", INCOMPLETE),
    ("UI06", "qajev/report.py", 'for field in skipped.get("unchecked") or []]', "for field in []]", INCOMPLETE),
    ("UI07", "qajev/report_html.py", 'for field in skipped.get("unchecked") or [])',
     "for field in [])", INCOMPLETE),
    ("UI08", "qajev/report.py", "{_ux_cell(field.get('desc'))}", "{field.get('desc')}", INCOMPLETE),
    ("UI09", "qajev/report.py", "{_ux_cell(field.get('key'))}", "{field.get('key')}", INCOMPLETE),
    ("UI10", "qajev/report_html.py", "{_e(field.get('desc'))}", "{field.get('desc')}", INCOMPLETE),
    ("UI11", "qajev/report_html.py", "{_e(field.get('key'))}", "{field.get('key')}", INCOMPLETE),
)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hashes(root):
    return {name: sha256(root / name) for name in OWNED}


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def junit_summary(path):
    tree = ET.parse(path)
    cases = []
    for case in tree.iter("testcase"):
        failure = case.find("failure")
        message = failure.get("message", "") if failure is not None else ""
        text = failure.text or "" if failure is not None else ""
        cases.append({"name": case.get("name"), "failure": failure is not None,
                      "assertion": failure is not None and ("AssertionError" in text or message.startswith("assert ")),
                      "error": case.find("error") is not None, "skipped": case.find("skipped") is not None})
    return cases


def run_test(stage, proof, label, test=None):
    junit = proof / f"{label}.xml"
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short",
               "--junitxml=" + str(junit), "tests/test_ux_report.py" + ("::" + test if test else "")]
    child_env = dict(os.environ, QAJEV_HOME=str(stage / "test-home"), QAJEV_DEVICES="desktop",
                     QAJEV_OUTLIVE_PARENT="1", PYTHONPATH=str(stage), PYTHONDONTWRITEBYTECODE="1")
    started = time.time()
    result = subprocess.run(command, cwd=stage, env=child_env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=60)
    log = proof / f"{label}.log"
    log.write_text(result.stdout)
    cases = junit_summary(junit)
    failed = [case for case in cases if case["failure"]]
    named_assertions = [case["name"] for case in failed if case["assertion"]
                        and (case["name"] == test or case["name"].startswith(test + "["))] if test else []
    control_passed = result.returncode == 0 and bool(cases) and not any(
        case["failure"] or case["error"] or case["skipped"] for case in cases)
    caught = (result.returncode == 1 and bool(named_assertions) and len(named_assertions) == len(failed)
              and not any(case["error"] for case in cases))
    return {"label": label, "test": test, "command": command, "returncode": result.returncode,
            "seconds": round(time.time() - started, 3), "passed": control_passed,
            "caught": caught if test else None, "named_assertions": named_assertions, "cases": cases,
            "log": log.name, "junit": junit.name}


def prove(source, proof):
    before = hashes(source)
    record = {"status": "running", "started_at": time.time(), "source": str(source),
              "python": sys.version, "interpreter": sys.executable, "nice": os.getpriority(os.PRIO_PROCESS, 0),
              "driver_sha256": sha256(Path(__file__)), "source_sha256_before": before,
              "faults_expected": len(FAULTS), "faults": []}
    try:
        with tempfile.TemporaryDirectory(prefix="qajev-ux-report-faults-") as temporary:
            stage = Path(temporary)
            shutil.copytree(source / "qajev", stage / "qajev", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy2(source / "qajev_upload_privacy.py", stage / "qajev_upload_privacy.py")
            (stage / "tests").mkdir()
            for filename in ("conftest.py", "test_ux_report.py"):
                shutil.copy2(source / "tests" / filename, stage / "tests" / filename)
            (stage / "pytest.ini").write_text("[pytest]\n")
            if hashes(stage) != before:
                raise RuntimeError("copied report sources do not match the bound checkout")
            record["control"] = run_test(stage, proof, "control")
            if not record["control"]["passed"]:
                raise RuntimeError("the unmodified control failed")
            for ident, name, original, replacement, test in FAULTS:
                target = stage / name
                unchanged = target.read_text()
                if unchanged.count(original) != 1:
                    raise RuntimeError(f"{ident}: expected exactly one mutation site")
                mutated = unchanged.replace(original, replacement, 1)
                target.write_text(mutated)
                fault = {"id": ident, "source": name, "test": test, "original": original,
                         "replacement": replacement,
                         "diff": "".join(difflib.unified_diff(unchanged.splitlines(keepends=True),
                                                             mutated.splitlines(keepends=True),
                                                             fromfile=name, tofile=name + ".fault"))}
                try:
                    fault.update(run_test(stage, proof, ident, test))
                finally:
                    target.write_text(unchanged)
                    fault["restored"] = hashes(stage) == before
                    record["faults"].append(fault)
                print(f"{ident}: {'CAUGHT' if fault['caught'] else 'MISSED'} by {test}", flush=True)
                if not fault["caught"] or not fault["restored"]:
                    raise RuntimeError(f"{ident}: named assertion did not catch the fault or source was not restored")
            record["restored_control"] = run_test(stage, proof, "restored-control")
            if not record["restored_control"]["passed"]:
                raise RuntimeError("the restored control failed")
        record["status"] = "PASS"
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, ET.ParseError) as exc:
        record["status"], record["error"] = "FAIL", f"{type(exc).__name__}: {exc}"
    finally:
        record["source_sha256_after"] = hashes(source)
        record["owned_source_unchanged"] = record["source_sha256_after"] == before
        record["faults_caught"] = sum(bool(fault.get("caught")) for fault in record["faults"])
        record["finished_at"] = time.time()
        if not record["owned_source_unchanged"]:
            record["status"], record["error"] = "FAIL", "checkout report sources changed during proof"
        write_json(proof / "RESULT.json", record)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source, proof = args.source.resolve(), args.output.resolve()
    proof.mkdir(parents=True, exist_ok=True)
    if any(proof.iterdir()):
        parser.error("proof output must be empty; existing evidence is never overwritten")
    shutil.copy2(Path(__file__), proof / "check-ux-report-faults.py")
    write_json(proof / "MUTATIONS.json", [dict(zip(("id", "source", "original", "replacement", "test"), fault))
                                          for fault in FAULTS])
    result = prove(source, proof)
    print(json.dumps({"status": result["status"], "faults_caught": result["faults_caught"],
                      "faults_expected": result["faults_expected"], "owned_source_unchanged":
                      result["owned_source_unchanged"], "result": str(proof / "RESULT.json")}))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
