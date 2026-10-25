"""Process boundary for the website runner's five-minute semantic-progress deadline.

Never interrupt a caller thread with a Python exception: native code can hold the GIL,
and MCP callers need not run on the main thread. The supervisor owns a spawned process
and its process group, with bounded termination and separately bounded browser cleanup.
"""

import contextlib
import multiprocessing
import os
import signal
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

TIMEOUT = 300.0
STOP_GRACE = 1.0
CLEANUP_LIMIT = 5.0
_sink = None
_critical_state = None
_send_lock = threading.Lock()
# Each category has one writer at a time under _send_lock. The supervisor reads after stopping that process.
CRITICAL = {"cost_pending": 0, "ledger": 0, "resource": 1, "resource_pending": 1,
            "resource_resolved": 1, "runtime": 2}
READY_SLOT = 3


def recording():
    return _sink is not None


def notify(kind, **data):
    if _sink is None and _critical_state is None:
        return
    from .config import redact_tree

    with _send_lock:
        if _critical_state is not None and kind in CRITICAL:
            category = CRITICAL[kind]
            _critical_state[category] += 1
            data["_critical"] = (category, int(_critical_state[category]))
        if _sink is not None:
            _sink.send(redact_tree({"kind": kind, "monotonic": time.monotonic(), **data}))


def active(identity):
    """Name the current operation without claiming progress (including repeated polls)."""
    notify("active", identity=identity)


def complete(identity, **data):
    notify("complete", identity=identity, **data)


@contextmanager
def operation(identity):
    active(identity)
    yield
    complete(identity)


def call(identity, fn, *args, **kwargs):
    with operation(identity):
        return fn(*args, **kwargs)


@contextmanager
def admission(run_dir, exclusive=False):
    """The supervisor keeps admission through a killed worker's owned cleanup."""
    from . import scenario_gate

    if run_dir is None:
        yield 0.0
        return
    began = time.monotonic()
    notify("queued")
    granted = False
    request_id = uuid.uuid4().hex
    try:
        if _sink is None:  # Direct runner calls, including small fixture tests.
            with scenario_gate.admit(run_dir, exclusive) as waited:
                notify("admitted")
                yield waited
        else:
            notify("admission_request", request_id=request_id, exclusive=exclusive)
            reply = _sink.recv()
            if reply.get("kind") != "admission_reply" or reply.get("request_id") != request_id:
                raise scenario_gate.AdmissionError("invalid scenario admission reply")
            if reply.get("error"):
                raise scenario_gate.AdmissionError(reply["error"])
            granted = True
            notify("admitted")
            yield time.monotonic() - began
    except BaseException as error:
        if granted:
            # An unwinding operation may still have a daemon command in flight. The supervisor must
            # stop this worker and clean up under its existing lease, rather than admit another tab.
            stop_reason = ("interrupted" if isinstance(error, KeyboardInterrupt) else
                           "transport_lost" if type(error).__name__ == "TransportLost" else "worker_error")
            notify("admission_abort", request_id=request_id, stop=stop_reason,
                   detail=f"{type(error).__name__}: {error}")
            granted = False
        raise
    finally:
        if granted:
            notify("admission_release", request_id=request_id)
        notify("admitted")  # An admission error must not leave the active clock suspended.


@dataclass
class Progress:
    timeout: float = TIMEOUT
    scenario: str | None = None
    identity: str = "worker startup"
    last: float = field(default_factory=time.monotonic)
    started: float = field(default_factory=time.monotonic)
    queued: bool = False
    queued_at: float = 0.0
    queue_seconds: float = 0.0
    partial: dict = field(default_factory=dict)

    def receive(self, message, now=None):
        now = time.monotonic() if now is None else now
        now = message.get("monotonic", now)
        kind = message["kind"]
        if kind == "scenario":
            self.scenario = message["name"]
            self.identity = "session setup"
            self.last = self.started = now
            self.partial = {}
            self.queue_seconds = 0.0
        elif kind == "queued":
            self.queued, self.queued_at = True, now
        elif kind == "admitted" and self.queued:
            waited = now - self.queued_at
            # Admission is not progress. Suspend the active clock for queue time only.
            self.last += waited
            self.queue_seconds += waited
            self.queued = False
        elif kind == "active":
            self.identity = message["identity"]
        elif kind == "complete":
            self.last = now
            self.identity = message["identity"]
        if message.get("partial") is not None:
            self.partial = message["partial"]

    def expired(self, now=None):
        now = time.monotonic() if now is None else now
        return not self.queued and now - self.last >= self.timeout


