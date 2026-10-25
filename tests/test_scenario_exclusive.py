"""Per-run admission and real spawned runner lifecycles; no browser or model calls."""

import contextlib
import json
import multiprocessing
import os
import signal
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from qajev import live, runner, scenario_gate, session, suite, watchdog

_ORIGINAL_INIT = runner._init_worker
_ORIGINAL_WORKER = runner._worker
_ORIGINAL_CHILD = runner._group_child


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_suffix('.next')
    staged.write_text(json.dumps(value))
    staged.replace(path)


def _wait(path, timeout=8):
    deadline = time.monotonic() + timeout
    while not Path(path).exists():
        assert time.monotonic() < deadline, f"fixture marker did not arrive: {path}"
        time.sleep(.01)
    return json.loads(Path(path).read_text())


def _wait_either(*paths):
    deadline = time.monotonic() + 8
    while not any(Path(path).exists() for path in paths):
        assert time.monotonic() < deadline, f'neither fixture phase arrived: {paths}'
        time.sleep(.01)


def _gate_actor(root, label, run_dir, exclusive):
    root = Path(root)
    _write(root / (label + '.requested'), {'pid': os.getpid()})
    try:
        with scenario_gate.admit(run_dir, exclusive=exclusive) as waited:
            _write(root / (label + '.entered'), {'pid': os.getpid(), 'waited': waited})
            _wait(root / (label + '.release'), timeout=15)
    except KeyboardInterrupt:
        _write(root / (label + '.interrupted'), True)
    finally:
        _write(root / (label + '.ended'), True)


