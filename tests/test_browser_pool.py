"""Admission uses live flock leases, not a count of possibly reused PIDs."""

import concurrent.futures
import fcntl
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from qajev import browser_pool, jobs

SAMPLE_MEMORY = browser_pool.available_memory


@pytest.fixture(autouse=True)
def pool(monkeypatch, tmp_path):
    from qajev import chrome

    made = []
    mkdtemp = chrome.tempfile.mkdtemp

    def allocate(*args, **kwargs):
        path = mkdtemp(*args, **kwargs)
        if kwargs.get("prefix", "").startswith("qajev-tmp-"):
            made.append(path)
        return path

    monkeypatch.setattr(chrome.tempfile, "mkdtemp", allocate)
    monkeypatch.setattr(browser_pool, "ROOT", tmp_path / "pool")
    monkeypatch.setattr(browser_pool, "available_memory", lambda: 32 * 1024**3)
    monkeypatch.setattr(jobs, "LOCK", tmp_path / "legacy.lock")
    monkeypatch.setattr(jobs, "local_runs_off", lambda: False)
    monkeypatch.delenv("QAJEV_BROWSER_RUNS", raising=False)
    monkeypatch.delenv("QAJEV_BROWSER_RAM_MIB", raising=False)
    monkeypatch.delenv("QAJEV_BROWSER_RESERVE_MIB", raising=False)
    yield made
    for path in made:
        shutil.rmtree(path, ignore_errors=True)


def test_two_runs_overlap_and_the_third_queues_until_a_slot_is_released():
    inside = threading.Barrier(3)
    release = threading.Event()
    events = []

    def run():
        with jobs.machine_lock("parallel fixture", browser_runs=2, wait=3):
            inside.wait(timeout=3)
            assert release.wait(3)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run) for _ in range(2)]
        try:
            inside.wait(timeout=3)  # both leases really coexist, without sleeping to guess overlap
        except threading.BrokenBarrierError:
            raise AssertionError("two browser runs failed to overlap") from None
        assert len(browser_pool.holders()) == 2
        with pytest.raises(jobs.Busy, match="parallel fixture"):
            with jobs.machine_lock("third", browser_runs=2, wait=0.05, emit=events.append):
                raise AssertionError("a third run exceeded capacity")
        assert events[0]["queued"] is True
        release.set()
        for future in futures:
            future.result()
    assert not browser_pool.holders()
    with jobs.machine_lock("next", browser_runs=2, wait=0.1):
        assert len(browser_pool.holders()) == 1


@pytest.mark.parametrize("available,expected", [(10 * 1024**3, 4), (4 * 1024**3, 2),
                                                (2 * 1024**3, 0), (None, 1)])
def test_ram_capacity_is_conservative_and_unknown_is_serial(available, expected):
    assert browser_pool.ram_capacity(4, available, 1024, 2048) == expected
    assert browser_pool.ram_capacity(4, 6 * 1024**3, 2048, 2048) == 2


@pytest.mark.parametrize("maximum,current,expected", [("max", "0", 8 * 1024**3),
                                                     (str(4 * 1024**3), str(1024**3), 3 * 1024**3),
                                                     (str(1024), str(2048), 0), ("broken", "0", None)])
def test_linux_memory_obeys_container_limits_and_refuses_unknown_limits(
        monkeypatch, tmp_path, maximum, current, expected):
    meminfo, cgroup = tmp_path / "meminfo", tmp_path / "cgroup"
    meminfo.write_text("MemAvailable: 8388608 kB\n")
    cgroup.write_text("0::/fixture\n")
    group = tmp_path / "groups" / "fixture"
    group.mkdir(parents=True)
    (group / "memory.max").write_text(maximum)
    (group / "memory.current").write_text(current)
    monkeypatch.setattr(browser_pool.sys, "platform", "linux")
    monkeypatch.setattr(browser_pool, "MEMINFO", meminfo)
    monkeypatch.setattr(browser_pool, "CGROUP", cgroup)
    monkeypatch.setattr(browser_pool, "CGROUP_ROOT", group.parent)
    assert SAMPLE_MEMORY() == expected


