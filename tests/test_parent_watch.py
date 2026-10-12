"""The parent watch (cli._stop_when_orphaned) waits on an event, never on time.sleep: tests swap time.sleep for fake
clocks, and a watch thread that called it then spun and moved their clock (CI run 38284007154 hung 120 s)."""

import threading
import time

from qajev import cli


def test_the_parent_watch_never_calls_time_sleep_and_stops_on_request(monkeypatch):
    monkeypatch.delenv("QAJEV_OUTLIVE_PARENT", raising=False)
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    stop = threading.Event()
    thread = cli._stop_when_orphaned(every=0.01, stop=stop)
    assert thread is not None and thread.is_alive()
    threading.Event().wait(0.1)  # about 10 rounds of the watch
    stop.set()
    thread.join(1)
    assert not thread.is_alive() and slept == []


def test_no_test_leaves_a_parent_watch_running():
    assert not [t for t in threading.enumerate() if t.name == "qajev-parent-watch" and t.is_alive()]
