"""Supervisor death and a closed progress pipe must not leave owned runner resources behind."""

import contextlib
import ctypes
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from qajev import chrome, runner, session, suite, watchdog


def _write(path, value):
    path = Path(path)
    staged = path.with_suffix('.next')
    staged.write_text(json.dumps(value))
    staged.replace(path)


def _wait_json(path, timeout=8):
    deadline = time.monotonic() + timeout
    while not Path(path).exists():
        assert time.monotonic() < deadline, f'owned process witness missing: {path}'
        time.sleep(.01)
    return json.loads(Path(path).read_text())


def _process_group(pgid):
    members = []
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == pgid:
                members.append(int(path.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
    return members


def _record(pid):
    return {'pid': pid, 'pgid': os.getpgid(pid), 'identity': chrome.identity(pid)}


def _reap_recorded(records):
    for record in records:
        with contextlib.suppress(ChildProcessError, ProcessLookupError):
            os.waitpid(record['pid'], os.WNOHANG)


def _kill_recorded_group(pgid, records):
    # Never signal a guessed/reused process group. Any surviving recorded member can prove this is still ours.
    for record in records:
        try:
            owned = os.getpgid(record['pid']) == pgid and chrome.still(record['pid'], record['identity'])
        except ProcessLookupError:
            continue
        if owned:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)
            return


def _native_worker_with_shell(root):
    root = Path(root)
    _write(root / 'worker-started.json', _record(os.getpid()))
    watchdog.notify('scenario', name='native-with-shell')
    shell = subprocess.Popen(['/bin/sh', '-c', 'sleep 30 & printf %s "$!" > "$1"; wait',
                              'watchdog-parent-fixture', str(root / 'sleep.pid')])
    deadline = time.monotonic() + 5
    while True:
        try:
            sleeper = int((root / 'sleep.pid').read_text())
            break
        except (FileNotFoundError, ValueError):
            assert time.monotonic() < deadline, 'owned shell did not publish its child PID'
            time.sleep(.01)
    records = [_record(pid) for pid in (os.getpid(), shell.pid, sleeper)]
    assert all(record['pgid'] == os.getpid() for record in records)
    _write(root / 'owned.json', records)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    ctypes.PyDLL(None).sleep(30)  # Deliberately holds the worker's GIL; a Python watcher thread cannot run.


def _supervisor(root):
    watchdog.supervise(_native_worker_with_shell, (root,), timeout=60)


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux subreaper and /proc prove exact owned-process reaping')
def test_sigkill_supervisor_reaps_native_worker_and_owned_shell_group(tmp_path):
    # Adopt only this test's orphaned descendants so "gone" includes reaped zombies. Constants: linux/prctl.h.
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0  # PR_GET_CHILD_SUBREAPER
    assert libc.prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    owner = multiprocessing.get_context('spawn').Process(target=_supervisor, args=(tmp_path,))
    records, pgid, remaining = [], None, None
    owner.start()
    try:
        records = _wait_json(tmp_path / 'owned.json')
        pgid = records[0]['pid']
        known = {record['pid'] for record in records}
        for pid in _process_group(pgid):
            if pid not in known:
                records.append(_record(pid))  # Includes the product guardian when present.
        _write(tmp_path / 'recorded-group.json', records)
        deadline = time.monotonic() + 3
        while True:
            wait_channel = Path(f'/proc/{pgid}/wchan').read_text().strip()
            if 'nanosleep' in wait_channel:
                break
            assert time.monotonic() < deadline, 'worker did not enter its real GIL-holding native sleep'
            time.sleep(.01)
        _write(tmp_path / 'native-entered.json', {'pid': pgid, 'wait_channel': wait_channel})
        owner.kill()  # Actual SIGKILL: no supervisor finally/atexit code can perform the cleanup.
        owner.join(3)
        assert owner.exitcode == -signal.SIGKILL
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            _reap_recorded(records)
            remaining = _process_group(pgid)
            if not remaining and all(not Path(f"/proc/{r['pid']}").exists() for r in records):
                break
            time.sleep(.02)
        remaining = _process_group(pgid)
        observed = {'remaining_before_teardown': remaining, 'records': records}
        _write(tmp_path / 'parent-death-result.json', observed)
    finally:
        if not records and (tmp_path / 'worker-started.json').exists():
            records = [_wait_json(tmp_path / 'worker-started.json')]
            pgid = records[0]['pgid']
        if pgid is not None:
            _kill_recorded_group(pgid, records)
        if owner.is_alive():
            owner.kill()
        owner.join(3)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            _reap_recorded(records)
            if all(not Path(f"/proc/{r['pid']}").exists() for r in records):
                break
            time.sleep(.02)
        _write(tmp_path / 'owned-cleanup.json', {
            'owner_exit': owner.exitcode,
            'remaining_recorded_pids': [r['pid'] for r in records if Path(f"/proc/{r['pid']}").exists()],
        })
        assert libc.prctl(36, previous.value, 0, 0, 0) == 0
    assert remaining == [], f'SIGKILL supervisor left owned native/shell group members alive: {remaining}'
    assert all(not Path(f"/proc/{r['pid']}").exists() for r in records)


def test_broken_notification_pipe_does_not_skip_run_group_cleanup(monkeypatch, tmp_path):
    closed = []

    class FailingSink:
        failed = False

        def send(self, message):
            if message['kind'] == 'active' and message.get('identity') == 'session cleanup':
                self.failed = True
                raise BrokenPipeError('fixture supervisor closed its progress pipe')

    class Page:
        def __init__(self, ledger, **_kw):
            self.ledger, self.url = ledger, 'about:blank'

        def page_socket(self):
            return None

        def set_device(self, _device):
            pass

        def arm(self, *_args):
            pass

        def check_host(self, _url):
            pass

        def navigate(self, url):
            self.url = url

        def probe(self, _expect):
            return {'url': self.url, 'status': 200, 'text': [True], 'probe': {}}

        def close(self):
            closed.append('Session.close')

    sink = FailingSink()
    monkeypatch.setattr(watchdog, '_sink', sink)
    monkeypatch.setattr(watchdog, '_critical_state', None)
    monkeypatch.setattr(session, '_jev', None)
    monkeypatch.setattr(session, 'configure_env', lambda _url: 'qajev-closed-pipe-fixture')
    monkeypatch.setattr(session, 'Session', Page)
    monkeypatch.setattr(session, 'stop_daemon', lambda: closed.append('stop_daemon'))
    parsed = suite.parse({'device': 'desktop', 'base_url': 'http://127.0.0.1', 'scenarios': [
        {'name': 'ready', 'expect': {'text': 'ready'}, 'settle': 0}]})
    metadata = {'headless': True, 'hosts': parsed.hosts, 'guard': {}, 'cwd': str(tmp_path)}
    result, pipe_error = None, None
    try:
        result = runner.run_group(parsed.scenarios, opts=runner.Options(shots=False, ux='off', load_high=None),
                                  cdp_url='fixture', suite_meta=metadata, run_dir=tmp_path, ledger=runner.Ledger(0))
    except (OSError, EOFError) as error:
        pipe_error = f'{type(error).__name__}: {error}'
    assert closed == ['Session.close', 'stop_daemon'], f'broken progress pipe skipped owned cleanup: {pipe_error}'
    assert sink.failed and watchdog._sink is None
    assert pipe_error is None
    assert result['results'][0]['outcome'] == 'pass'
