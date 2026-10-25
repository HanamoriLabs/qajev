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
    ("W08-orphan-shell", W, "        stop(process, grouped, ready=critical)\n", "        stop(process, False)\n",
     T + "test_process_group_cleanup_kills_descendants"),
    ("W09-corrupt-earlier-results", R, 'for result in failure.pop("results"):\n            keep(result)',
     'for result in failure.pop("results"):\n            keep({**result, "outcome": "skipped"})',
     T + "test_actual_runner_entry_retains_prior_results_and_never_replays"),
    ("W10-discard-measured-checks", R, 'partial = failure.pop("partial")',
     'partial = {}\n        failure.pop("partial")',
     T + "test_actual_runner_entry_retains_prior_results_and_never_replays"),
    ("W11-incomplete-ownership-is-clean", W, 'cleanup_complete=cleaned and not pending and not unread[1],',
     'cleanup_complete=cleaned,', T + "test_unacknowledged_resource_creation_reports_cleanup_uncertainty"),
    ("W12-close-foreign-context", W, "contexts & present_contexts", "present_contexts",
     T + "test_independent_cleanup_closes_only_recorded_targets_contexts"),
    ("W13-close-foreign-tab", W, "targets & present", "present",
     T + "test_independent_cleanup_closes_only_recorded_targets_contexts"),
    ("W14-replay-watchdog-expired-hook", R, "        pending = eligible\n",
     "        if eligible:\n            eligible[0].before = scenario.before + eligible[0].before\n"
     "        pending = eligible\n", T + "test_actual_runner_entry_retains_prior_results_and_never_replays"),
    ("W15-timeout-keeps-pass", R, '"harness", detail, time.monotonic() - failure["active_seconds"])',
     '"pass", detail, time.monotonic() - failure["active_seconds"])',
     T + "test_actual_runner_entry_retains_prior_results_and_never_replays"),
    ("W16-unknown-jev-history-is-progress", R, 'if _progress_action(h):', 'if True:',
     T + "test_jev_loop_progress_ignores_decisions_noops_and_failed_actions"),
    ("W17-disable-safe-continuation", R, 'failure["stop"] == "no_progress"', 'False',
     T + "test_actual_runner_entry_retains_prior_results_and_never_replays"),
    ("W18-reuse-discarded-daemon-tab-generation", R, 'name = session_mod.configure_env(cdp_url)',
     'name = session_mod.configure_env(cdp_url, name="qajev-watchdog-fixed")',
     T + "test_actual_runner_entry_retains_prior_results_and_never_replays"),
    ("W19-dependency-bypass", R, 'blocked_by = [d for d in scenario.depends_on if outcomes.get(d) != "pass"]',
     'blocked_by = []', T + "test_actual_runner_entry_retains_prior_results_and_never_replays"),
    ("W20-resume-after-unverified-cleanup", R, 'and failure["cleanup_complete"]', 'and True',
     T + "test_recovery_is_refused_after_unverified_cleanup_operator_or_other_stops"),
    ("W21-resume-after-operator-cancel", R, 'failure["stop"] == "no_progress"',
     'failure["stop"] in {"no_progress", "interrupted"}',
     T + "test_recovery_is_refused_after_unverified_cleanup_operator_or_other_stops"),
    ("W22-reset-cumulative-cap-cost", R,
     'ledger.usd = {"typesafe": summary["usd_typesafe_estimated"], "text": summary["usd_text"]}',
     'ledger.usd = {"typesafe": 0.0, "text": 0.0}',
     T + "test_recovery_keeps_cumulative_cap_cost_and_refuses_unknown_paid_cost"),
    ("W23-concurrent-model-clears-other-call", W, 'cost_pending.discard(message["call_id"])',
     'cost_pending.clear()', T + "test_concurrent_model_completion_does_not_hide_another_outstanding_call"),
    ("W24-concurrent-creation-clears-other-request", W, 'pending.discard(message["request_id"])',
     'pending.clear()', T + "test_unacknowledged_resource_creation_reports_cleanup_uncertainty"),
    ("W25-reset-whole-run-load-wait", R,
     'if key in {"load_waited", "account", "signed_here", "sign_in_attempted"}:',
     'if key in {"account", "signed_here", "sign_in_attempted"}:',
     T + "test_whole_run_load_wait_budget_is_not_restarted_after_watchdog"),
    ("W26-drop-parent-sign-in-metadata", R,
     'if key in {"load_waited", "account", "signed_here", "sign_in_attempted"}:',
     'if key in {"load_waited", "account", "sign_in_attempted"}:',
     T + "test_same_tab_signin_runtime_reaches_parent_report_without_replaying_login"),
    ("W27-trust-close-ack-without-removal", W,
     'cleaned = not (targets & {t["id"] for t in chrome.targets(cdp_url)}) and cleaned',
     'cleaned = True', T + "test_cleanup_requires_observed_target_removal_not_just_close_acknowledgement"),
    ("W28-creation-retry-forgets-unacknowledged-request", S,
     'except TimeoutError:\n                if creating:\n'
     '                    # Preserve upstream retry behavior, but an unknown first creation still needs an owner.\n'
     '                    request_id = uuid.uuid4().hex',
     'except TimeoutError:\n                if creating:\n'
     '                    # Reuse the old request id, losing its unresolved ownership.\n'
     '                    request_id = request_id',
     T + "test_preserved_upstream_creation_retry_cannot_confirm_ownership_of_its_first_attempt"),
    ("W29-leak-serial-parent-signin-daemon", R,
     '# Every scenario group now starts its own daemon, including serial runs.\n'
     '                session_mod.stop_daemon()',
     '# Leave the parent daemon alive in a serial run.\n                if opts.jobs > 1:\n'
     '                    session_mod.stop_daemon()',
     T + "test_serial_spawned_run_closes_the_parent_owned_signin_daemon"),
    ("W30-child-cancellation-loses-suffix", R,
     'if any(result.get("interrupted") for result in out["results"]):', 'if False:',
     T + "test_child_operator_interruption_preserves_prefix_and_marks_suffix_without_recovery"),
    ("W31-ignore-unread-critical-updates", W,
     'unread = [int(critical[index]) != seen[index] for index in range(3)]',
     'unread = [False, False, False]', T + "test_unread_critical_notifications_remain_uncertain_at_stop"),
    ("W32-missed-ready-leaves-owned-descendants", W,
     'stop(process, grouped, ready=critical)', 'stop(process, grouped)',
     T + "test_unread_ready_still_stops_only_the_owned_process_group"),
    ("W33-resume-with-unknown-runtime-budget", R,
     'and not failure.get("runtime_state_incomplete", False)', 'and True',
     T + "test_unknown_runtime_bookkeeping_refuses_a_replacement_worker"),
    ("W34-invalid-daemon-capture-helper", S, 'daemon_start=chrome.started(pid) if pid else None',
     'daemon_start=chrome.start_token(pid) if pid else None',
     T + "test_daemon_owner_capture_and_cleanup_use_real_process_start"),
    ("W35-invalid-daemon-recheck-helper", W, 'chrome.started(pid) not in {None, token}',
     'chrome.start_token(pid) not in {None, token}',
     T + "test_daemon_owner_capture_and_cleanup_use_real_process_start"),
    ("W36-no-supervisor-death-guard", W,
     'with _parent_guard():\n        notify("ready", pid=os.getpid())',
     'with contextlib.nullcontext():\n        notify("ready", pid=os.getpid())',
     "tests/test_watchdog_parent.py::test_sigkill_supervisor_reaps_native_worker_and_owned_shell_group"),
    ("W37-broken-notification-skips-cleanup", W,
     'except (OSError, EOFError):\n'
     '                # Reporting cannot skip owned cleanup after the supervisor\'s pipe has gone away.\n'
     '                _sink = None',
     'except (OSError, EOFError):\n                raise',
     "tests/test_watchdog_parent.py::test_broken_notification_pipe_does_not_skip_run_group_cleanup"),
]

if __name__ == "__main__":
    sys.argv += ["--endpoint", "http://127.0.0.1:9"]
    raise SystemExit(base.main())
