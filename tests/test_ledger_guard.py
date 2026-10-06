import json
import multiprocessing
import re

import pytest

from qajev import guard
from qajev.ledger import CostCapReached, Ledger

TS = "https://api.typesafe.ai/v1/systemone"
OR = "https://openrouter.ai/api/v1/chat/completions"


def test_cap_is_checked_before_the_call_is_made():
    calls = []
    ledger = Ledger(cap_usd=0.001, typesafe_usd_per_call=0.0005)
    post = ledger.wrap(lambda url, key, body: calls.append(url) or {"usage": {"total_tokens": 10}})
    post(TS, "k", {})
    post(TS, "k", {})
    with pytest.raises(CostCapReached):
        post(TS, "k", {})
    assert len(calls) == 2
    assert ledger.summary()["calls"]["typesafe"] == 2 and ledger.summary()["usd"] == 0.001


def test_openrouter_is_asked_for_real_cost_and_it_is_used():
    seen = {}

    def post(url, key, body):
        seen.update(body)
        return {"usage": {"prompt_tokens": 5, "completion_tokens": 3, "cost": 0.0002}}

    ledger = Ledger(cap_usd=1)
    ledger.wrap(post)(OR, "k", {"model": "m"})
    assert seen["usage"] == {"include": True}
    s = ledger.summary()
    assert s["usd_text"] == 0.0002 and s["tokens"]["text"] == 8 and s["text_cost_reported"]


def test_failed_calls_are_counted_and_re_raised():
    ledger = Ledger()

    def boom(*_):
        raise RuntimeError("Model provider returned HTTP 500; no action executed.")

    with pytest.raises(RuntimeError):
        ledger.wrap(boom)(TS, "k", {})
    assert ledger.summary()["errors"] == 1 and ledger.summary()["calls"]["typesafe"] == 0


def test_workers_share_one_cap():
    shared = multiprocessing.get_context("spawn").Value("d", 0.0)
    a, b = Ledger(0.001, 0.0005, shared=shared), Ledger(0.001, 0.0005, shared=shared)
    a.wrap(lambda *_: {})(TS, "k", {})
    b.wrap(lambda *_: {})(TS, "k", {})
    with pytest.raises(CostCapReached):
        a.wrap(lambda *_: {})(TS, "k", {})


def matches(patterns, text):
    return any(re.search(p, text, re.I) for p in patterns)


@pytest.mark.parametrize("label", [
    "Sign out", "Log out", "Logout", "Close all tabs", "Delete account", "Delete my account", "Remove workspace",
    "Revoke", "Rotate key", "Pay now", "Buy", "Checkout", "Upgrade", "Manage billing", "Cancel subscription",
    "Start free trial", "Start team", "Change password", "Leave team", "Transfer ownership", "Clear all data",
    "Danger zone", "Save all",
])
def test_danger_labels_are_always_hidden(label):
    assert matches(guard.DENY, label), label


@pytest.mark.parametrize("label", [
    "Sign in", "Close", "Cancel", "Search", "Pricing", "Next", "Continue", "Open settings", "Payments history",
    "Remove filter", "Delete",
])
def test_ordinary_controls_are_not_denied(label):
    assert not matches(guard.DENY, label), label


@pytest.mark.parametrize("label, hidden", [
    ("Save", True), ("Create project", True), ("Add task", True), ("Submit", True), ("Delete", True),
    ("Invite", True), ("Search", False), ("Next", False), ("Close", False), ("Cancel", False),
    ("Settings", False), ("New York office", False), ("New", True), ("+ New project", True), ("Settle up", False),
])
def test_read_only_hides_changing_controls(label, hidden):
    assert matches(guard.MUTATING, label) is hidden, label


def test_mic_controls_are_hidden_unless_a_transcript_is_supplied():
    assert matches(guard.build_config()["deny"], "Hold to talk")
    assert not matches(guard.build_config(speech="hello")["deny"], "Hold to talk")


def test_config_is_versioned_and_embedded_as_json():
    a, b = guard.build_config(mode="readonly"), guard.build_config(mode="mutate")
    assert a["v"] != b["v"]
    source = guard.script(a)
    assert "__QAJEV_CONFIG__" not in source
    assert json.dumps(a) in source
    with pytest.raises(ValueError):
        guard.build_config(mode="yolo")


@pytest.mark.parametrize("label, hidden", [
    ("Roll", False), ("Roll dice", False), ("Roll call", False), ("Rolls", False),
    ("Roll back", True), ("Rollback release", True), ("roll back deploy", True), ("Roll-back", True),
])
def test_only_a_rollback_is_denied_not_a_dice_roll(label, hidden):
    # run 20261007-035805-c494: backgammon's "Roll" was hidden as danger, and 4 scenarios went harness
    assert matches(guard.DENY, label) is hidden, label