def test_low_ram_queues_instead_of_starting_or_killing_active_runs(monkeypatch):
    with jobs.machine_lock("first", browser_runs=4, wait=0.1):
        monkeypatch.setattr(browser_pool, "available_memory", lambda: 3 * 1024**3)
        with pytest.raises(jobs.Busy, match="memory"):
            with jobs.machine_lock("second", browser_runs=4, wait=0.05):
                raise AssertionError("RAM admission was bypassed")
        assert [h["what"] for h in browser_pool.holders()] == ["first"]


@pytest.mark.parametrize("available", [0, 2 * 1024**3])
def test_insufficient_known_memory_blocks_even_the_first_browser(monkeypatch, available):
    monkeypatch.setattr(browser_pool, "available_memory", lambda: available)
    with pytest.raises(jobs.Busy, match="memory"):
        with jobs.machine_lock("first", browser_runs=4, wait=0.05):
            raise AssertionError("the first browser bypassed its memory gate")
    assert not browser_pool.holders()


def test_unknown_memory_allows_only_one_active_browser(monkeypatch):
    monkeypatch.setattr(browser_pool, "available_memory", lambda: None)
    with jobs.machine_lock("first", browser_runs=4):
        with pytest.raises(jobs.Busy, match="memory"):
            with jobs.machine_lock("second", browser_runs=4, wait=0.05):
                raise AssertionError("unknown memory admitted parallel browsers")


@pytest.mark.parametrize("key,value,free_gib", [("QAJEV_BROWSER_RAM_MIB", "2048", 6),
                                               ("QAJEV_BROWSER_RESERVE_MIB", "3072", 5)])
def test_ram_budget_and_reserve_configuration_change_real_admission(monkeypatch, key, value, free_gib):
    monkeypatch.setenv(key, value)
    monkeypatch.setattr(browser_pool, "available_memory", lambda: free_gib * 1024**3)
    with jobs.machine_lock("one", browser_runs=4):
        with jobs.machine_lock("two", browser_runs=4):
            with pytest.raises(jobs.Busy, match="memory"):
                with jobs.machine_lock("three", browser_runs=4, wait=0.05):
                    raise AssertionError("configured memory admission was ignored")


@pytest.mark.parametrize("key", ["QAJEV_BROWSER_RAM_MIB", "QAJEV_BROWSER_RESERVE_MIB"])
@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_invalid_memory_configuration_refuses_before_creating_a_ticket(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="browser"):
        with jobs.machine_lock("invalid", browser_runs=2):
            raise AssertionError("invalid RAM setting was accepted")
    assert not browser_pool.holders()


@pytest.mark.parametrize("output,expected", [
    ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
     "Pages free: 10.\nPages inactive: 20.\nPages speculative: 5.\n", 573440),
    ("not a memory sample", None),
])
def test_darwin_available_memory_uses_the_reported_page_size(monkeypatch, output, expected):
    monkeypatch.setattr(browser_pool.sys, "platform", "darwin")
    monkeypatch.setattr(browser_pool.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(stdout=output))
    assert SAMPLE_MEMORY() == expected


def test_native_and_serial_requests_remain_exclusive():
    for kwargs in ({}, {"browser_runs": 1}):
        with jobs.machine_lock("exclusive", **kwargs):
            with pytest.raises(jobs.Busy):
                with jobs.machine_lock("parallel", browser_runs=4, wait=0.05):
                    raise AssertionError("parallel run overlapped exclusive work")
    with jobs.machine_lock("parallel", browser_runs=4):
        with pytest.raises(jobs.Busy):
            with jobs.machine_lock("native", wait=0.05):
                raise AssertionError("native work overlapped a browser")


def test_a_larger_new_request_cannot_raise_the_active_runs_limit():
    with jobs.machine_lock("limit two", browser_runs=2):
        with jobs.machine_lock("asks four", browser_runs=4):
            with pytest.raises(jobs.Busy):
                with jobs.machine_lock("third", browser_runs=4, wait=0.05):
                    raise AssertionError("the new request raised the active capacity")


