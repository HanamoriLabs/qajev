"""Separate CLI interpreters exercise real file-lock admission; only browser work is a fixture."""

import contextlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

WORKER = r'''
import json,os,sys,time
from pathlib import Path
from qajev import browser_pool,cli,jobs,runner
root,label,mode=sys.argv[1:]
root=Path(root)
assert browser_pool.ROOT==Path(f"/tmp/qajev-browser-pool-{os.getuid()}")
browser_pool.ROOT=root/'pool'
jobs.LOCK=root/'legacy.lock'
if mode!='live':
    def available():
        sample=(root/'memory').read_text()
        return None if sample=='unknown' else int(sample)
    browser_pool.available_memory=available
def work(*_args,**_kwargs):
    marker=root/(label+'.entering')
    marker.write_text(json.dumps({'pid':os.getpid(),'at':time.monotonic(),
        'holders':browser_pool.holders(),'timing':browser_pool.timing()}))
    marker.replace(root/(label+'.entered'))
    deadline=time.monotonic()+20
    while not (root/(label+'.release')).exists():
        assert time.monotonic()<deadline,'fixture release deadline'
        time.sleep(.01)
    (root/(label+'.ended')).write_text(str(time.monotonic()))
    return {'gate':'PASS','exit_code':0}
if mode=='native':
    args=cli.build_parser().parse_args(['play','fixture','--events'])
    args.argv=['play','fixture']
    with cli._machine_lock(args):work()
else:
    if mode!='live':runner.run=work
    argv=['run',str(root/'suite.json'),'--runner','local','--env-file',str(root/'empty.env'),
          '--json','--events','--no-shots','--out',str(root/(label+'-reports'))]
    if mode=='live':argv+=['--browser-runs','2','--headless','--ux','off']
    elif mode!='env':argv+=['--browser-runs',mode]
    raise SystemExit(cli.main(argv))
'''


@pytest.fixture
def processes(tmp_path):
    (tmp_path / 'memory').write_text(str(10 * 1024**3))
    (tmp_path / 'empty.env').touch(mode=0o600)
    (tmp_path / 'suite.json').write_text(json.dumps({
        'name': 'Admission fixture', 'base_url': 'http://localhost', 'devices': ['desktop'],
        'scenarios': [{'name': 'Fixture', 'about': 'Synthetic admission workload', 'expect': {'text': 'fixture'}}],
    }))
    children = []

    class Processes:
        root = tmp_path

        def memory(self, gib):
            # Replace atomically so the independently sampling child never reads half a value.
            staged = tmp_path / 'memory.next'
            staged.write_text('unknown' if gib is None else str(int(gib * 1024**3)))
            staged.replace(tmp_path / 'memory')

        def start(self, label, mode='2', *, configured=None, budget=None, reserve=None):
            env = {k: v for k, v in os.environ.items() if not k.startswith('QAJEV_')}
            env.update(QAJEV_HOME=str(tmp_path / (label + '-home')), QAJEV_LOCK_WAIT='8',
                       QAJEV_OUTLIVE_PARENT='1', PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            if configured is not None:
                env['QAJEV_BROWSER_RUNS'] = str(configured)
            if budget is not None:
                env['QAJEV_BROWSER_RAM_MIB'] = str(budget)
            if reserve is not None:
                env['QAJEV_BROWSER_RESERVE_MIB'] = str(reserve)
            if mode == 'live':
                binary = Path('/usr/lib/chromium/chromium')
                env['QAJEV_CHROME'] = str(binary) if binary.is_file() else str(shutil.which('chromium'))
                env['QAJEV_CHROME_FLAGS'] = shlex.join([
                    '--disable-gpu', '--proxy-server=http://127.0.0.1:9',
                    '--proxy-bypass-list=127.0.0.1;localhost;*.localhost',
                    '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost',
                ])
            stdout = (tmp_path / (label + '.stdout')).open('w')
            stderr = (tmp_path / (label + '.events')).open('w')
            child = subprocess.Popen([sys.executable, '-c', WORKER, str(tmp_path), label, mode],
                                     env=env, stdout=stdout, stderr=stderr, start_new_session=True)
            children.append((label, child, stdout, stderr))
            return child

        def wait(self, label, *, queued=False):
            child = next(p for name, p, *_ in children if name == label)
            deadline = time.monotonic() + 7
            while time.monotonic() < deadline:
                marker = tmp_path / (label + '.entered')
                if queued:
                    assert not marker.exists(), f'{label} entered instead of queueing'
                    if any(e.get('event') == 'waiting' for e in self.events(label)):
                        return
                elif marker.exists():
                    return json.loads(marker.read_text())
                assert child.poll() is None, (tmp_path / (label + '.events')).read_text()
                time.sleep(.01)
            raise AssertionError(f'{label} did not ' + ('queue' if queued else 'enter') + ' before the deadline')

        def events(self, label):
            rows = []
            for line in (tmp_path / (label + '.events')).read_text().splitlines():
                with contextlib.suppress(json.JSONDecodeError):
                    rows.append(json.loads(line))
            return rows

        def finish(self, label):
            child = next(p for name, p, *_ in children if name == label)
            (tmp_path / (label + '.release')).touch()
            assert child.wait(timeout=8) == 0, (tmp_path / (label + '.events')).read_text()

    yield Processes()
    for label, child, stdout, stderr in children:
        (tmp_path / (label + '.release')).touch()
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGINT)
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=2)
        stdout.close()
        stderr.close()