class _Stopped(BaseException):
    pass


def _terminate(_signum, _frame):
    raise _Stopped()


def _child(pipe, target, args, kwargs, critical):
    global _sink, _critical_state
    # POSIX is explicit: never silently run without an enforceable process boundary.
    os.setsid()
    critical[READY_SLOT] = 1  # Group ownership must not depend on receipt of the queued ready message.
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the supervisor owns operator interruption
    _sink = pipe
    _critical_state = critical
    notify("ready", pid=os.getpid())
    try:
        result = target(*args, **kwargs)
        notify("returned", result=result)
    except _Stopped:
        pass
    except BaseException as error:
        notify("error", detail=f"{type(error).__name__}: {error}")
    finally:
        pipe.close()
        _sink = None


def stop(process, grouped, *, ready=None):
    """TERM then KILL, including shell descendants. Every wait has a fixed bound."""
    if process.pid is None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        grouped = grouped or (ready is not None and bool(ready[READY_SLOT]))
        if grouped:
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                if process.is_alive():
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(process.pid, sig)
        elif process.is_alive():
            with contextlib.suppress(ProcessLookupError):
                os.kill(process.pid, sig)
        process.join(STOP_GRACE)
        # Even if the direct child has exited, KILL its remaining group descendants.
    if process.is_alive():
        raise RuntimeError(f"watchdog could not reap owned worker {process.pid}")


def _cleanup(cdp_url, targets, contexts, daemon):
    from . import chrome

    cleaned = True
    if daemon:
        from browser_harness import admin

        name = daemon["name"]
        pid, token = daemon.get("pid"), daemon.get("start")
        if not pid or not token or chrome.started(pid) not in {None, token}:
            cleaned = False  # Unknown ownership or a reused PID: never signal it.
        else:
            admin.restart_daemon(name)
            cleaned = chrome.reaped(pid)
        # Stop the daemon before closing its tabs: its recovery code must not create a replacement tab.
    present = {t["id"] for t in chrome.targets(cdp_url)} if targets else set()
    for target in targets & present:
        cleaned = chrome.close_target(cdp_url, target) and cleaned
    if targets:
        cleaned = not (targets & {t["id"] for t in chrome.targets(cdp_url)}) and cleaned
    if contexts:
        import json

        from websockets.sync.client import connect

        version = chrome.version(cdp_url)
        if not version:
            return False
        # Use a fresh direct connection: the failed daemon is never used for cleanup.
        with connect(version["webSocketDebuggerUrl"], open_timeout=1, close_timeout=0.2) as socket:
            socket.send(json.dumps({"id": 1, "method": "Target.getBrowserContexts"}))
            while True:
                reply = json.loads(socket.recv(timeout=1))
                if reply.get("id") == 1:
                    present_contexts = set(reply["result"]["browserContextIds"])
                    break
            for n, context in enumerate(contexts & present_contexts, 2):
                socket.send(json.dumps({"id": n, "method": "Target.disposeBrowserContext",
                                        "params": {"browserContextId": context}}))
                while True:
                    reply = json.loads(socket.recv(timeout=1))
                    if reply.get("id") == n:
                        cleaned = "error" not in reply and cleaned
                        break
            socket.send(json.dumps({"id": 1000000, "method": "Target.getBrowserContexts"}))
            while True:
                reply = json.loads(socket.recv(timeout=1))
                if reply.get("id") == 1000000:
                    cleaned = not (contexts & set(reply["result"]["browserContextIds"])) and cleaned
                    break
    return cleaned