def test_exception_releases_lease_and_emits_separate_timings():
    events = []
    measured = None
    with pytest.raises(RuntimeError):
        with jobs.machine_lock("fixture", browser_runs=2, emit=events.append):
            measured = browser_pool.timing()
            assert measured is not None and measured["queue_seconds"] >= 0
            time.sleep(0.02)
            raise RuntimeError("fixture")
    assert browser_pool.timing() is None
    assert not browser_pool.holders()
    ended = events[-1]
    assert ended["event"] == "released"
    assert ended["timing"]["run_seconds"] >= 0.02
    assert measured is not None
    assert ended["timing"]["queue_seconds"] == measured["queue_seconds"]


@pytest.mark.parametrize("value", ["0", "-1", "65", "x", "1.5", True])
def test_bad_capacity_is_refused_before_admission(value):
    with pytest.raises(ValueError, match="browser"):
        browser_pool.limit(value)


def test_capacity_config_is_used_and_caller_wins(monkeypatch):
    assert browser_pool.limit() == 1
    monkeypatch.setenv("QAJEV_BROWSER_RUNS", "4")
    assert browser_pool.limit() == 4
    assert browser_pool.limit(2) == 2


def test_stale_lease_is_removed_but_a_live_lease_is_never_inferred_from_pid():
    browser_pool.ROOT.mkdir()
    stale = browser_pool.ROOT / "000000-stale.json"
    stale.write_text(json.dumps({"pid": 1, "state": "active", "limit": 1}))
    assert not browser_pool.holders()
    assert not stale.exists()


def test_waiting_exclusive_work_is_not_overtaken_by_a_parallel_newcomer():
    queued = threading.Event()
    entered = threading.Event()
    release = threading.Event()

    def exclusive():
        with jobs.machine_lock("exclusive", wait=3, emit=lambda e: queued.set() if e.get("queued") else None):
            entered.set()
            assert release.wait(3)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        with jobs.machine_lock("first", browser_runs=2):
            future = executor.submit(exclusive)
            assert queued.wait(2)
            with pytest.raises(jobs.Busy):
                with jobs.machine_lock("newcomer", browser_runs=2, wait=0.05):
                    raise AssertionError("new work overtook the exclusive FIFO ticket")
        assert entered.wait(2)
        release.set()
        future.result()


def test_legacy_exclusive_lock_still_blocks_parallel_admission():
    with jobs.LOCK.open("a+") as old:
        fcntl.flock(old, fcntl.LOCK_EX)
        with pytest.raises(jobs.Busy, match="legacy"):
            with jobs.machine_lock("parallel", browser_runs=4, wait=0.05):
                raise AssertionError("legacy exclusive holder was ignored")


