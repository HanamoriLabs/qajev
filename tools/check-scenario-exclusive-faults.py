"""Planted per-run admission faults; real CPU processes and restored fixture source copies only."""

import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'scenario_exclusive_fault_helpers', Path(__file__).with_name('check-evidence-faults.py'))
assert spec is not None and spec.loader is not None
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

G = 'qajev/scenario_gate.py'
R = 'qajev/runner.py'
W = 'qajev/watchdog.py'
L = 'qajev/live.py'
S = 'qajev/suite.py'
T = 'tests/test_scenario_exclusive.py::'
base.FAULTS = [
    ('E01-truthy-marker', S, 'if type(exclusive) is not bool:', 'if False:',
     T + 'test_exclusive_is_opt_in_and_accepts_only_boolean_values'),
    ('E02-writer-is-reader', G, '(fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH)', 'fcntl.LOCK_SH',
     T + 'test_real_process_ordinary_overlap_exclusive_drain_hold_and_resume'),
    ('E03-forget-writer-intent', G, '        except BlockingIOError:\n            return False',
     '        except BlockingIOError:\n'
     '            if self.has_turn:\n'
     '                fcntl.flock(self.turn, fcntl.LOCK_UN)\n'
     '                self.has_turn = False\n'
     '            return False',
     T + 'test_writer_intent_blocks_new_readers_until_existing_readers_drain'),
    ('E04-unmarked-peer-bypass', R,
     'if any(getattr(scenario, "exclusive", False) for scenario in scenarios):', 'if False:',
     T + 'test_spawned_runner_drains_setup_live_and_close_then_preserves_chain_tab'),
    ('E05-scenario-setup-bypass', R,
     'with watchdog.admission(admission_dir, exclusive=getattr(scenario, "exclusive", False)) as waited:',
     'with watchdog.admission(None, exclusive=getattr(scenario, "exclusive", False)) as waited:',
     T + 'test_spawned_runner_drains_setup_live_and_close_then_preserves_chain_tab'),
    ('E06-normal-cleanup-bypass', R, 'with watchdog.admission(admission_dir):', 'with watchdog.admission(None):',
     T + 'test_normal_group_cleanup_waits_behind_an_admitted_exclusive_scope'),
    ('E07-live-teardown-still-running', L, 'self.join(timeout=None if self.drain else self.interval + 12)',
     'self.join(timeout=self.interval + 12)',
     T + 'test_marked_live_teardown_drains_blocked_capture_before_admission_releases'),
    ('E08-release-before-failed-child-cleanup', W,
     '        cleanup_complete = False\n        try:\n            cleaned = cleanup',
     '        cleanup_complete = False\n'
     '        if lease is not None:\n'
     '            lease.close()\n'
     '            lease = None\n'
     '        try:\n            cleaned = cleanup',
     T + 'test_supervisor_retains_failed_child_lease_through_owned_cleanup'),
    ('E09-bounded-cleanup-bypass', W, 'with scenario_gate.admit(admission_dir):', 'with scenario_gate.admit(None):',
     T + 'test_bounded_cleanup_acquires_inside_child_before_any_cdp'),
    ('E10-uncertain-cleanup-admits-next', W, 'scenario_gate.poison(admission_dir)', 'pass',
     T + 'test_uncertain_failed_worker_cleanup_refuses_later_admission'),
    ('E11-queue-consumes-deadline', W, 'began = time.monotonic()\n    notify("queued")',
     'began = time.monotonic()', T + 'test_admission_queue_time_does_not_expire_semantic_progress'),
    ('E12-unwind-releases-before-cleanup', W,
     'notify("admission_abort", request_id=request_id, stop=stop_reason,\n'
     '                   detail=f"{type(error).__name__}: {error}")',
     'notify("admission_release", request_id=request_id)',
     T + 'test_supervisor_retains_failed_child_lease_through_owned_cleanup'),
    ('E13-latched-transport-releases', R,
     'if admission_dir is not None and (error := session_mod.transport_error()) is not None:',
     'if False and (error := session_mod.transport_error()) is not None:',
     T + 'test_latched_transport_failure_aborts_before_releasing_scenario_admission'),
    ('E14-new-tab-per-chain-step', R, 'if session is None:', 'if True:',
     T + 'test_spawned_runner_drains_setup_live_and_close_then_preserves_chain_tab'),
    ('E15-ordinary-work-serialized', G, 'self.exclusive = exclusive', 'self.exclusive = True',
     T + 'test_real_process_ordinary_overlap_exclusive_drain_hold_and_resume'),
    ('E16-independent-runs-share-lock', G, 'self.directory = Path(run_dir) / ".scenario-admission"',
     'self.directory = Path(run_dir).parent / ".scenario-admission"',
     T + 'test_separate_runs_do_not_share_exclusive_admission'),
    ('E17-failed-owner-reacquires-own-lease', W, 'admission_dir=admission_dir if lease is None else None',
     'admission_dir=admission_dir',
     T + 'test_supervisor_retains_failed_child_lease_through_owned_cleanup'),
]

if __name__ == '__main__':
    sys.argv += ['--endpoint', 'http://127.0.0.1:9']
    raise SystemExit(base.main())
