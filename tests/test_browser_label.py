"""A throwaway Chrome (--ephemeral) is called one in the run's record, its report and `qajev browser status`, not by
the profile name it was started under. 5 Oct: a tester read "QAJev-managed profile default" for a throwaway run and
took it for the shared profile."""

import time
from types import SimpleNamespace

from qajev import chrome, cli, report


def owned(ephemeral):
    return {"profile": "default", "port": 9351, "cdp_url": "http://127.0.0.1:9351", "headless": True, "pid": 1,
            "ephemeral": ephemeral}


def written(tmp_path, browser):
    result = {"name": "home", "url": "http://h/", "goal": None, "outcome": "pass", "reason": "loaded", "checks": [],
              "findings": [], "screens": [], "about": "x"}
    ledger = {"usd": 0.0, "usd_typesafe_estimated": 0.0, "usd_text": 0.0, "calls": {"typesafe": 0, "text": 0},
              "tokens": {"typesafe": 0, "text": 0}, "errors": 0, "text_cost_reported": True, "cap_usd": 0.0}
    data = report.build(SimpleNamespace(name="smoke h"), [result], [ledger], browser=browser, started_at=time.time(),
                        strict=False, interrupted=False, run_dir=tmp_path)
    report.write(tmp_path, data)
    return (tmp_path / "report.md").read_text(), (tmp_path / "report.html").read_text()


def test_a_throwaway_chromes_run_record_and_report_say_so(tmp_path):
    record = chrome.run_record(owned(True))
    assert record == {"cdp_url": "http://127.0.0.1:9351", "managed": True, "profile": "default", "headless": True,
                      "port": 9351, "ephemeral": True}
    md, html = written(tmp_path, record)
    assert "QAJev throwaway profile (deleted after the run)" in md and "profile default" not in md
    assert "QAJev throwaway profile (deleted after the run)" in html and "profile default" not in html


def test_a_named_profile_is_still_called_by_its_name(tmp_path):
    record = chrome.run_record(owned(False))
    assert record["ephemeral"] is False
    md, html = written(tmp_path, record)
    assert "QAJev-managed profile default" in md and "QAJev-managed profile default" in html


def test_browser_status_marks_a_throwaway_chrome(monkeypatch, capsys):
    monkeypatch.setattr(chrome, "status", lambda profile=None: [
        {**owned(True), "alive": True, "tabs": 3},
        {**owned(False), "port": 9350, "cdp_url": "http://127.0.0.1:9350", "pid": 2, "alive": True, "tabs": 1}])
    assert cli.main(["browser", "status"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("default (throwaway)") and "9351" in lines[0]
    assert lines[1].startswith("default ") and "throwaway" not in lines[1]
