"""Malformed supplier usage must not make paid calls free or reopen a broken quote."""

import multiprocessing

import pytest

from qajev.ledger import CostCapReached, Ledger

TS = "https://api.typesafe.ai/v1/systemone"
BOUND = 0.002753


@pytest.mark.parametrize("usage", [
    {"cost": 0, "input_tokens": -1},
    {"cost": 0, "output_tokens": float("nan")},
    {"cost": 0, "prompt_tokens": True},
    {"cost": 0, "total_tokens": -1},
    {"cost": 0, "cost_usd": -1},
    {"cost_usd": 0, "total_cost": float("inf")},
    {"cost": 0, "input_tokens": 10, "prompt_tokens": "invalid"},
])
def test_malformed_supplied_usage_cannot_turn_a_reported_zero_into_free_spend(usage):
    ledger = Ledger(1)
    ledger.wrap(lambda *_: {"usage": usage})(TS, "fake", {})
    assert ledger.summary()["unknown_usd"] == BOUND
    assert ledger.summary()["actual_usd"] == 0


def test_clean_zero_remains_zero_and_valid_dollar_aliases_use_conservative_maximum():
    ledger = Ledger(1)
    ledger.wrap(lambda *_: {"usage": {"cost_usd": 0}})(TS, "fake", {})
    assert ledger.summary()["usd"] == 0
    ledger.wrap(lambda *_: {"usage": {"cost": 0, "cost_usd": 0.001}})(TS, "fake", {})
    assert ledger.summary()["actual_usd"] == 0.001


def _breach_in_child(shared, output):
    ledger = Ledger(1, shared=shared)
    ledger.wrap(lambda *_: {"usage": {"cost_usd": 0.1}})(TS, "fake", {})
    output.put(ledger.summary())


@pytest.mark.parametrize("paired", [False, True], ids=["bare_value", "paired_flag"])
def test_another_process_cannot_spend_after_a_quote_breach_even_after_an_inflight_refund(paired):
    ctx = multiprocessing.get_context("spawn")
    if paired:
        from qajev.ledger import SharedBudget

        shared = SharedBudget.create(ctx)
    else:
        shared = ctx.Value("d", 0.0)
    inflight = Ledger(1, shared=shared)
    attempt = inflight.before(TS, {})
    output = ctx.Queue()
    process = ctx.Process(target=_breach_in_child, args=(shared, output))
    process.start()
    try:
        breached = output.get(timeout=10)
        process.join(timeout=10)
        assert process.exitcode == 0
        assert breached["actual_usd"] == 0.1 and breached["bound_violated"]
        inflight.after(TS, {"usage": {"cost_usd": 0}}, attempt)
        calls = []
        with pytest.raises(CostCapReached):
            Ledger(1, shared=shared).wrap(lambda *_: calls.append(True))(TS, "fake", {})
        assert not calls
        if paired:
            assert inflight.spent() == 0.1  # a frozen bit is not a fictional provider charge
        else:
            assert breached["guard_hold_usd"] >= 0.9
            assert breached["reserved_usd"] >= 0.9
    finally:
        if process.is_alive():
            process.terminate()
            process.join()


def test_context_breach_cannot_be_hidden_by_an_invalid_companion_field_or_zero_dollars():
    ledger = Ledger(1)
    ledger.wrap(lambda *_: {"usage": {"input_tokens": 70000, "output_tokens": -1, "cost_usd": 0}})(TS, "fake", {})
    assert ledger.summary()["bound_violated"]
    assert ledger.summary()["unknown_usd"] == BOUND
    with pytest.raises(CostCapReached):
        ledger.wrap(lambda *_: pytest.fail("broken context bound must stop later requests"))(TS, "fake", {})
