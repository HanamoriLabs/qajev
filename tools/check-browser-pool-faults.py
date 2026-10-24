"""One restored source copy, a passing control before each fault, assertion failures only."""

import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "pool_fault_helpers", Path(__file__).with_name("check-evidence-faults.py"))
assert spec is not None and spec.loader is not None
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

U = "tests/test_browser_pool.py::"
P = "qajev/browser_pool.py"
C = "qajev/chrome.py"
R = "qajev/runner.py"
S = "qajev/smoke.py"

base.FAULTS = [
    ("P01-capacity", P, "len(active) < capacity", "len(active) <= capacity",
     U + "test_two_runs_overlap_and_the_third_queues_until_a_slot_is_released"),
    ("P02-active-limit", P, 'min([requested, *(r["limit"] for r in active)])', "requested",
     U + "test_a_larger_new_request_cannot_raise_the_active_runs_limit"),
    ("P03-ram-admission", P, "capacity = min(capacity, memory)", "capacity = capacity",
     U + "test_low_ram_queues_instead_of_starting_or_killing_active_runs"),
    ("P04-unknown-memory", P, "return min(requested, 1)", "return requested",
     U + "test_ram_capacity_is_conservative_and_unknown_is_serial"),
    ("P05-reserve", P, "available - reserve_mib * MIB", "available",
     U + "test_ram_capacity_is_conservative_and_unknown_is_serial"),
    ("P06-fifo", P, "first == path.name and ", "",
     U + "test_waiting_exclusive_work_is_not_overtaken_by_a_parallel_newcomer"),
    ("P07-dead-ticket", P, "                path.unlink()\n",
     "                records.append((path.name, json.load(handle)))\n",
     U + "test_stale_lease_is_removed_but_a_live_lease_is_never_inferred_from_pid"),
    ("P08-linked-root", P, "info = ROOT.lstat()", "info = ROOT.stat()",
     U + "test_linked_pool_and_ticket_are_refused"),
    ("P09-run-clock", P, 'round(now - admitted, 3) if admitted is not None else 0.0', "0.0",
     U + "test_exception_releases_lease_and_emits_separate_timings"),
    ("P10-queue-clock", P, '(admitted if admitted is not None else now) - lease["queued_mono"]',
     'now - lease["queued_mono"]', U + "test_exception_releases_lease_and_emits_separate_timings"),
    ("P11-config", P, 'os.environ.get("QAJEV_BROWSER_RUNS", "1")', '"1"',
     U + "test_capacity_config_is_used_and_caller_wins"),
    ("C01-probe-port", C, "port = free_port() if port is None else port", "port = port or free_port()",
     U + "test_parallel_chrome_uses_kernel_ports_and_private_profile_temp_and_state"),
    ("C02-state-key", C, "-{profile_dir.name.rsplit('-', 1)[-1]}", "",
     U + "test_parallel_chrome_uses_kernel_ports_and_private_profile_temp_and_state"),
    ("C03-temp-env", C, '"TMPDIR": str(temporary)', '"TMPDIR": "/tmp"',
     U + "test_parallel_chrome_uses_kernel_ports_and_private_profile_temp_and_state"),
    ("C04-port-identity", C, "record.get('launch_port', record['port'])", "record['port']",
     U + "test_parallel_chrome_uses_kernel_ports_and_private_profile_temp_and_state"),
    ("C05-flag-guard", C, "if isolated and any(", "if False and any(",
     U + "test_parallel_chrome_refuses_flags_that_break_resource_isolation"),
    ("C06-start-interrupt", C,
     'raise ChromeError(f"Chrome did not open its debugging port within {wait}s")\n    except BaseException:',
     'raise ChromeError(f"Chrome did not open its debugging port within {wait}s")\n    except Exception:',
     U + "test_allocated_port_start_interrupt_stops_only_its_own_process"),
    ("C07-record-failure", C, "        _kill(proc.pid)\n        _state_file(state_key).unlink(missing_ok=True)",
     "        _state_file(state_key).unlink(missing_ok=True)",
     U + "test_failed_chrome_record_write_stops_the_spawned_process_before_deleting_its_profile"),
    ("C08-port-range", C, "if not 1 <= port <= 65535:", "if False:",
     U + "test_allocated_debug_port_waits_for_a_complete_valid_owned_file"),
    ("P12-budget-config", P, 'os.environ.get("QAJEV_BROWSER_RAM_MIB", "1024")', '"1024"',
     U + "test_ram_budget_and_reserve_configuration_change_real_admission"),
    ("P13-reserve-config", P, 'os.environ.get("QAJEV_BROWSER_RESERVE_MIB", "2048")', '"2048"',
     U + "test_ram_budget_and_reserve_configuration_change_real_admission"),
    ("P14-container", P, 'min(available, max(0, int(maximum) - int((root / "memory.current").read_text())))',
     "available", U + "test_linux_memory_obeys_container_limits_and_refuses_unknown_limits"),
    ("P15-page-size", P, "page_size = int(match[1])", "page_size = 4096",
     U + "test_darwin_available_memory_uses_the_reported_page_size"),
    ("P16-first-memory", P, "capacity = min(capacity, memory)",
     "capacity = min(capacity, memory) if active else capacity",
     U + "test_insufficient_known_memory_blocks_even_the_first_browser"),
    ("P17-unknown-wire", P, "ram_capacity(requested, available_memory(), budget, reserve)",
     "ram_capacity(requested, available_memory() or 32 * 1024**3, budget, reserve)",
     U + "test_unknown_memory_allows_only_one_active_browser"),
    ("P18-overlap", P, "fcntl.LOCK_SH if requested > 1 else fcntl.LOCK_EX", "fcntl.LOCK_EX",
     U + "test_two_runs_overlap_and_the_third_queues_until_a_slot_is_released"),
    ("C11-port-prefix", C, '(?:\\s|$)', '',
     U + "test_parallel_chrome_uses_kernel_ports_and_private_profile_temp_and_state"),
    ("R01-profile", R, "ephemeral=opts.ephemeral or browser_pool.parallel()", "ephemeral=opts.ephemeral",
     U + "test_runner_isolates_chrome_and_report_folders_and_persists_timings"),
    ("R02-port", R, 'port=0 if browser_pool.parallel() else None', "port=None",
     U + "test_runner_isolates_chrome_and_report_folders_and_persists_timings"),
    ("R03-timing", R, '        built["timing"] = measured', "        pass",
     U + "test_runner_isolates_chrome_and_report_folders_and_persists_timings"),
    ("R04-output-isolation", R,
     '    run_dir = Path(tempfile.mkdtemp(prefix=f"{time.strftime(\'%Y%m%d-%H%M%S\')}'
     '-{slug(suite.name)}-", dir=opts.out_dir))',
     '    run_dir = opts.out_dir / "shared"\n    run_dir.mkdir(exist_ok=True)',
     U + "test_runner_isolates_chrome_and_report_folders_and_persists_timings"),
    ("C09-short-temp", C, 'dir=TEMP_ROOT))', 'dir=profile_dir))',
     U + "test_parallel_chrome_uses_kernel_ports_and_private_profile_temp_and_state"),
    ("C10-temp-cleanup", C, '        _remove_temp(record)\n    return stopped', '    return stopped',
     U + "test_parallel_chrome_uses_kernel_ports_and_private_profile_temp_and_state"),
    ("S01-profile", S, "ephemeral=opts.ephemeral or browser_pool.parallel()", "ephemeral=opts.ephemeral",
     U + "test_smoke_uses_the_same_isolation_and_timing_contract"),
    ("S02-port", S, 'port=0 if browser_pool.parallel() else None', "port=None",
     U + "test_smoke_uses_the_same_isolation_and_timing_contract"),
    ("S03-timing", S, '        built["timing"] = measured', "        pass",
     U + "test_smoke_uses_the_same_isolation_and_timing_contract"),
    ("W01-cli-config", "qajev/cli.py", 'browser_pool.limit(getattr(args, "browser_runs", None))', "1",
     U + "test_cli_capacity_config_and_external_or_real_device_exclusivity"),
    ("W02-mcp", "qajev/mcp_server.py", '["--browser-runs", str(limit(browser_runs))]', "[]",
     U + "test_mcp_all_browser_entrypoints_pass_capacity_to_cli"),
    ("W03-live-clock", "qajev/jobs.py", 'now - measured["admitted_at"]', 'now - measured["queued_at"]',
     U + "test_job_status_separates_live_queue_startup_and_execution_clocks"),
    ("W04-child-slot", "qajev/jobs.py", ' and held.get("inherit_children", True)', '',
     U + "test_browser_child_acquires_its_own_slot_instead_of_inheriting_native_privilege"),
    ("W05-queued-finalizing", "qajev/jobs.py", 'waiting = any(e.get("queued") for e in events)',
     'waiting = last.get("queued")',
     U + "test_job_status_separates_live_queue_startup_and_execution_clocks"),
    ("W06-legacy-child", "qajev/jobs.py", 'held.get("inherit_children", True)',
     'held.get("inherit_children", False)', U + "test_a_child_can_inherit_an_older_native_parents_legacy_lock"),
    ("W07-devices-off", "qajev/cli.py", 'devices.strip().lower() != "off"', 'True',
     U + "test_cli_capacity_config_and_external_or_real_device_exclusivity"),
]

if __name__ == "__main__":
    sys.argv += ["--endpoint", "http://127.0.0.1:9"]  # only Python fixture cases, no browser endpoint
    raise SystemExit(base.main())
