"""Restored, assertion-based faults for registered observation, using no real credentials or browser."""

import argparse
import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

FAULTS = [
    ("O01", 'self.home = Path(home).absolute()', 'self.home = Path(home).absolute().parent / "ignored-home"',
     "test_selected_home_external_reports_and_every_scenario"),
    ("O02", 'for root in self.roots:\n            for path', 'for root in []:\n            for path',
     "test_backfill_reports_without_jobs_and_missing_images_are_explicit"),
    ("O03", 'self._set("configuration", config)',
     'self._set("configuration", config)\n        self.db.execute("DELETE FROM outbox")\n        self.db.commit()',
     "test_disconnect_restart_and_source_pruning_preserve_unsent_artifacts"),
    ("O05", 'if current.is_symlink():', 'if False:',
     "test_evidence_never_reads_a_symlink_or_unapproved_report_root"),
    ("O06", 'for scenario in data.get("scenarios", []):', 'for scenario in data.get("scenarios", [])[:500]:',
     "test_selected_home_external_reports_and_every_scenario"),
    ("O07", 'if confirm.get("synced") != matches or confirm.get("missing") != []:', 'if False:',
     "test_upload_ack_is_confirmed_by_another_manifest_read"),
    ("O08", 'if self._setting("machine") not in {None, identity}:', 'if False:',
     "test_state_cannot_be_reused_for_a_different_home_or_machine"),
    ("O09", 'stat.S_IMODE(info.st_mode) != 0o600', 'False',
     "test_machine_credential_must_be_private_owned_regular_and_not_a_symlink"),
    ("O10", 'request.ProxyHandler({}), _NoRedirect()', 'request.ProxyHandler({})',
     "test_http_redirect_never_forwards_machine_credential"),
    ("O11", 'body = sanitize_request(method, path, body)', 'body = body',
     "test_observation_transport_sanitizes_metadata_before_outbound_request"),
    ("O12", 'if gate in ENDED and not any(file["path"] == "report.json" for file in files):', 'if False:',
     "test_early_harness_without_output_directory_keeps_full_selected_job_result"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1] / "qajev/observer.py"
    original = source.read_bytes()
    results = []
    try:
        for slug, old, new, test in FAULTS:
            assert original.decode().count(old) == 1, slug
            source.write_text(original.decode().replace(old, new))
            junit = args.out / (slug + ".xml")
            try:
                with (args.out / (slug + ".log")).open("w") as log:
                    proc = subprocess.run([sys.executable, "-m", "pytest", "tests/test_observer.py", "-q", "-k", test,
                                           f"--junitxml={junit}"], stdout=log, stderr=subprocess.STDOUT, timeout=60)
                failures = [case.find("failure") for case in ET.parse(junit).iter("testcase")
                            if case.find("failure") is not None and case.get("name", "").startswith(test)]
                caught = proc.returncode == 1 and any("AssertionError" in (failure.text or "")
                         or "Failed: DID NOT RAISE" in (failure.text or "") for failure in failures)
                results.append({"fault": slug, "test": test, "exit": proc.returncode, "caught": caught})
                assert caught, f"{slug} did not cause a named assertion failure"
            finally:
                source.write_bytes(original)
                assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(original).digest()
    finally:
        source.write_bytes(original)
        (args.out / "faults.json").write_text(json.dumps({"results": results, "restored": True}, indent=2) + "\n")


if __name__ == "__main__":
    main()