def test_dead_process_releases_its_slot_without_trusting_its_pid():
    script = """
import sys, time
from pathlib import Path
from qajev import browser_pool
browser_pool.ROOT = Path(sys.argv[1])
browser_pool.available_memory = lambda: 32 * 1024**3
with browser_pool.acquire('child', browser_runs=2, legacy_lock=Path(sys.argv[2])):
    print('ready', flush=True)
    time.sleep(30)
"""
    child = subprocess.Popen([sys.executable, "-c", script, str(browser_pool.ROOT), str(jobs.LOCK)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        assert len(browser_pool.holders()) == 1
        child.terminate()
        child.wait(timeout=5)
        assert not browser_pool.holders()
        with jobs.machine_lock("replacement", browser_runs=2, wait=0.1):
            assert len(browser_pool.holders()) == 1
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_browser_child_acquires_its_own_slot_instead_of_inheriting_native_privilege():
    script = """
import json, sys
from pathlib import Path
from qajev import browser_pool, jobs
browser_pool.ROOT = Path(sys.argv[1])
jobs.LOCK = Path(sys.argv[2])
browser_pool.available_memory = lambda: 32 * 1024**3
with jobs.machine_lock('child', browser_runs=2, wait=1):
    print(json.dumps({'holders': len(browser_pool.holders()), 'parallel': browser_pool.parallel()}))
"""
    with jobs.machine_lock("parent browser", browser_runs=2):
        child = subprocess.run([sys.executable, "-c", script, str(browser_pool.ROOT), str(jobs.LOCK)],
                               capture_output=True, text=True, timeout=5)
        assert child.returncode == 0, child.stderr
        assert json.loads(child.stdout) == {"holders": 2, "parallel": True}


def test_a_child_can_inherit_an_older_native_parents_legacy_lock():
    script = """
import sys
from pathlib import Path
from qajev import browser_pool, jobs
browser_pool.ROOT = Path(sys.argv[1])
jobs.LOCK = Path(sys.argv[2])
with jobs.machine_lock('child', wait=0.2):
    print('inside')
"""
    with jobs.LOCK.open("w+") as old:
        fcntl.flock(old, fcntl.LOCK_EX)
        json.dump({"pid": os.getpid(), "what": "legacy native parent"}, old)
        old.flush()
        child = subprocess.run([sys.executable, "-c", script, str(browser_pool.ROOT), str(jobs.LOCK)],
                               capture_output=True, text=True, timeout=5)
        assert child.returncode == 0, child.stderr
        assert child.stdout.strip() == "inside"


def test_queue_wait_has_no_execution_time_and_interrupt_leaves_no_ticket(monkeypatch):
    events = []
    with jobs.machine_lock("holder"):
        def interrupt(_):
            raise KeyboardInterrupt

        monkeypatch.setattr(browser_pool.time, "sleep", interrupt)
        with pytest.raises(KeyboardInterrupt):
            with jobs.machine_lock("queued", browser_runs=2, emit=events.append):
                raise AssertionError("waiting run entered")
        assert [h["what"] for h in browser_pool.holders()] == ["holder"]
    assert events[-1]["timing"]["run_seconds"] == 0


def test_linked_pool_and_ticket_are_refused(monkeypatch, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    browser_pool.ROOT.symlink_to(outside, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        browser_pool.holders()
    browser_pool.ROOT.unlink()
    browser_pool.ROOT.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("unchanged")
    (browser_pool.ROOT / "000-linked.json").symlink_to(sentinel)
    with pytest.raises((ValueError, OSError)):
        browser_pool.holders()
    assert sentinel.read_text() == "unchanged"


def test_parallel_chrome_uses_kernel_ports_and_private_profile_temp_and_state(monkeypatch, tmp_path):
    from qajev import chrome

    monkeypatch.setattr(chrome, "STATE", tmp_path / "chrome")
    monkeypatch.setattr(chrome, "EPHEMERAL_ROOT", tmp_path / "ephemeral")
    monkeypatch.setattr(chrome, "binary", lambda: "/fixture/chrome")
    monkeypatch.setattr(chrome, "version", lambda *_args, **_kwargs: {"Browser": "fixture"})
    def forbidden_probe():
        raise AssertionError("port probing races with other runs")

    monkeypatch.setattr(chrome, "free_port", forbidden_probe)
    calls = []

    def spawn(args, **kwargs):
        calls.append((args, kwargs))
        assert "--remote-debugging-port=0" in args
        profile = Path(next(a.split("=", 1)[1] for a in args if a.startswith("--user-data-dir=")))
        (profile / "DevToolsActivePort").write_text(f"{43000 + len(calls)}\n/devtools/browser/fixture\n")
        return SimpleNamespace(pid=900000 + len(calls), poll=lambda: None)

    monkeypatch.setattr(chrome.subprocess, "Popen", spawn)
    first = chrome.start("same", ephemeral=True, headless=True, port=0)
    second = chrome.start("same", ephemeral=True, headless=True, port=0)
    for key in ("port", "profile_dir", "temp_dir", "state_key"):
        assert first[key] != second[key], key
    assert [r["port"] for r in (first, second)] == [43001, 43002]
    for record, (args, kwargs) in zip((first, second), calls, strict=True):
        assert kwargs["env"]["TMPDIR"] == record["temp_dir"]
        assert Path(record["temp_dir"]).parent == Path("/tmp")
        assert len(record["temp_dir"].encode()) + 60 < 108  # Linux sun_path includes Chrome's socket suffix
        assert Path(record["profile_dir"]).stat().st_mode & 0o777 == 0o700
        monkeypatch.setattr(chrome, "cmdline", lambda _pid, a=args: " ".join(a))
        assert chrome.owns(record)  # actual port is absent from Chrome's command: launch_port is zero
        wrong = {**record, "profile_dir": str(tmp_path / "unrelated")}
        assert not chrome.owns(wrong)
        monkeypatch.setattr(chrome, "cmdline", lambda _pid, a=args: " ".join(a).replace(
            "--remote-debugging-port=0", "--remote-debugging-port=01"))
        assert not chrome.owns(record), "port zero must not match a longer port argument"
    killed = []
    monkeypatch.setattr(chrome, "_kill", killed.append)
    monkeypatch.setattr(chrome, "cmdline", lambda _pid: " ".join(calls[0][0]))
    assert chrome.stop(first["state_key"])
    assert killed == [first["pid"]]
    assert not Path(first["profile_dir"]).exists()
    assert not Path(first["temp_dir"]).exists()
    assert Path(second["profile_dir"]).is_dir()
    assert Path(second["temp_dir"]).is_dir()


@pytest.mark.parametrize("flag", ["--remote-debugging-port=9222", "--user-data-dir=/fixture/shared",
                                  "--remote-debugging-pipe"])
def test_parallel_chrome_refuses_flags_that_break_resource_isolation(monkeypatch, tmp_path, flag):
    from qajev import chrome

    monkeypatch.setattr(chrome, "EPHEMERAL_ROOT", tmp_path / "ephemeral")
    monkeypatch.setenv("QAJEV_CHROME_FLAGS", flag)
    def forbidden_spawn(*_a, **_kw):
        raise AssertionError("must refuse before spawn")

    monkeypatch.setattr(chrome.subprocess, "Popen", forbidden_spawn)
    with pytest.raises(chrome.ChromeError, match="override"):
        chrome.start(ephemeral=True, port=0)
    assert list(chrome.EPHEMERAL_ROOT.iterdir()) == []


def test_allocated_port_start_interrupt_stops_only_its_own_process(monkeypatch, tmp_path, pool):
    from qajev import chrome

    monkeypatch.setattr(chrome, "STATE", tmp_path / "state")
    monkeypatch.setattr(chrome, "EPHEMERAL_ROOT", tmp_path / "ephemeral")
    monkeypatch.setattr(chrome, "binary", lambda: "/fixture/chrome")
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda *_a, **_kw: SimpleNamespace(pid=900001, poll=lambda: None))
    monkeypatch.setattr(chrome.time, "sleep", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
    killed = []
    monkeypatch.setattr(chrome, "_kill", killed.append)
    with pytest.raises(KeyboardInterrupt):
        chrome.start(ephemeral=True, port=0, wait=1)
    assert killed == [900001]
    assert list(chrome.EPHEMERAL_ROOT.iterdir()) == []
    assert not any(Path(path).exists() for path in pool)


@pytest.mark.parametrize("initial", ["", "0", "65536", "incomplete"])
def test_allocated_debug_port_waits_for_a_complete_valid_owned_file(monkeypatch, tmp_path, initial):
    from qajev import chrome

    monkeypatch.setattr(chrome, "STATE", tmp_path / "state")
    monkeypatch.setattr(chrome, "EPHEMERAL_ROOT", tmp_path / "ephemeral")
    monkeypatch.setattr(chrome, "binary", lambda: "/fixture/chrome")
    active = []

    def spawn(args, **_kwargs):
        profile = Path(next(a.split("=", 1)[1] for a in args if a.startswith("--user-data-dir=")))
        active.append(profile / "DevToolsActivePort")
        active[0].write_text(initial)
        return SimpleNamespace(pid=900001, poll=lambda: None)

    def ready(url, **_kwargs):
        assert url == "http://127.0.0.1:43003"
        return {"Browser": "fixture"}

    monkeypatch.setattr(chrome.subprocess, "Popen", spawn)
    monkeypatch.setattr(chrome.time, "sleep", lambda _: active[0].write_text("43003\n/fixture\n"))
    monkeypatch.setattr(chrome, "version", ready)
    record = chrome.start(ephemeral=True, port=0, wait=1)
    assert record["port"] == 43003


def test_failed_chrome_record_write_stops_the_spawned_process_before_deleting_its_profile(monkeypatch, tmp_path, pool):
    from qajev import chrome

    monkeypatch.setattr(chrome, "STATE", tmp_path / "state")
    monkeypatch.setattr(chrome, "EPHEMERAL_ROOT", tmp_path / "ephemeral")
    monkeypatch.setattr(chrome, "binary", lambda: "/fixture/chrome")
    monkeypatch.setattr(chrome, "version", lambda *_a, **_kw: {"Browser": "fixture"})
    profile = []

    def spawn(args, **_kwargs):
        profile.append(Path(next(a.split("=", 1)[1] for a in args if a.startswith("--user-data-dir="))))
        (profile[0] / "DevToolsActivePort").write_text("43003\n/fixture\n")
        return SimpleNamespace(pid=900001, poll=lambda: None)

    def failed_write(*_a, **_kw):
        raise OSError("fixture disk full")

    stopped = []

    def stop(pid):
        assert profile[0].is_dir(), "stop must precede removal of Chrome's profile"
        stopped.append(pid)

    monkeypatch.setattr(chrome.subprocess, "Popen", spawn)
    monkeypatch.setattr(chrome, "_write_state", failed_write)
    monkeypatch.setattr(chrome, "_kill", stop)
    with pytest.raises(OSError):
        chrome.start(ephemeral=True, port=0)
    assert stopped == [900001]
    assert not profile[0].exists()
    assert not any(Path(path).exists() for path in pool)


def test_parallel_chrome_never_publishes_a_partial_state_record_to_other_runs(monkeypatch, tmp_path):
    from qajev import chrome

    monkeypatch.setattr(chrome, "STATE", tmp_path / "state")
    monkeypatch.setattr(chrome, "EPHEMERAL_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(chrome, "binary", lambda: "/fixture/chrome")
    monkeypatch.setattr(chrome, "version", lambda *_a, **_kw: {"Browser": "fixture"})
    monkeypatch.setattr(chrome, "_kill", lambda _: None)

    def spawn(args, **_kwargs):
        profile = Path(next(a.split("=", 1)[1] for a in args if a.startswith("--user-data-dir=")))
        (profile / "DevToolsActivePort").write_text("43003\n/fixture\n")
        return SimpleNamespace(pid=900001, poll=lambda: None)

    original_dump, original_write = json.dump, Path.write_text
    observed = []

    def paused_write(output, text):
        split = len(text) // 2
        output.write(text[:split])
        output.flush()
        try:
            chrome.status()  # a second run's actual reader, interleaved before this write finishes
        except ValueError as error:
            raise AssertionError("another run observed a partial Chrome record") from error
        observed.append(True)
        output.write(text[split:])

    def dump(value, output, **kwargs):
        if Path(output.name).parent == chrome.STATE:
            paused_write(output, json.dumps(value, **kwargs))
        else:
            original_dump(value, output, **kwargs)

    def write(path, text, *args, **kwargs):
        if path.parent == chrome.STATE and path.suffix == ".json":
            with path.open("w") as output:
                paused_write(output, text)
            return len(text)
        return original_write(path, text, *args, **kwargs)

    monkeypatch.setattr(chrome.subprocess, "Popen", spawn)
    monkeypatch.setattr(chrome.json, "dump", dump)
    monkeypatch.setattr(Path, "write_text", write)
    record = chrome.start(ephemeral=True, port=0)
    assert observed
    assert json.loads(chrome._state_file(record["state_key"]).read_text())["port"] == 43003


def test_failed_atomic_record_write_keeps_old_state_and_removes_its_temporary_file(monkeypatch, tmp_path):
    from qajev import chrome

    monkeypatch.setattr(chrome, "STATE", tmp_path)
    original = tmp_path / "kept.json"
    original.write_text('{"port": 43001}')

    def fail(*_a, **_kw):
        raise OSError("fixture disk full")

    monkeypatch.setattr(chrome.json, "dump", fail)
    with pytest.raises(OSError):
        chrome._write_state("kept", {"port": 43002})
    assert original.read_text() == '{"port": 43001}'
    assert sorted(p.name for p in tmp_path.iterdir()) == ["kept.json"]


def test_temp_cleanup_refuses_paths_outside_its_owned_namespace(tmp_path):
    from qajev import chrome

    sentinel = tmp_path / "unrelated"
    sentinel.mkdir()
    (sentinel / "keep").write_text("keep")
    chrome._remove_temp({"temp_dir": str(sentinel), "owner_pid": os.getpid()})
    assert (sentinel / "keep").read_text() == "keep"


@pytest.mark.parametrize("command", ["check", "run", "smoke"])
def test_cli_capacity_config_and_external_or_real_device_exclusivity(command, monkeypatch):
    from qajev import cli

    args = SimpleNamespace(command=command, argv=[command], browser_runs=3, cdp_url=None,
                           real_devices=None, quiet=True, events=False)
    with cli._machine_lock(args):
        assert browser_pool.parallel()
        assert browser_pool.holders()[0]["limit"] == 3
    for special in ({"cdp_url": "http://127.0.0.1:9222"}, {"real_devices": ["android"]}):
        isolated = SimpleNamespace(**{**vars(args), **special})
        with cli._machine_lock(isolated):
            assert not browser_pool.parallel()
            assert browser_pool.holders()[0]["limit"] == 1
    off = SimpleNamespace(**{**vars(args), "real_devices": "off"})
    with cli._machine_lock(off):
        assert browser_pool.parallel()
    stored = SimpleNamespace(real_devices=["android"])
    with cli._machine_lock(args, stored):
        assert not browser_pool.parallel()
    monkeypatch.setenv("QAJEV_BROWSER_RUNS", "4")
    args.browser_runs = None
    with cli._machine_lock(args):
        assert browser_pool.holders()[0]["limit"] == 4


def test_mcp_all_browser_entrypoints_pass_capacity_to_cli(monkeypatch, tmp_path):
    import asyncio

    from qajev import mcp_server

    commands = []

    async def dispatch(args, *_a, **_kw):
        commands.append(args)
        return {"gate": "PASS"}

    monkeypatch.setattr(mcp_server, "_dispatch", dispatch)
    suite = tmp_path / "suite.yaml"
    suite.write_text("name: Fixture\nscenarios: []\n")
    ctx = cast(mcp_server.Context, None)  # inert: the dispatch fixture never touches the injected context
    for call in (mcp_server.qa_check("http://localhost", ctx, browser_runs=3, runner="local"),
                 mcp_server.qa_run_suite(ctx, suite_path=str(suite), browser_runs=3, runner="local"),
                 mcp_server.qa_smoke("http://localhost", ctx, browser_runs=3, runner="local"),
                 mcp_server.qa_project_run("fixture", ctx, browser_runs=3, runner="local")):
        asyncio.run(call)
    assert len(commands) == 4
    for argv in commands:
        assert "--browser-runs" in argv
        assert argv[argv.index("--browser-runs") + 1] == "3"


def test_job_status_separates_live_queue_startup_and_execution_clocks(monkeypatch, tmp_path):
    job_id = "20261024-120000-abcd"
    monkeypatch.setattr(jobs, "JOBS", tmp_path)
    folder = tmp_path / job_id
    folder.mkdir()
    (folder / "job.json").write_text(json.dumps({"id": job_id, "pid": os.getpid(), "title": "fixture",
                                                "started_at": time.time() - 10, "argv": []}))
    queued = {"event": "waiting", "queued": True,
              "timing": {"queued_at": time.time() - 8, "admitted_at": None,
                         "queue_seconds": 0, "run_seconds": 0}}
    events = folder / "events.jsonl"
    events.write_text(json.dumps(queued) + "\n")
    waiting = jobs.status(job_id)
    assert waiting["state"] == "queued"
    assert "timing" in waiting
    assert waiting["timing"]["queue_seconds"] >= 8
    assert waiting["timing"]["run_seconds"] == 0
    with events.open("a") as out:
        out.write(json.dumps({"event": "released", "timing": queued["timing"]}) + "\n")
    assert jobs.status(job_id)["state"] == "queued"  # cancellation is finalizing, but execution never started
    admitted = {"event": "admitted", "timing": {**queued["timing"], "queue_seconds": 5,
                                                "admitted_at": time.time() - 3}}
    events.write_text(json.dumps(queued) + "\n" + json.dumps(admitted) + "\n")
    starting = jobs.status(job_id)
    assert starting["state"] == "running"  # Chrome is starting: no run event yet
    assert starting["timing"]["queue_seconds"] == 5
    assert 3 <= starting["timing"]["run_seconds"] < 4
    (folder / "exit_code").write_text("130")
    ended = {"event": "released", "timing": {**admitted["timing"], "run_seconds": 4}}
    with events.open("a") as out:
        out.write(json.dumps(ended) + "\n")
    assert jobs.status(job_id)["timing"]["run_seconds"] == 4


def test_runner_isolates_chrome_and_report_folders_and_persists_timings(monkeypatch, tmp_path):
    from qajev import chrome, runner, suite

    launches, stops = [], []

    def start(profile, **kwargs):
        launches.append(kwargs)
        return {"cdp_url": "http://127.0.0.1:43001", "state_key": str(len(launches)),
                "ephemeral": kwargs["ephemeral"]}

    monkeypatch.setattr(chrome, "reap", lambda: [])
    monkeypatch.setattr(chrome, "start", start)
    monkeypatch.setattr(chrome, "run_record", lambda owned: {"managed": True, "headless": True})
    monkeypatch.setattr(chrome, "stop", stops.append)
    def run_group(*_a, **_kw):
        time.sleep(0.01)
        return {"results": [], "ledger": _kw["ledger"].summary()}

    monkeypatch.setattr(runner, "run_group", run_group)
    monkeypatch.setattr(runner.time, "strftime", lambda *_a, **_kw: "20261024-120000")
    spec = suite.parse({"name": "same", "base_url": "http://localhost", "scenarios": [
        {"name": "fixture", "about": "Fixture", "expect": {"text": "fixture"}}]})
    opts = runner.Options(out_dir=tmp_path / "reports", headless=True, ephemeral=False)
    with jobs.machine_lock("first", browser_runs=2):
        first = runner.run(spec, opts)
    with jobs.machine_lock("second", browser_runs=2):
        second = runner.run(spec, opts)
    assert all(call["ephemeral"] is True and call.get("port") == 0 for call in launches)
    assert stops == ["1", "2"]
    assert first["run_dir"] != second["run_dir"]
    for built in (first, second):
        saved = json.loads((Path(built["run_dir"]) / "report.json").read_text())
        assert "timing" in saved
        assert saved["timing"] == built["timing"]
        assert saved["timing"]["queue_seconds"] >= 0
        assert saved["timing"]["run_seconds"] > 0


def test_smoke_uses_the_same_isolation_and_timing_contract(monkeypatch, tmp_path):
    from test_smoke_live import fake_browser

    from qajev import chrome, smoke

    opts = fake_browser(monkeypatch, tmp_path / "reports")
    opts.ephemeral = False
    seen = []

    def start(*_a, **kwargs):
        seen.append(kwargs)
        return {"cdp_url": "http://127.0.0.1:43001", "ephemeral": kwargs["ephemeral"], "state_key": "fixture"}

    monkeypatch.setattr(chrome, "start", start)
    stopped = []
    monkeypatch.setattr(chrome, "stop", stopped.append)
    with jobs.machine_lock("smoke", browser_runs=2):
        built = smoke.run("http://localhost", opts, max_pages=0, device="desktop")
    assert seen == [{"headless": False, "ephemeral": True, "port": 0}]
    assert stopped == ["fixture"]
    assert "timing" in built
    assert built["timing"]["run_seconds"] >= 0
    assert built["timing"]["admitted_at"] is not None