def _cleanup_child(pipe, cdp_url, targets, contexts, daemon, admission_dir=None):
    from . import scenario_gate

    os.setsid()
    try:
        # Waiting is inside the existing bounded cleanup process, never in its supervisor.
        with scenario_gate.admit(admission_dir):
            pipe.send(_cleanup(cdp_url, targets, contexts, daemon))
    except Exception:
        pipe.send(False)
    finally:
        pipe.close()


def cleanup(ctx, cdp_url, targets, contexts, daemon, admission_dir=None):
    """Browser/daemon cleanup cannot extend a watchdog stop indefinitely."""
    if not targets and not contexts and not daemon:
        return True
    receive, send = ctx.Pipe(duplex=False)
    child = ctx.Process(target=_cleanup_child, args=(send, cdp_url, targets, contexts, daemon, admission_dir))
    child.start()
    send.close()
    try:
        if receive.poll(CLEANUP_LIMIT):
            with contextlib.suppress(EOFError):
                return bool(receive.recv())
        return False
    finally:
        stop(child, grouped=True)
        receive.close()


def supervise(target, args=(), kwargs=None, *, emit=None, timeout=TIMEOUT, cdp_url=None, admission_dir=None):
    """Return (value, failure). Stream trusted runner events; ignore them for liveness.

    IPC identity changes never reset the deadline. Only explicit semantic completion
    does. The deadline also bounds startup and teardown, not only loop boundaries.
    """
    if os.name != "posix" or not hasattr(os, "setsid"):
        raise RuntimeError("scenario progress watchdog requires POSIX process groups; run refused")
    from . import scenario_gate

    ctx = multiprocessing.get_context("spawn")
    receive, send = ctx.Pipe(duplex=True)  # Admission grants go back to the waiting scenario worker.
    critical = ctx.RawArray("Q", 4)
    seen = [0, 0, 0]
    process = ctx.Process(target=_child, args=(send, target, args, kwargs or {}, critical))
    state = Progress(timeout=timeout)
    results, targets, contexts, pending, daemon, ledger = [], set(), set(), set(), None, None
    cost_pending = set()
    runtime = {}
    grouped = False
    process.start()
    send.close()
    failure: dict[str, Any] | None = None
    value = None
    lease = waiting_admission = None
    admission_id = None
    try:
        while True:
            if waiting_admission is not None:
                try:
                    admitted = waiting_admission.poll()
                except (scenario_gate.AdmissionError, OSError) as error:
                    receive.send({"kind": "admission_reply", "request_id": admission_id, "error": str(error)})
                    waiting_admission.close()
                    waiting_admission = None
                else:
                    if admitted:
                        lease, waiting_admission = waiting_admission, None
                        receive.send({"kind": "admission_reply", "request_id": admission_id})
            # Check before reading: an endless event/console stream cannot starve the timer.
            if state.expired():
                failure = {"stop": "no_progress", "detail": f"no real progress for {timeout:g} s"}
                break
            if receive.poll(0.05):
                try:
                    message = receive.recv()
                except EOFError:
                    failure = {"stop": "worker_error", "detail": "scenario worker exited without a result"}
                    break
                kind = message["kind"]
                state.receive(message)
                if kind == "ready":
                    grouped = True
                elif kind == "admission_request":
                    if admission_dir is None or lease is not None or waiting_admission is not None:
                        failure = {"stop": "worker_error", "detail": "invalid nested scenario admission"}
                        break
                    admission_id = message["request_id"]
                    try:
                        waiting_admission = scenario_gate.Admission(admission_dir, message["exclusive"])
                    except OSError as error:
                        receive.send({"kind": "admission_reply", "request_id": admission_id, "error": str(error)})
                elif kind == "admission_release":
                    if lease is None or message["request_id"] != admission_id:
                        failure = {"stop": "worker_error", "detail": "invalid scenario admission release"}
                        break
                    lease.close()
                    lease = None
                elif kind == "admission_abort":
                    if lease is None or message["request_id"] != admission_id:
                        failure = {"stop": "worker_error", "detail": "invalid scenario admission abort"}
                    else:
                        failure = {"stop": message["stop"], "detail": message["detail"]}
                    break
                elif kind == "resource":
                    targets.update(message.get("targets", []))
                    contexts.update(message.get("contexts", []))
                    if message.get("daemon"):
                        daemon = {**(daemon or {}), "name": message["daemon"]}
                    if message.get("daemon_pid"):
                        daemon = {**(daemon or {}), "pid": message["daemon_pid"],
                                  "start": message["daemon_start"]}
                elif kind == "resource_pending":
                    pending.add(message["request_id"])
                elif kind == "resource_resolved":
                    pending.discard(message["request_id"])
                elif kind == "ledger":
                    ledger = message["value"]
                elif kind == "cost_pending":
                    if message["pending"]:
                        cost_pending.add(message["call_id"])
                    else:
                        cost_pending.discard(message["call_id"])
                elif kind == "runtime":
                    runtime = message["value"]
                elif kind == "event":
                    event = message["value"]
                    if event.get("event") == "scenario":
                        results.append(event["result"])
                    if callable(emit):
                        emit(event)
                elif kind == "returned":
                    if lease is not None or waiting_admission is not None:
                        failure = {"stop": "worker_error", "detail": "worker returned before releasing admission"}
                        break
                    value = message["result"]
                    break
                elif kind == "error":
                    failure = {"stop": "worker_error", "detail": message["detail"]}
                    break
                if "_critical" in message:
                    category, generation = message["_critical"]
                    seen[category] = generation
            elif not process.is_alive():
                failure = {"stop": "worker_error", "detail": f"scenario worker exited {process.exitcode}"}
                break
    except KeyboardInterrupt:
        failure = {"stop": "interrupted", "detail": "stopped by the operator"}
    except (OSError, EOFError) as error:
        failure = {"stop": "worker_error", "detail": f"scenario worker channel lost: {type(error).__name__}"}
    finally:
        ended = time.monotonic()
        stop(process, grouped, ready=critical)
        receive.close()
        if waiting_admission is not None:
            waiting_admission.close()
    if failure is not None:
        # Do not drain a potentially partial pipe frame. Unread critical updates are conservatively uncertain.
        unread = [int(critical[index]) != seen[index] for index in range(3)]
        # An admitted worker may leave an outstanding daemon command after death. Keep its exact lease
        # through cleanup. Without a lease, acquire shared admission inside the bounded cleanup child.
        cleanup_complete = False
        try:
            cleaned = cleanup(ctx, cdp_url, targets, contexts, daemon,
                              admission_dir=admission_dir if lease is None else None)
            cleanup_complete = cleaned and not pending and not unread[1]
        finally:
            try:
                if admission_dir is not None and not cleanup_complete:
                    scenario_gate.poison(admission_dir)
            finally:
                if lease is not None:
                    lease.close()
        queued = state.queue_seconds + (ended - state.queued_at if state.queued else 0)
        failure.update(scenario=state.scenario, identity=state.identity, partial=state.partial,
                       results=results, ledger=ledger, cleanup_complete=cleanup_complete,
                       cost_uncertain=bool(cost_pending) or unread[0],
                       runtime=runtime,
                       resource_ownership_uncertain=bool(pending) or unread[1],
                       runtime_state_incomplete=unread[2],
                       idle_seconds=round(ended - state.last - (ended - state.queued_at if state.queued else 0), 3),
                       active_seconds=round(ended - state.started - queued, 3),
                       queue_seconds=round(queued, 3))
    return value, failure