@pytest.mark.parametrize('mode,configured', [('2', None), ('env', 2)])
def test_separate_cli_processes_overlap_and_a_third_waits(processes, mode, configured):
    p = processes
    first = p.start('first', mode, configured=configured)
    a = p.wait('first')
    second = p.start('second', mode, configured=configured)
    b = p.wait('second')
    assert first.pid != second.pid == b['pid'] and a['pid'] == first.pid
    assert {r['pid'] for r in b['holders']} == {first.pid, second.pid}
    assert not (p.root / 'first.ended').exists(), 'the first process exited before overlap'
    p.start('third', mode, configured=configured)
    p.wait('third', queued=True)
    p.finish('first')
    c = p.wait('third')
    assert {r['pid'] for r in c['holders']} == {second.pid, c['pid']}
    p.finish('second')
    p.finish('third')


def test_second_process_uses_remaining_ram_after_first_browser_is_resident(processes):
    p = processes
    p.memory(4)
    first = p.start('first')
    p.wait('first')
    p.memory(3)  # 1 GiB resident, 2 GiB reserve, still 1 GiB for the next run.
    second = p.start('second')
    b = p.wait('second')
    assert {r['pid'] for r in b['holders']} == {first.pid, second.pid}
    assert not (p.root / 'first.ended').exists()
    p.finish('second')
    p.finish('first')


def test_low_remaining_ram_queues_second_process_then_admits_when_ram_returns(processes):
    p = processes
    first = p.start('first')
    p.wait('first')
    p.memory(2.5)  # Less than the configured reserve plus one new browser budget.
    second = p.start('second')
    p.wait('second', queued=True)
    assert first.poll() is None
    p.memory(3)
    b = p.wait('second')
    assert {r['pid'] for r in b['holders']} == {first.pid, second.pid}
    p.finish('second')
    p.finish('first')


def test_pending_browser_reservations_cannot_spend_the_same_free_ram_twice(processes):
    p = processes
    p.memory(4)  # Two 1 GiB budgets plus reserve; startup has not consumed either budget yet.
    first = p.start('first', '4')
    p.wait('first')
    second = p.start('second', '4')
    p.wait('second')
    p.start('third', '4')
    p.wait('third', queued=True)
    p.finish('first')
    c = p.wait('third')
    assert {r['pid'] for r in c['holders']} == {second.pid, c['pid']}
    assert first.returncode == 0
    p.finish('second')
    p.finish('third')


