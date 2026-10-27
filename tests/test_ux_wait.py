"""A shorter UX settle budget preserves scans and admits no false quiet result."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qajev import ux_checks, ux_options


def busy_observer(monkeypatch, cap=125, state=None, network_quiet=10000):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(ux_checks.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(ux_checks.time, "sleep", lambda delay: setattr(clock, "now", clock.now + delay))
    session = Mock()
    session.net = SimpleNamespace(ux_quiet_ms=lambda: network_quiet)
    config = {**ux_options.resolve(), "ux_settle_timeout_ms": cap}
    observer = ux_checks.Observer(session, config, None, "busy")
    observer.probe = Mock(return_value=state or {"quiet_ms": 0, "finite_animations": 0})
    observer.scan = Mock()
    observer.collect = Mock()
    return observer, clock


@pytest.mark.parametrize("state,network_quiet", [
    ({"quiet_ms": 0, "finite_animations": 0}, 10000),
    ({"quiet_ms": 10000, "finite_animations": 1}, 10000),
    ({"quiet_ms": 10000, "finite_animations": 0}, 0),
])
def test_busy_page_stops_waiting_but_keeps_every_scan_and_unsettled_note(monkeypatch, state, network_quiet):
    observer, clock = busy_observer(monkeypatch, state=state, network_quiet=network_quiet)
    observer.start()
    observer.before(action="script")
    observer.after()
    observer.finish()
    assert clock.now <= 0.376, "three UX waits must use the configured 125ms cap"
    assert observer.scan.call_count == 3, "startup, after-action and final scans all still run"
    assert observer.result["notes"] == ["page did not settle"]
    observer.collect.assert_called_once()
    assert observer.in_action is False


@pytest.mark.parametrize("explicit,expected", [(2, .125), (.05, .05)])
def test_internal_wait_uses_the_shorter_of_its_own_budget_and_run_cap(monkeypatch, explicit, expected):
    observer, clock = busy_observer(monkeypatch)
    assert observer.settle(quiet_ms=300, timeout=explicit) is False
    assert clock.now == pytest.approx(expected)
    assert observer.result["notes"] == ["page did not settle"]


def test_zero_cap_still_probes_and_scans_without_sleeping(monkeypatch):
    observer, clock = busy_observer(monkeypatch, cap=0)
    observer.start()
    assert clock.now == 0
    observer.probe.assert_called_once_with("settleInfo")
    observer.scan.assert_called_once()
    assert observer.result["notes"] == ["page did not settle"]


def test_default_wait_and_a_genuinely_quiet_page_keep_existing_behavior(monkeypatch):
    observer, clock = busy_observer(monkeypatch, cap=10000)
    assert observer.settle() is False
    assert clock.now == pytest.approx(10)
    observer, clock = busy_observer(monkeypatch, cap=10000,
                                   state={"quiet_ms": 10000, "finite_animations": 0})
    assert observer.settle() is True
    assert .75 <= clock.now < .81
    assert not observer.result["notes"]
