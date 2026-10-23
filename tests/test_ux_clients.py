"""One client's incomplete scan must survive the shared multiplayer verdict."""
from types import SimpleNamespace

import pytest

from qajev import clients, session, suite


@pytest.mark.parametrize("mode,outcome", [("advisory", "pass"), ("strict", "fail")])
@pytest.mark.parametrize("status,with_finding", [("incomplete", False), ("complete", True)])
def test_incomplete_client_scan_is_preserved_and_strict_cannot_pass(
        tmp_path, monkeypatch, mode, outcome, status, with_finding):
    class UX:
        def safe(self, function):
            function()

        def start(self):
            pass

        def finish(self):
            pass

        def apply(self, result):
            findings = [{"check": "UX-TAP", "message": "Small target", "element": {"key": "#small"},
                         "screenshot": "ux/small.png"}] if with_finding else []
            result["ux"] = {"mode": mode, "status": status, "findings": findings, "notes": [], "skipped": [
                {"check": "UX-INPUT-NUMBER", "reason": "scan incomplete: budget exhausted",
                 "unchecked": [{"key": "#fee", "desc": 'input[text] "Fee"'}]}] if status == "incomplete" else []}
            if mode == "strict":
                result.update(outcome="fail", reason="ux: scan incomplete" if status == "incomplete"
                              else "ux: 1 findings")

    class Page:
        def __init__(self, *_, **__):
            self.jev = SimpleNamespace(cdp=None, admin=SimpleNamespace(ensure_daemon=None))
            self.why_failed = lambda *_: None

        def arm(self, *_):
            pass

        def set_device(self, *_):
            pass

        def configure_ux(self, *_):
            self.ux_observer = UX()
            return self.ux_observer

        def navigate(self, url):
            self.url = url

        def probe(self, _):
            return {"url": self.url, "status": 200, "text": [True], "probe": {}}

        def screenshot(self, _):
            return None

        def close(self):
            pass
    monkeypatch.setattr(session, "Session", Page)
    monkeypatch.setattr(session, "Tab", lambda *_: SimpleNamespace(close=lambda: None))
    parsed = suite.parse({"name": "UX clients", "devices": ["phone"], "scenarios": [{
        "name": "Shared form", "url": "http://127.0.0.1:8877", "clients": ["one", "two"],
        "expect": {"text": ["Ready"]}}]})
    result = clients.run(parsed.scenarios[0], ledger=None, hosts={"127.0.0.1:8877"}, run_dir=tmp_path,
                         suite_meta={"headless": True, "guard": {}, "ux_options": {"ux": mode}})
    assert result["ux"]["status"] == status
    assert len(result["ux"]["skipped"]) == (2 if status == "incomplete" else 0)
    assert len(result["ux"]["findings"]) == (2 if with_finding else 0)
    assert result["outcome"] == outcome
    if mode == "strict":
        assert result["reason"] == ("ux: scan incomplete" if status == "incomplete" else "ux: 2 findings")