def test_each_process_keeps_its_own_budget_and_the_largest_active_reserve(processes):
    p = processes
    p.memory(4)
    p.start('first', '4', budget=2048, reserve=1024)
    p.wait('first')
    p.start('second', '4', budget=1024, reserve=1024)
    p.wait('second')
    p.start('third', '4', budget=1024, reserve=1024)
    p.wait('third', queued=True)
    p.finish('second')
    p.wait('third')
    p.finish('third')
    p.finish('first')

    p.start('large-reserve', '4', budget=1024, reserve=3072)
    p.wait('large-reserve')
    p.start('small-reserve', '4', budget=1024, reserve=1024)
    p.wait('small-reserve', queued=True)
    p.finish('large-reserve')
    p.wait('small-reserve')
    p.finish('small-reserve')


def test_unknown_first_reservation_stays_serial_until_that_process_finishes(processes):
    p = processes
    p.memory(None)
    p.start('first')
    p.wait('first')
    p.memory(10)
    p.start('second')
    p.wait('second', queued=True)
    p.finish('first')
    p.wait('second')
    p.finish('second')


def test_older_active_lease_without_ram_metadata_is_kept_exclusive():
    from qajev import browser_pool

    older = {'pid': 123, 'limit': 2, 'state': 'active'}
    assert browser_pool._memory_capacity(2, 10 * 1024**3, 1024, 2048, [older]) == (1, None)


@pytest.mark.parametrize('first_mode,second_mode', [('native', '2'), ('2', 'native')])
def test_native_process_remains_exclusive_with_parallel_config(processes, first_mode, second_mode):
    p = processes
    first = p.start('first', first_mode, configured=4)
    p.wait('first')
    second = p.start('second', second_mode, configured=4)
    p.wait('second', queued=True)
    p.finish('first')
    b = p.wait('second')
    assert [r['pid'] for r in b['holders']] == [second.pid]
    assert first.returncode == 0
    p.finish('second')


def test_unconfigured_cli_is_serial_and_does_not_gain_a_ram_wait(processes):
    p = processes
    p.memory(0)
    p.start('first', 'env')
    a = p.wait('first')
    assert a['holders'][0]['limit'] == 1
    p.start('second', 'env')
    p.wait('second', queued=True)
    p.finish('first')
    p.wait('second')
    p.finish('second')


@pytest.mark.live
@pytest.mark.skipif(os.environ.get('QAJEV_PROCESS_LIVE') != '1', reason='opt-in local Chromium fixture')
def test_two_real_cli_browsers_reach_a_shared_page_barrier_and_keep_private_resources(processes):
    p = processes
    both_pages = threading.Barrier(2)

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/fixture':
                self.send_response(204)
                self.end_headers()
                return
            both_pages.wait(timeout=45)  # neither run can finish before both browsers really load the page
            body = b'<!doctype html><title>Overlap fixture</title><h1>Both browsers arrived</h1>'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Page)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    spec = json.loads((p.root / 'suite.json').read_text())
    spec['base_url'] = f'http://127.0.0.1:{server.server_port}'
    spec['scenarios'][0].update(url='/fixture', expect={'text': 'Both browsers arrived'})
    (p.root / 'suite.json').write_text(json.dumps(spec))
    try:
        children = [p.start(label, 'live') for label in ('first', 'second')]
        for label, child in zip(('first', 'second'), children, strict=True):
            assert child.wait(timeout=100) == 0, (p.root / (label + '.events')).read_text()
        reports = [json.loads((p.root / (label + '.stdout')).read_text()) for label in ('first', 'second')]
        for result in reports:
            assert result['gate'] == 'PASS' and result['cost']['calls'] == {'text': 0, 'typesafe': 0}
            assert result['browser']['managed'] and result['browser']['ephemeral']
            for key in ('profile_dir', 'temp_dir'):
                assert not Path(result['browser'][key]).exists()
        a, b = reports
        for key in ('port', 'profile_dir', 'temp_dir', 'state_key'):
            assert a['browser'][key] != b['browser'][key]
        for left, right in ((a, b), (b, a)):
            assert left['timing']['admitted_at'] < right['timing']['admitted_at'] + right['timing']['run_seconds']
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
