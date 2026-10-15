"""An alias is a request, not proof of which model answered. No provider calls."""

import json
import time

import pytest

from qajev import providers, report, suite
from qajev.ledger import Ledger


def test_openrouter_shows_the_unpinned_warning_without_warning_about_typesafe():
    routed = providers.describe(providers.resolve({"OPENROUTER_API_KEY": "fixture-key"}))
    pinned = providers.describe(providers.resolve({"TYPESAFE_API_KEY": "fixture-key"}))
    assert "not pinned" in routed["jev"]
    assert "not pinned" not in pinned["jev"]


@pytest.mark.parametrize("reply, expected", [
    ({"model": "jev-1.13.0"}, "jev-1.13.0"),
    ({"model": "jev", "upstream_model": "jev-1.14.0"}, "jev-1.14.0"),
    ({"model": "jev", "upstream_model": None}, "unreported"),
    ({}, "unreported"),
])
def test_the_report_records_the_answered_model_and_never_substitutes_the_requested_alias(tmp_path, reply, expected):
    ledger = Ledger()
    post = ledger.wrap(lambda *_: {**reply, "usage": {"input_tokens": 1}})
    post(providers.OPENROUTER_SYSTEMONE, "fixture-key", {"model": "~typesafe/jev-latest"})
    s = suite.parse({"name": "Version evidence", "scenarios": [{"name": "fixture", "url": "https://example.test/",
                                                              "expect": {"text": ["Fixture"]}}]})
    data = report.build(s, [], [ledger.summary()], browser={}, started_at=time.time(), strict=False,
                        interrupted=False, run_dir=tmp_path)
    report.write(tmp_path, data)
    assert json.loads((tmp_path / "report.json").read_text())["cost"]["answered_models"] == [expected]
    for name in ("report.md", "report.html"):
        text = (tmp_path / name).read_text()
        assert "Answered models" in text and expected in text
        assert "~typesafe/jev-latest" not in text


def test_the_report_keeps_all_answered_versions_when_an_alias_changes():
    a, b = Ledger(), Ledger()
    a.after(providers.OPENROUTER_SYSTEMONE, {"model": "jev-1.13.0"})
    b.after(providers.OPENROUTER_SYSTEMONE, {"model": "jev-1.14.0"})
    assert report.merge_ledgers([a.summary(), b.summary()])["answered_models"] == ["jev-1.13.0", "jev-1.14.0"]
