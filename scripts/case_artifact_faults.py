"""Exactly three focused, offline mutants, run serially through one light lane. No browser or paid call."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

CLI = Path(__file__).resolve().parents[1]


def hashes(cli, cloud):
    paths = [*(cli / "qajev").glob("*.py"), *(cloud / "qcloud").glob("*.py")]
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    cloud = args.cloud_root.resolve(strict=True)
    before = hashes(CLI, cloud)
    oversize = ('        if isinstance(meta, dict) and meta.get("error") == "too_big":\n'
                '            raise CaptureError(f"JSON capture exceeds the {max_bytes}-byte file limit; '
                'no snapshot was written")')
    faults = [
        ("png-kind-is-jpeg", "cli", "qajev/session.py", 'params = {"format": format,',
         'params = {"format": "jpeg",', "test_png_kind_is_lossless_png_bytes_and_has_no_jpeg_quality"),
        ("oversize-json-is-cut", "cli", "qajev/artifacts.py", oversize,
         '        if isinstance(meta, dict) and meta.get("error") == "too_big":\n            return b"{}\\n"',
         "test_oversize_json_is_a_plain_error_not_a_cut_attachment"),
        ("artifact-upload-allowlist-omits-files", "cloud", "qcloud/jobmap.py", " + exports + list(REPORT_FILES)",
         " + list(REPORT_FILES)", "test_upload_allowlist_keeps_named_png_and_full_json_before_report_json"),
    ]
    environment = {name: value for name, value in os.environ.items()
                   if not any(word in name.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))}
    environment["QAJEV_REAL_DEVICES"] = ""
    rows = []
    for name, repo, filename, old, new, check in faults:
        with tempfile.TemporaryDirectory(prefix="qajev-case-fault-") as temp:
            target = Path(temp)
            if repo == "cli":
                shutil.copytree(CLI / "qajev", target / "qajev", ignore=shutil.ignore_patterns("__pycache__"))
                (target / "tests").mkdir()
                shutil.copy2(CLI / "tests/test_case_artifacts.py", target / "tests/test_case_artifacts.py")
                test = f"tests/test_case_artifacts.py::{check}"
            else:
                shutil.copytree(cloud / "qcloud", target / "qcloud", ignore=shutil.ignore_patterns("__pycache__"))
                test = f"qcloud/test_case_artifacts.py::CaseArtifacts::{check}"
            path = target / filename
            text = path.read_text()
            if text.count(old) != 1:
                raise RuntimeError("Mutation must match exactly once: " + name)
            path.write_text(text.replace(old, new))
            report = target / "result.xml"
            environment["PYTHONPATH"] = str(target)
            done = subprocess.run([sys.executable, "-m", "pytest", "-q", test, "--junitxml=" + str(report)],
                                  cwd=target, env=environment, capture_output=True, text=True, timeout=45)
            named = []
            if report.exists():
                for case in ET.parse(report).iter("testcase"):
                    failure = case.find("failure")
                    if case.get("name") == check and failure is not None:
                        message = failure.get("message", "")
                        if "AssertionError" in message or message.startswith("assert "):
                            named.append(check)
            caught = done.returncode != 0 and bool(named)
            rows.append({"fault": name, "named_check": check, "caught": caught, "exit_code": done.returncode})
            print(name + ": " + ("CAUGHT" if caught else "MISSED"), flush=True)
            if not caught:
                break
    unchanged = before == hashes(CLI, cloud)
    proof = {"faults": rows, "caught": sum(row["caught"] for row in rows), "planned": len(faults),
             "source_sha256": before, "source_unchanged": unchanged, "serial": True,
             "provider_calls": 0, "browser_runs": 0, "production_changes": 0, "github_actions": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(proof, indent=2) + "\n")
    return 0 if proof["caught"] == len(faults) and unchanged else 1


if __name__ == "__main__":
    raise SystemExit(main())
