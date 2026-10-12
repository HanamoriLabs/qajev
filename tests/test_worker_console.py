"""A worker's console errors and warnings, and its uncaught exceptions, are findings (foleyapp, 13 Oct: Foley's database
runs in a worker, and its SQLITE_CORRUPT never reached the page's console, so QAJev could not show it)."""

from qajev import netlog, verdict

WORKER = {"sessionId": "w1", "targetInfo": {"type": "worker", "targetId": "t1",
                                            "url": "http://localhost:5284/_app/immutable/workers/db-worker.js"}}


def listener():
    f = netlog.Failures("ws://unused")
    f.take({"method": "Target.attachedToTarget", "params": WORKER})
    return f


def test_a_workers_console_errors_warnings_and_exceptions_are_kept_and_its_logs_are_not():
    f = listener()
    f.take({"sessionId": "w1", "method": "Runtime.consoleAPICalled", "params": {
        "type": "error",
        "args": [{"type": "string", "value": "SQLite3Error: SQLITE_CORRUPT: database disk image is malformed"}]}})
    f.take({"sessionId": "w1", "method": "Runtime.consoleAPICalled", "params": {
        "type": "warning",
        "args": [{"type": "string", "value": "OPFS pool:"}, {"type": "number", "value": 3, "description": "3"}]}})
    f.take({"sessionId": "w1", "method": "Runtime.consoleAPICalled", "params": {
        "type": "log", "args": [{"type": "string", "value": "opened"}]}})
    f.take({"sessionId": "w1", "method": "Runtime.exceptionThrown", "params": {"exceptionDetails": {
        "text": "Uncaught", "exception": {"description": "Error: lock lost\n    at db-worker.js:9"}}}})
    logs = f.take_worker_logs()
    assert [(e["level"], e["detail"]) for e in logs] == [
        ("error", "db-worker.js: SQLite3Error: SQLITE_CORRUPT: database disk image is malformed"),
        ("warning", "db-worker.js: OPFS pool: 3"),
        ("error", "db-worker.js: Error: lock lost"),
    ]
    assert f.take_worker_logs() == []  # taken once


def test_a_page_or_frame_session_console_is_not_a_worker_finding():
    f = listener()
    f.take({"sessionId": "frame9", "method": "Runtime.consoleAPICalled", "params": {
        "type": "error", "args": [{"type": "string", "value": "from a frame"}]}})
    f.take({"method": "Runtime.consoleAPICalled", "params": {"type": "error", "args": [{"value": "page"}]}})
    assert f.take_worker_logs() == []


def test_worker_logs_become_findings():
    probe = {"errors": [{"kind": "worker", "level": "error", "detail": "db-worker.js: SQLITE_CORRUPT"},
                        {"kind": "worker", "level": "warning", "detail": "db-worker.js: slow"}]}
    found = verdict.findings_from_probe(probe, scenario="s", url="http://localhost:5284/",
                                        first_party_hosts={"localhost:5284"})
    assert [(f["severity"], f["kind"], f["detail"]) for f in found] == [
        ("S2", "worker error", "db-worker.js: SQLITE_CORRUPT"), ("S3", "worker warning", "db-worker.js: slow")]