@pytest.fixture
def gate_actors(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    children = {}

    class Actors:
        root = tmp_path

        def start(self, label, *, exclusive=False, run='run'):
            run_dir = tmp_path / run if run is not None else None
            if run_dir:
                run_dir.mkdir(exist_ok=True)
            child = ctx.Process(target=_gate_actor, args=(str(tmp_path), label, run_dir, exclusive))
            children[label] = child
            child.start()
            _wait(tmp_path / (label + '.requested'))
            return child

        def entered(self, label):
            return _wait(tmp_path / (label + '.entered'))

        def waiting(self, label):
            deadline = time.monotonic() + .25
            while time.monotonic() < deadline:
                assert not (tmp_path / (label + '.entered')).exists(), f'{label} crossed held admission'
                assert children[label].is_alive(), f'{label} exited while waiting'
                time.sleep(.01)

        def release(self, label):
            _write(tmp_path / (label + '.release'), True)
            children[label].join(5)
            assert children[label].exitcode == 0

    yield Actors()
    for label, child in children.items():
        _write(tmp_path / (label + '.release'), True)
        child.join(.2)
        if child.is_alive():
            child.terminate()
            child.join(2)
        if child.is_alive():
            child.kill()
            child.join(2)


def _parsed(**fields):
    return suite.parse({'base_url': 'http://127.0.0.1', 'device': 'desktop', 'scenarios': [
        {'name': 'fixture', 'expect': {'text': 'ready'}, **fields}]}).scenarios[0]


def test_exclusive_is_opt_in_and_accepts_only_boolean_values():
    assert _parsed().exclusive is False
    assert _parsed(exclusive=False).exclusive is False
    assert _parsed(exclusive=True).exclusive is True
    for bad in (None, 0, 1, 'true', 'false', [], {}):
        with pytest.raises(suite.SuiteError, match='exclusive'):
            _parsed(exclusive=bad)


def test_unmarked_admission_bypasses_without_creating_markers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with scenario_gate.admit(None) as waited:
        assert waited == 0
        assert list(tmp_path.iterdir()) == []
    assert list(tmp_path.iterdir()) == []


def test_real_process_ordinary_overlap_exclusive_drain_hold_and_resume(gate_actors):
    a = gate_actors
    left = a.start('left')
    a.entered('left')
    right = a.start('right')
    a.entered('right')
    assert left.pid != right.pid and left.is_alive() and right.is_alive()
    a.start('solo', exclusive=True)
    a.waiting('solo')
    a.release('left')
    a.waiting('solo')
    a.release('right')
    solo = a.entered('solo')
    assert solo['waited'] > 0
    a.start('later')
    a.waiting('later')
    a.release('solo')
    a.entered('later')
    a.release('later')


def test_separate_runs_do_not_share_exclusive_admission(gate_actors):
    a = gate_actors
    first = a.start('first', exclusive=True, run='one')
    a.entered('first')
    second = a.start('second', exclusive=True, run='two')
    a.entered('second')
    assert first.is_alive() and second.is_alive()
    a.release('first')
    a.release('second')


def test_writer_intent_blocks_new_readers_until_existing_readers_drain(gate_actors):
    a = gate_actors
    a.start('left')
    a.entered('left')
    a.start('right')
    a.entered('right')
    writer = scenario_gate.Admission(a.root / 'run', exclusive=True)
    later = scenario_gate.Admission(a.root / 'run')
    try:
        assert writer.poll() is False  # Claims writer intent; existing shared holders still own the room.
        assert later.poll() is False
        a.release('left')
        assert writer.poll() is False
        assert later.poll() is False
        a.release('right')
        assert writer.poll() is True
        assert later.poll() is False
        writer.close()
        assert later.poll() is True
    finally:
        writer.close()
        later.close()


def test_cancelled_pending_writer_releases_its_turnstile(gate_actors):
    a = gate_actors
    a.start('reader')
    a.entered('reader')
    writer = scenario_gate.Admission(a.root / 'run', exclusive=True)
    later = scenario_gate.Admission(a.root / 'run')
    try:
        assert writer.poll() is False
        assert later.poll() is False
        writer.close()
        assert later.poll() is True  # The existing reader may still overlap another ordinary reader.
    finally:
        writer.close()
        later.close()
        a.release('reader')


@pytest.mark.parametrize('stop', ['interrupt', 'kill'])
def test_real_process_cancellation_or_death_releases_owned_admission(gate_actors, stop):
    a = gate_actors
    first = a.start('first', exclusive=True)
    a.entered('first')
    if stop == 'interrupt':
        os.kill(first.pid, signal.SIGINT)
        _wait(a.root / 'first.interrupted')
    else:
        first.kill()
    first.join(3)
    assert not first.is_alive()
    a.start('later')
    a.entered('later')
    a.release('later')


def _phase(root, name, phase, **data):
    root = Path(root)
    _write(root / 'events' / f'{name}-{phase}.json', {'pid': os.getpid(), **data})
    barriers = json.loads((root / 'control.json').read_text()).get('barriers', {})
    if phase in barriers.get(name, []):
        _wait(root / 'releases' / f'{name}-{phase}.json', timeout=15)


def _fixture_group_child(*args):
    """Install browser dependencies inside the actual supervised spawn, leaving runner/supervisor real."""
    root = Path(args[4]).parent.parent
    state = {'name': args[0][0].name}
    notify = watchdog.notify
    _phase(root, state['name'], 'group', run_dir=str(args[4]))

    def observed(kind, **data):
        if kind == 'scenario':
            state['name'] = data['name']
            _phase(root, state['name'], 'selected')
        elif kind == 'active' and data.get('identity') == 'session cleanup':
            state['cleanup'] = True
            _phase(root, state['name'], 'cleanup_attempt')
        elif kind == 'queued':
            _phase(root, state['name'], 'cleanup_queued' if state.get('cleanup') else 'queued')
        elif kind == 'admission_release' and session.transport_error() is not None:
            _phase(root, state['name'], 'unsafe_release')
        elif kind == 'event' and data['value'].get('event') == 'scenario':
            result = data['value']['result']
            _phase(root, result['name'], 'result', outcome=result['outcome'])
        if kind == 'resource':
            data.pop('daemon', None)  # This browser fixture has no real daemon resource to stop.
        notify(kind, **data)

    class Page:
        def __init__(self, ledger, **_kw):
            self.ledger, self.url, self.tab = ledger, 'about:blank', uuid.uuid4().hex
            _phase(root, state['name'], 'setup', tab=self.tab)

        def page_socket(self):
            return self.tab

        def set_device(self, _device):
            pass

        def arm(self, *_args):
            pass

        def check_host(self, _url):
            pass

        def navigate(self, url):
            self.url = url
            _phase(root, state['name'], 'navigate', tab=self.tab, url=url)

        def probe(self, _expect):
            return {'url': self.url, 'status': 200, 'text': [True], 'probe': {}}

        def run_hook(self, hook, _url):
            _phase(root, state['name'], 'action', tab=self.tab, url=self.url)
            if hook.get('js') == 'latch_transport()':
                session._jev = SimpleNamespace(transport=SimpleNamespace(error=session.TransportLost('fixture loss')))

        def close(self):
            _phase(root, state['name'], 'close', tab=self.tab)

    @contextlib.contextmanager
    def frames(*_a, **kw):
        _phase(root, state['name'], 'live_enter', drain=kw.get('drain', False))
        try:
            yield
        finally:
            _phase(root, state['name'], 'live_exit')

    watchdog.notify = observed
    session.Session = Page
    session.stop_daemon = lambda: _phase(root, state['name'], 'daemon_close')
    runner.live.frames = frames
    return _ORIGINAL_CHILD(*args)


def _fixture_initializer(*args):
    _ORIGINAL_INIT(*args)
    runner._group_child = _fixture_group_child


def _fixture_pool_worker(payload):
    root = Path(payload[4]).parent.parent
    _phase(root, payload[0][0].name, 'worker', enabled=bool(payload[3].get('scenario_gate')))
    return _ORIGINAL_WORKER(payload)


@pytest.fixture
def spawned_runner(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, '_init_worker', _fixture_initializer)
    monkeypatch.setattr(runner, '_worker', _fixture_pool_worker)
    monkeypatch.setattr(runner, '_group_child', _fixture_group_child)
    monkeypatch.setattr(runner.providers, 'resolve', lambda: {})
    monkeypatch.setattr(runner.providers, 'describe', lambda _r: {'jev': 'none', 'text': 'none'})
    monkeypatch.setattr(runner.chrome, 'reap', lambda: [])
    monkeypatch.setattr(runner.chrome, 'version', lambda _u: {'fixture': True})
    threads, answers = [], []

    class Run:
        root = tmp_path

        def start(self, scenarios, *, barriers, jobs=3):
            _write(tmp_path / 'control.json', {'barriers': barriers})
            parsed = suite.parse({'base_url': 'http://127.0.0.1', 'device': 'desktop', 'scenarios': scenarios})
            opts = runner.Options(cdp_url='http://127.0.0.1:9', out_dir=tmp_path / 'reports',
                                  shots=False, ux='off', load_high=None, jobs=jobs)

            def execute():
                try:
                    answers.append(runner.run(parsed, opts))
                except BaseException as error:
                    answers.append(error)

            thread = threading.Thread(target=execute, daemon=True)
            threads.append(thread)
            thread.start()

        def phase(self, name, phase):
            return _wait(tmp_path / 'events' / f'{name}-{phase}.json')

        def absent(self, name, phase):
            assert not (tmp_path / 'events' / f'{name}-{phase}.json').exists(), (name, phase)

        def release(self, name, phase):
            _write(tmp_path / 'releases' / f'{name}-{phase}.json', True)

        def finish(self):
            threads[-1].join(12)
            assert not threads[-1].is_alive(), 'actual runner did not finish'
            assert answers and isinstance(answers[-1], dict), answers
            return answers[-1]

    yield Run()
    if (tmp_path / 'control.json').exists():
        for name, phases in json.loads((tmp_path / 'control.json').read_text())['barriers'].items():
            for phase in phases:
                _write(tmp_path / 'releases' / f'{name}-{phase}.json', True)
    for thread in threads:
        thread.join(18)
        assert not thread.is_alive(), 'fixture runner leaked after releases'


def _scenario(name, **fields):
    return {'name': name, 'url': '/' + name, 'before': [{'js': 'fixture()'}],
            'expect': {'text': 'ready'}, 'settle': 0, **fields}


def test_spawned_runner_drains_setup_live_and_close_then_preserves_chain_tab(spawned_runner):
    r = spawned_runner
    r.start([_scenario('left'), _scenario('right'), _scenario('lead'),
             _scenario('solo', exclusive=True, depends_on=['lead']),
             _scenario('tail', url=None, depends_on=['solo']), _scenario('late')], jobs=4, barriers={
                 'left': ['setup', 'action', 'live_exit', 'close'], 'right': ['action'],
                 'lead': ['action'], 'solo': ['action', 'live_exit'], 'late': ['worker', 'action']})
    r.phase('left', 'setup')
    r.phase('right', 'action')
    r.phase('lead', 'action')
    r.release('lead', 'action')
    r.phase('solo', 'queued')
    _await_writer_intent(r.phase('lead', 'group')['run_dir'])
    r.phase('late', 'worker')
    r.release('late', 'worker')
    r.phase('late', 'queued')
    r.absent('solo', 'navigate')
    r.release('left', 'setup')
    r.phase('left', 'action')
    r.release('left', 'action')
    r.release('right', 'action')
    r.phase('left', 'live_exit')
    r.absent('solo', 'navigate')
    r.absent('late', 'setup')
    r.release('left', 'live_exit')
    _wait_either(r.root / 'events/left-close.json', r.root / 'events/solo-action.json')
    if (r.root / 'events/left-close.json').exists():
        r.absent('solo', 'navigate')  # Cleanup may drain first, or wait safely behind the exclusive writer.
        r.release('left', 'close')
    r.phase('solo', 'action')
    r.absent('late', 'setup')
    r.release('solo', 'action')
    r.phase('solo', 'live_exit')
    r.absent('late', 'setup')
    r.release('solo', 'live_exit')
    r.release('left', 'close')
    r.phase('late', 'action')
    r.release('late', 'action')
    report = r.finish()
    assert [row['outcome'] for row in report['scenarios']] == ['pass'] * 6
    chain = [r.phase(name, 'action') for name in ('lead', 'solo', 'tail')]
    assert len({row['tab'] for row in chain}) == 1
    assert chain[2]['url'] == chain[1]['url'] == 'http://127.0.0.1/solo'
    for name in ('left', 'right', 'lead', 'late'):
        assert r.phase(name, 'worker')['enabled'] is True
    for name in ('left', 'right', 'lead', 'solo', 'tail', 'late'):
        assert r.phase(name, 'live_enter')['drain'] is True


def test_jobs3_resumes_three_ordinary_workers_after_exclusive_scope(spawned_runner):
    r = spawned_runner
    r.start([_scenario('solo', exclusive=True), _scenario('left'), _scenario('right'), _scenario('third')],
            jobs=3, barriers={'solo': ['action'], 'left': ['worker', 'action'],
                              'right': ['worker', 'action'], 'third': ['action']})
    r.phase('solo', 'action')
    for name in ('left', 'right'):
        r.phase(name, 'worker')
        r.release(name, 'worker')
        r.phase(name, 'queued')
        r.absent(name, 'setup')
    r.absent('third', 'worker')
    r.release('solo', 'action')
    actions = [r.phase(name, 'action') for name in ('left', 'right', 'third')]
    workers = [r.phase(name, 'worker') for name in ('left', 'right', 'third')]
    assert len({row['pid'] for row in workers}) == 3
    assert len({row['pid'] for row in actions}) == 3
    assert len({row['tab'] for row in actions}) == 3
    for name in ('left', 'right', 'third'):
        r.absent(name, 'result')
        r.release(name, 'action')
    assert [row['outcome'] for row in r.finish()['scenarios']] == ['pass'] * 4


def test_unmarked_runner_keeps_parallel_ordinary_work_and_disables_gate(spawned_runner):
    r = spawned_runner
    r.start([_scenario('left'), _scenario('right')], jobs=2,
            barriers={'left': ['action'], 'right': ['action']})
    left, right = r.phase('left', 'action'), r.phase('right', 'action')
    assert left['pid'] != right['pid']
    assert left['tab'] != right['tab']
    r.release('left', 'action')
    r.release('right', 'action')
    assert [row['outcome'] for row in r.finish()['scenarios']] == ['pass', 'pass']
    for name in ('left', 'right'):
        assert r.phase(name, 'worker')['enabled'] is False
        assert r.phase(name, 'live_enter')['drain'] is False


def test_normal_group_cleanup_waits_behind_an_admitted_exclusive_scope(spawned_runner):
    r = spawned_runner
    r.start([_scenario('mark', exclusive=True), _scenario('ordinary', depends_on=['mark'])], jobs=1,
            barriers={'ordinary': ['result']})
    r.phase('ordinary', 'result')
    run_dir = r.phase('mark', 'group')['run_dir']
    writer = scenario_gate.Admission(run_dir, exclusive=True)
    try:
        deadline = time.monotonic() + 5
        while not writer.poll():
            assert time.monotonic() < deadline, 'completed ordinary scenario never released its work admission'
            time.sleep(.01)
        r.release('ordinary', 'result')
        r.phase('ordinary', 'cleanup_attempt')
        _wait_either(r.root / 'events/ordinary-cleanup_queued.json', r.root / 'events/ordinary-close.json')
        r.absent('ordinary', 'close')
        r.absent('ordinary', 'daemon_close')
    finally:
        writer.close()
        r.release('ordinary', 'result')
    assert [row['outcome'] for row in r.finish()['scenarios']] == ['pass', 'pass']
    r.phase('ordinary', 'close')
    r.phase('ordinary', 'daemon_close')


def test_latched_transport_failure_aborts_before_releasing_scenario_admission(spawned_runner):
    r = spawned_runner
    r.start([_scenario('broken', exclusive=True, after=[{'js': 'latch_transport()'}]), _scenario('later')],
            jobs=1, barriers={})
    report = r.finish()
    assert report['scenarios'][0]['outcome'] == 'harness'
    assert report['scenarios'][0]['stop'] == 'transport_lost'
    assert report['scenarios'][1]['outcome'] == 'skipped'
    r.absent('broken', 'unsafe_release')
    r.absent('later', 'action')


def test_marked_live_teardown_drains_blocked_capture_before_admission_releases(tmp_path, monkeypatch):
    run_dir = tmp_path / 'run'
    live.touch(run_dir)
    captured, release, exiting, exited = (threading.Event() for _ in range(4))
    fronted = []

    def capture(_self, **_kw):
        captured.set()
        assert release.wait(5)
        return b'fixture frame'

    monkeypatch.setattr(live.Frames, 'capture', capture)
    monkeypatch.setattr(live.Frames, '_front', lambda _self: fronted.append(exited.is_set()))
    writer = scenario_gate.Admission(run_dir, exclusive=True)

    def ordinary_scope():
        with scenario_gate.admit(run_dir):
            frames = live.frames(run_dir, 'fixture socket', managed=True, drain=True)
            # Compress the legacy bounded join so this regression need not wait its normal 12-second grace.
            real_join = frames.join
            frames.join = lambda timeout=None: real_join(.03 if timeout is not None else None)
            with frames:
                assert captured.wait(3)
                exiting.set()
        exited.set()

    thread = threading.Thread(target=ordinary_scope, daemon=True)
    thread.start()
    try:
        assert exiting.wait(3)
        assert not exited.wait(.15), 'live teardown released an active grabber'
        assert writer.poll() is False
        release.set()
        thread.join(5)
        assert not thread.is_alive() and exited.is_set()
        assert writer.poll() is True
        assert not any(fronted), 'an old grabber brought its page forward after releasing admission'
    finally:
        release.set()
        thread.join(5)
        writer.close()


@pytest.fixture
def cleanup_server():
    state = SimpleNamespace(requests=[], present=True, hold_close=False, keep_target=False,
                            closing=threading.Event(), release=threading.Event())

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state.requests.append(self.path)
            if self.path == '/json/list':
                body = [{'id': 'owned'}] if state.present else []
            elif self.path == '/json/close/owned':
                state.closing.set()
                if state.hold_close:
                    assert state.release.wait(6), 'cleanup fixture release missing'
                if not state.keep_target:
                    state.present = False
                body = {'closed': True}
            else:
                raise AssertionError(f'unexpected cleanup fixture request: {self.path}')
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f'http://127.0.0.1:{server.server_port}'
    yield state
    state.release.set()
    server.shutdown()
    server.server_close()
    thread.join(2)


def _owned_scope_then_die(run_dir, marker, exclusive, failure):
    watchdog.notify('scenario', name='owned')
    with watchdog.admission(run_dir, exclusive=exclusive):
        watchdog.notify('resource', targets=['owned'])
        _write(marker, {'pid': os.getpid()})
        _wait(str(marker) + '.release', timeout=15)
        if failure == 'error':
            raise RuntimeError('fixture operation unwind')
        if failure == 'transport':
            raise session.TransportLost('fixture transport unwind')
        if failure == 'interrupt':
            raise KeyboardInterrupt
        os._exit(7)


def _supervision_thread(target, args, *, run_dir, url=None, timeout=3):
    answers = []

    def run():
        try:
            answers.append(watchdog.supervise(target, args, timeout=timeout, cdp_url=url,
                                               admission_dir=run_dir))
        except BaseException as error:
            answers.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, answers


@pytest.mark.parametrize('exclusive', [False, True], ids=['failed-reader', 'failed-writer'])
@pytest.mark.parametrize('failure_mode', ['death', 'error', 'transport', 'interrupt'])
def test_supervisor_retains_failed_child_lease_through_owned_cleanup(tmp_path, cleanup_server, exclusive, failure_mode):
    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    marker = tmp_path / 'owned.json'
    cleanup_server.hold_close = True
    thread, answers = _supervision_thread(_owned_scope_then_die, (run_dir, marker, exclusive, failure_mode),
                                          run_dir=run_dir, url=cleanup_server.url)
    contender = None
    try:
        _wait(marker)
        contender = scenario_gate.Admission(run_dir, exclusive=not exclusive)
        assert contender.poll() is False
        _write(str(marker) + '.release', True)
        assert cleanup_server.closing.wait(5), 'same-lease cleanup blocked behind its own queued contender'
        assert contender.poll() is False, 'child death released foreground ownership before cleanup finished'
        cleanup_server.release.set()
        thread.join(8)
        assert not thread.is_alive() and answers and isinstance(answers[0], tuple), answers
        value, failure = answers[0]
        assert value is None
        expected = {'death': 'worker_error', 'error': 'worker_error',
                    'transport': 'transport_lost', 'interrupt': 'interrupted'}
        assert failure['stop'] == expected[failure_mode]
        assert failure['cleanup_complete'] is True
        assert contender.poll() is True
    finally:
        if contender:
            contender.close()
        _write(str(marker) + '.release', True)
        cleanup_server.release.set()
        thread.join(10)


def test_bounded_cleanup_acquires_inside_child_before_any_cdp(tmp_path, cleanup_server):
    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    writer = scenario_gate.Admission(run_dir, exclusive=True)
    assert writer.poll() is True
    answers = []
    began = time.monotonic()
    thread = threading.Thread(target=lambda: answers.append(watchdog.cleanup(
        multiprocessing.get_context('spawn'), cleanup_server.url, {'owned'}, set(), None,
        admission_dir=run_dir)), daemon=True)
    thread.start()
    try:
        thread.join(8)
        assert not thread.is_alive(), 'cleanup admission escaped its bounded worker'
        assert answers == [False]
        assert time.monotonic() - began < 8
        assert cleanup_server.requests == [], 'cleanup touched CDP while another scenario held exclusive admission'
    finally:
        writer.close()
        thread.join(8)
    assert watchdog.cleanup(multiprocessing.get_context('spawn'), cleanup_server.url,
                            {'owned'}, set(), None, admission_dir=run_dir) is True
    assert cleanup_server.requests == ['/json/list', '/json/close/owned', '/json/list']


def _unacknowledged_resource_then_die(run_dir):
    watchdog.notify('scenario', name='unknown-owner')
    with watchdog.admission(run_dir):
        watchdog.notify('resource_pending', method='Target.createTarget', request_id='unknown')
        os._exit(7)


def test_uncertain_failed_worker_cleanup_refuses_later_admission(tmp_path):
    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    _, failure = watchdog.supervise(_unacknowledged_resource_then_die, (run_dir,),
                                    timeout=3, admission_dir=run_dir)
    assert failure['cleanup_complete'] is False
    assert failure['resource_ownership_uncertain'] is True
    for exclusive in (False, True):
        with pytest.raises(scenario_gate.AdmissionError, match='cleanup.*uncertain'):
            with scenario_gate.admit(run_dir, exclusive=exclusive):
                raise AssertionError('uncertain cleanup granted foreground admission')


def _queued_scope(run_dir, marker):
    watchdog.notify('scenario', name='queued')
    _write(marker, {'pid': os.getpid()})
    with watchdog.admission(run_dir, exclusive=True):
        return {'entered': True}


def _await_writer_intent(run_dir):
    deadline = time.monotonic() + 5
    while True:
        probe = scenario_gate.Admission(run_dir)
        try:
            if not probe.poll():
                return
        finally:
            probe.close()
        assert time.monotonic() < deadline, 'supervisor never registered the queued writer intent'
        time.sleep(.01)


def test_queued_child_death_is_detected_without_waiting_for_the_room(tmp_path):
    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    reader = scenario_gate.Admission(run_dir)
    assert reader.poll() is True
    marker = tmp_path / 'queued.json'
    thread, answers = _supervision_thread(_queued_scope, (run_dir, marker), run_dir=run_dir)
    try:
        child = _wait(marker)['pid']
        _await_writer_intent(run_dir)
        os.kill(child, signal.SIGKILL)
        thread.join(5)
        assert not thread.is_alive(), 'supervisor blocked on a dead queued child'
        assert answers and isinstance(answers[0], tuple), answers
        assert answers[0][1]['stop'] == 'worker_error'
    finally:
        reader.close()
        thread.join(8)
    with scenario_gate.admit(run_dir, exclusive=True):
        pass


def test_admission_queue_time_does_not_expire_semantic_progress(tmp_path):
    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    reader = scenario_gate.Admission(run_dir)
    assert reader.poll() is True
    marker = tmp_path / 'queued.json'
    thread, answers = _supervision_thread(_queued_scope, (run_dir, marker), run_dir=run_dir, timeout=2)
    try:
        _wait(marker)
        _await_writer_intent(run_dir)
        assert not answers
        thread.join(2.1)
        assert thread.is_alive() and not answers, 'admission wait consumed the semantic-progress deadline'
    finally:
        reader.close()
        thread.join(8)
    assert answers and answers[0] == ({'entered': True}, None), answers
