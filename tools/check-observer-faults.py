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
     'self._set("configuration", config)\n            self.db.execute("DELETE FROM outbox")\n'
     '            self.db.commit()',
     "test_disconnect_restart_and_source_pruning_preserve_unsent_artifacts"),
    ("O04", 'if previous and json.loads(previous[0])["gate"] in ENDED and rec["gate"] not in ENDED:', 'if False:',
     "test_live_queued_running_then_final_state_is_not_acknowledged_early"),
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
    ("O13", 'self._reserve(len(data))', 'pass',
     "test_outbox_cap_refuses_next_blob_before_any_bytes_are_written"),
    ("O14", '    def _write(self):\n        self._limit_database()', '    def _write(self):\n        pass',
     "test_database_metadata_cannot_grow_beyond_total_outbox_cap"),
    ("O15", 'raise ObserverError(f"Local run {local!r}: {failure}") from None', 'raise failure',
     "test_permanent_refusal_identifies_the_pending_local_run"),
    ("O16", '        except ValueError:\n            images = []', '        except TypeError:\n            images = []',
     "test_invalid_effect_metadata_becomes_missing_evidence_without_a_traceback"),
    ("O17", '    def _prune_confirmed(self) -> None:\n',
     '    def _prune_confirmed(self) -> None:\n        return\n',
     "test_confirmed_upload_prunes_blobs_without_restaging_unchanged_inventory"),
    ("O18", 'for sha in (confirmed | stored) - pending:', 'for sha in confirmed | stored:',
     "test_confirmed_pruning_preserves_blobs_shared_with_pending_runs"),
    ("O19", 'if previous and previous[1] == digest:', 'if False:',
     "test_confirmed_upload_prunes_blobs_without_restaging_unchanged_inventory"),
    ("O20", 'self._set("configuration", config)\n            self._prune_confirmed()',
     'self._set("configuration", config)',
     "test_restart_reclaims_confirmed_blobs_after_interrupted_cleanup"),
    ("O21", 'if hashlib.sha256(content).hexdigest() != file["sha256"]:\n'
     '                raise ObserverError("Run evidence changed during inventory; work stays pending.")',
     'if False:\n                raise ObserverError("Run evidence changed during inventory; work stays pending.")',
     "test_changing_evidence_during_inventory_is_not_checkpointed"),
    ("O22", 'if previous and previous[1] == digest:', 'if previous:',
     "test_changed_confirmed_report_snapshots_new_evidence_before_upload"),
    ("O23", 'for sha in (confirmed | stored) - pending:', 'for sha in confirmed - pending:',
     "test_superseded_pending_snapshot_releases_its_old_blob"),
    ("O24", 'with contextlib.suppress(Exception):', 'with contextlib.nullcontext():',
     "test_cleanup_failure_does_not_replace_the_local_run_error"),
    ("O25", 'if previous[2] or all((self.blob_dir / file["sha256"]).is_file() for file in files):', 'if True:',
     "test_missing_pending_blob_is_restaged_from_unchanged_source"),
    ("O26", 'raise ObserverError("Durable evidence is unreadable or missing; the run remains pending.") from None',
     'raise', "test_missing_pending_blob_without_source_has_safe_local_run_error"),
    ("O27", 'fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)', 'pass',
     "test_second_observer_cannot_prune_an_owners_uncheckpointed_blob"),
    ("O28", 'if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):', 'if False:',
     "test_pruning_refuses_invalid_hashes_and_symlinks"),
    ("O29", 'path = self.blob_dir / sha\n            if path.is_symlink():',
     'path = self.blob_dir / sha\n            if False:', "test_pruning_refuses_invalid_hashes_and_symlinks"),
    ("O30", 'self._blob_bytes = sum(path.stat().st_size for path in retained if path.is_file())', 'pass',
     "test_missing_pending_blob_is_restaged_from_unchanged_source"),
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
