"""An attached Chrome's address (`--cdp-url .../devtools/browser/<id>`) holds that browser's key: a run keeps the
key out of everything it records or prints (redteam-1's L1 and L2 on #295, 7 Nov 2026). No browser starts here."""

import json
from types import SimpleNamespace

import pytest

from qajev import chrome, runner, smoke, suite

KEY = "kQ7fakeRelayKeyOnlyThisTest0x92"
ADDRESS = f"http://127.0.0.1:9432/devtools/browser/{KEY}"
SHOWN = "http://127.0.0.1:9432/devtools/browser/<id>"


def _suite():
    return suite.parse({"name": "attached", "base_url": "http://localhost", "scenarios": [
        {"name": "home", "url": "/", "about": "Fixture: the run's record", "expect": {"text": "fixture"}}]})


def test_a_run_on_an_attached_chrome_records_its_address_without_the_key(monkeypatch, tmp_path):
    monkeypatch.setattr(chrome, "reap", lambda: [])
    monkeypatch.setattr(chrome, "version", lambda url, timeout=1.0: {"Browser": "fixture"} if url == ADDRESS else None)
    seen = []

    def group(scenarios, **kw):
        seen.append(kw.get("cdp_url"))
        return {"ledger": kw["ledger"].summary(), "results": [
            {"name": s.name, "url": s.url, "outcome": "pass", "reason": "fixture", "checks": [], "findings": [],
             "screens": [], "seconds": 0} for s in scenarios]}

    monkeypatch.setattr(runner, "run_group", group)
    built = runner.run(_suite(), runner.Options(cdp_url=ADDRESS, out_dir=tmp_path))
    assert built["browser"]["cdp_url"] == SHOWN
    assert KEY not in json.dumps(built, default=str)
    assert seen == [ADDRESS]  # the run itself still drives the real address


def test_a_run_whose_attached_chrome_does_not_answer_names_it_without_the_key(monkeypatch, tmp_path):
    monkeypatch.setattr(chrome, "reap", lambda: [])
    monkeypatch.setattr(chrome, "version", lambda url, timeout=1.0: None)
    with pytest.raises(runner.ConfigError) as refused:
        runner.run(_suite(), runner.Options(cdp_url=ADDRESS, out_dir=tmp_path))
    assert str(refused.value) == f"no Chrome DevTools endpoint at {SHOWN}"


def test_a_smoke_whose_attached_chrome_does_not_answer_names_it_without_the_key(monkeypatch, tmp_path):
    monkeypatch.setattr(chrome, "reap", lambda: [])
    monkeypatch.setattr(chrome, "version", lambda url, timeout=1.0: None)
    with pytest.raises(runner.ConfigError) as refused:
        smoke.run("https://shop.example/", SimpleNamespace(emit=None, out_dir=tmp_path, cdp_url=ADDRESS),
                  max_pages=1, device="desktop")
    assert str(refused.value) == f"no Chrome DevTools endpoint at {SHOWN}"
