"""Passing controls, assertion failures and source restoration for process admission."""

import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "process_fault_helpers", Path(__file__).with_name("check-evidence-faults.py"))
assert spec is not None and spec.loader is not None
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

P = "qajev/browser_pool.py"
T = "tests/test_process_admission.py::"
base.FAULTS = [
    ("A01-exclusive-browser-lock", P, "fcntl.LOCK_SH if requested > 1 else fcntl.LOCK_EX", "fcntl.LOCK_EX",
     T + "test_separate_cli_processes_overlap_and_a_third_waits"),
    ("A02-charge-resident-twice", P, "capacity = len(active) + ram_capacity", "capacity = ram_capacity",
     T + "test_second_process_uses_remaining_ram_after_first_browser_is_resident"),
    ("A03-bypass-remaining-ram", P, "capacity = min(capacity, memory)", "capacity = capacity",
     T + "test_low_remaining_ram_queues_second_process_then_admits_when_ram_returns"),
    ("A04-spend-startup-ram-twice", P, "remaining = min(available, max(0, envelope - reserved))",
     "remaining = available", T + "test_pending_browser_reservations_cannot_spend_the_same_free_ram_twice"),
    ("A05-native-inherits-parallel-env", P, "limit(browser_runs) if browser_runs is not None else 1",
     "limit(browser_runs)", T + "test_native_process_remains_exclusive_with_parallel_config"),
    ("A06-default-is-parallel", P, 'os.environ.get("QAJEV_BROWSER_RUNS", "1")',
     'os.environ.get("QAJEV_BROWSER_RUNS", "2")',
     T + "test_unconfigured_cli_is_serial_and_does_not_gain_a_ram_wait"),
    ("A07-cli-drops-count", "qajev/cli.py", 'browser_pool.limit(getattr(args, "browser_runs", None))', "1",
     T + "test_separate_cli_processes_overlap_and_a_third_waits"),
    ("A08-reprice-active-budgets", P, 'sum(r["ram_budget_mib"] for r in active) * MIB',
     "len(active) * budget_mib * MIB",
     T + "test_each_process_keeps_its_own_budget_and_the_largest_active_reserve"),
    ("A09-drop-active-reserve", P, 'reserve_mib = max([reserve_mib, *(r["ram_reserve_mib"] for r in active)])',
     "reserve_mib = reserve_mib",
     T + "test_each_process_keeps_its_own_budget_and_the_largest_active_reserve"),
    ("A10-unknown-allows-parallel", P, "return 1, None", "return requested, None",
     T + "test_unknown_first_reservation_stays_serial_until_that_process_finishes"),
]

if __name__ == "__main__":
    sys.argv += ["--endpoint", "http://127.0.0.1:9"]
    raise SystemExit(base.main())
