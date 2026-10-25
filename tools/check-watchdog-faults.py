"""Planted watchdog regressions, checked in restored CPU-only fixture copies."""

import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "watchdog_fault_helpers", Path(__file__).with_name("check-evidence-faults.py"))
assert spec is not None and spec.loader is not None
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

W = "qajev/watchdog.py"
R = "qajev/runner.py"
S = "qajev/session.py"
T = "tests/test_progress_watchdog.py::"
base.FAULTS = [
    ("W01-wall-clock", W,
     "def expired(self, now=None):\n        now = time.monotonic() if now is None else now",
     "def expired(self, now=None):\n        now = time.time() if now is None else now",
     T + "test_monotonic_deadline_does_not_count_polls_noise_or_identity_changes"),
    ("W02-poll-heartbeat", W,
     'elif kind == "active":\n            self.identity = message["identity"]',
     'elif kind == "active":\n            self.last = now\n            self.identity = message["identity"]',
     T + "test_monotonic_deadline_does_not_count_polls_noise_or_identity_changes"),
    ("W03-noise-heartbeat", W,
     'if message.get("partial") is not None:',
     'if kind == "event":\n            self.last = now\n        if message.get("partial") is not None:',
     T + "test_monotonic_deadline_does_not_count_polls_noise_or_identity_changes"),
    ("W04-queue-is-active", W,
     "return not self.queued and now - self.last >= self.timeout", "return now - self.last >= self.timeout",
     T + "test_declared_180_second_wait_valid_and_queue_time_separate"),
    ("W05-queue-is-progress", W, "self.last += waited", "self.last = now",
     T + "test_declared_180_second_wait_valid_and_queue_time_separate"),
    ("W06-ignore-semantic-completion", W,
     'elif kind == "complete":\n            self.last = now', 'elif kind == "complete":\n            pass',
     T + "test_declared_180_second_wait_valid_and_queue_time_separate"),
    ("W07-ipc-receipt-time", W, '        now = message.get("monotonic", now)\n', '',
     T + "test_completion_uses_real_event_time_instead_of_ipc_delivery_time"),
    ("W08-orphan-shell", W, "stop(process, grouped)", "stop(process, False)",
     T + "test_process_group_cleanup_kills_descendants"),
    ("W09-discard-earlier-results", R, 'results = failure.pop("results")',
     'results = []\n    failure.pop("results")',
     T + "test_actual_runner_entry_retains_prior_results_and_never_replays"),
    ("W10-discard-measured-checks", R, 'partial = failure.pop("partial")',
     'partial = {}\n    failure.pop("partial")',
     T + "test_actual_runner_entry_retains_prior_results_and_never_replays"),
    ("W11-incomplete-ownership-is-clean", W, 'cleanup_complete=cleaned and not pending,',
     'cleanup_complete=cleaned,', T + "test_unacknowledged_resource_creation_reports_cleanup_uncertainty"),
    ("W12-close-foreign-context", W, "contexts & present_contexts", "present_contexts",
     T + "test_independent_cleanup_closes_only_recorded_targets_contexts"),
    ("W13-close-foreign-tab", W, "targets & present", "present",
     T + "test_independent_cleanup_closes_only_recorded_targets_contexts"),
    ("W14-replay-unacknowledged-command", S,
     '            response = raw_cdp(method, session_id=session_id, _response_timeout=timeout, **params)\n',
     '            try:\n'
     '                response = raw_cdp(method, session_id=session_id, _response_timeout=timeout, **params)\n'
     '            except TimeoutError:\n'
     '                response = raw_cdp(method, session_id=session_id, _response_timeout=timeout, **params)\n',
     "tests/test_transport_loss.py::test_unacknowledged_action_timeout_is_never_replayed_or_counted_as_progress"),
    ("W15-timeout-keeps-pass", R, '"harness", detail, time.monotonic() - failure["active_seconds"])',
     '"pass", detail, time.monotonic() - failure["active_seconds"])',
     T + "test_actual_runner_entry_retains_prior_results_and_never_replays"),
    ("W16-unknown-jev-history-is-progress", R, 'if _progress_action(h):', 'if True:',
     T + "test_jev_loop_progress_ignores_decisions_noops_and_failed_actions"),
]

if __name__ == "__main__":
    sys.argv += ["--endpoint", "http://127.0.0.1:9"]
    raise SystemExit(base.main())
