from types import SimpleNamespace

import pytest

from qajev import cli


@pytest.mark.parametrize("mode,status,expected", [("advisory", "complete", "UX 1 advisory"),
                                                 ("strict", "complete", "UX 1"),
                                                 ("advisory", "incomplete", "UX scan incomplete")])
def test_terminal_verdict_keeps_the_ux_count_or_incomplete_status_on_stderr(capsys, mode, status, expected):
    result = {"name": "Phone form", "outcome": "pass" if mode == "advisory" else "fail", "reason": "loaded",
              "ux": {"mode": mode, "status": status, "findings": [{"check": "UX-COVER"}], "skipped": [], "notes": []}}
    cli._screen_printer(SimpleNamespace(events=False, quiet=False))({"event": "scenario", "result": result})
    out = capsys.readouterr()
    assert out.out == ""
    assert expected in out.err and result["outcome"] in out.err


def test_off_terminal_result_adds_no_ux_text(capsys):
    cli._screen_printer(SimpleNamespace(events=False, quiet=False))({"event": "scenario", "result": {
        "name": "Phone form", "outcome": "pass", "reason": "loaded"}})
    out = capsys.readouterr()
    assert "UX" not in out.err and out.out == ""
