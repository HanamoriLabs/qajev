"""Offline attempts: no model/network/browser calls are made in this module."""

import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from qajev.ledger import CostCapReached, Ledger

TS = "https://api.typesafe.ai/v1/systemone"
TEXT = "https://openrouter.ai/api/v1/chat/completions"
BODY = {"model": "jev-latest"}
BOUND = 0.002753


def test_atomic_thread_reserve_rejects_second_inflight_attempt():
    entered, release = threading.Event(), threading.Event()
    ledger = Ledger(BOUND)

    def transport(*_):
        entered.set()
        assert release.wait(5)
        return {"usage": {"input_tokens": 100}}

    post = ledger.wrap(transport)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(post, TS, "fake", BODY.copy())
        assert entered.wait(5)
        try:
            with pytest.raises(CostCapReached):
                post(TS, "fake", BODY.copy())
        finally:
            release.set()
        first.result()


def test_atomic_shared_budget_counts_another_ledgers_inflight_reservation():
    shared = multiprocessing.get_context("spawn").Value("d", 0.0)
    first, second = Ledger(BOUND, shared=shared), Ledger(BOUND, shared=shared)
    attempt = first.before(TS, BODY.copy())
    with pytest.raises(CostCapReached):
        second.wrap(lambda *_: {})(TS, "fake", BODY.copy())
    first.after(TS, {"usage": {"input_tokens": 0}}, attempt)
    second.wrap(lambda *_: {"usage": {"input_tokens": 0}})(TS, "fake", BODY.copy())


def test_typesafe_token_usage_is_priced_instead_of_flat_estimate():
    ledger = Ledger(1)
    ledger.wrap(lambda *_: {"usage": {"input_tokens": 10_000}})(TS, "fake", BODY.copy())
    summary = ledger.summary()
    assert summary["usd"] == 0.00042
    assert summary["cost_basis"]["token_priced"] == 1


def test_missing_text_cost_is_unknown_and_retains_conservative_liability():
    ledger = Ledger(1, unknown_usd_per_attempt=0.1)
    ledger.wrap(lambda *_: {"usage": {"total_tokens": 10}})(TEXT, "fake", {"model": "unknown"})
    summary = ledger.summary()
    assert summary["usd"] == 0.1
    assert summary["unknown_usd"] == 0.1
    assert summary["cost_basis"]["unknown"] == 1
    assert summary["text_cost_reported"] is False


def test_reported_real_zero_is_distinct_from_missing_usage():
    ledger = Ledger(1, unknown_usd_per_attempt=0.1)
    ledger.wrap(lambda *_: {"usage": {"cost": 0}})(TEXT, "fake", {"model": "unknown"})
    assert ledger.summary()["usd"] == 0
    assert ledger.summary()["unknown_usd"] == 0
    assert ledger.summary()["cost_basis"]["reported"] == 1


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), True, "0"])
def test_invalid_reported_cost_cannot_become_free(bad):
    ledger = Ledger(1, unknown_usd_per_attempt=0.1)
    ledger.wrap(lambda *_: {"usage": {"cost": bad}})(TEXT, "fake", {"model": "unknown"})
    assert ledger.summary()["unknown_usd"] == 0.1


def test_unknown_route_fails_before_transport_without_explicit_bound():
    called = []
    with pytest.raises(CostCapReached, match="bound"):
        Ledger(1).wrap(lambda *_: called.append(1))(TEXT, "fake", {"model": "unknown"})
    assert not called


def test_possibly_billed_exception_never_releases_the_reservation():
    ledger = Ledger(BOUND)

    def fail(*_):
        raise RuntimeError("connection lost")

    with pytest.raises(RuntimeError):
        ledger.wrap(fail)(TS, "fake", BODY.copy())
    with pytest.raises(CostCapReached):
        ledger.wrap(lambda *_: {})(TS, "fake", BODY.copy())
    assert ledger.summary()["unknown_usd"] == BOUND


def test_settlement_is_idempotent_and_does_not_release_other_attempts():
    shared = multiprocessing.get_context("spawn").Value("d", 0.0)
    ledger = Ledger(2 * BOUND, shared=shared)
    first = ledger.before(TS, BODY.copy())
    second = ledger.before(TS, BODY.copy())
    for _ in range(2):
        ledger.after(TS, {"usage": {"input_tokens": 0}}, first)
    assert ledger.summary()["reserved_usd"] == BOUND
    assert ledger.spent() == BOUND
    assert ledger.summary()["cost_basis"]["token_priced"] == 1
    ledger.after(TS, {"usage": {"input_tokens": 0}}, second)
    assert ledger.summary()["usd"] == 0


def test_legacy_per_call_override_cannot_lower_documented_upper_bound():
    ledger = Ledger(BOUND - 0.000001, typesafe_usd_per_call=0)
    with pytest.raises(CostCapReached):
        ledger.wrap(lambda *_: {})(TS, "fake", BODY.copy())


def test_larger_per_call_override_is_labelled_operator_estimate():
    ledger = Ledger(1, typesafe_usd_per_call=0.1)
    ledger.before(TS, BODY.copy())
    assert ledger.summary()["reserved_usd"] == 0.1
    assert ledger.summary()["reservation_basis"]["operator_estimate"] == 1


def _process_attempt(shared, ready, go, results):
    ledger = Ledger(BOUND, shared=shared)
    ready.put(True)
    assert go.wait(10)
    try:
        ledger.before(TS, BODY.copy())
        results.put("reserved")
    except CostCapReached:
        results.put("refused")


def test_spawned_processes_cannot_both_reserve_the_last_allowance():
    ctx = multiprocessing.get_context("spawn")
    shared, ready, go, results = ctx.Value("d", 0.0), ctx.Queue(), ctx.Event(), ctx.Queue()
    processes = [ctx.Process(target=_process_attempt, args=(shared, ready, go, results)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        for _ in processes:
            assert ready.get(timeout=10)
        go.set()
        assert sorted(results.get(timeout=10) for _ in processes) == ["refused", "reserved"]
        assert shared.value == BOUND
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()


def test_transport_retries_have_separate_reservations_and_stop_before_unaffordable_retry():
    from types import SimpleNamespace

    from qajev import providers

    calls = []
    client = SimpleNamespace(post=lambda *a, **kw: calls.append((a, kw)) or
                             SimpleNamespace(status_code=503, is_error=True))
    ledger = Ledger(2 * BOUND)
    with pytest.raises(CostCapReached):
        providers.metered(client, ledger, sleep=lambda _: None)(TS, "fake", BODY.copy())
    assert len(calls) == 2
    assert ledger.summary()["errors"] == 2
    assert ledger.summary()["unknown_usd"] == 2 * BOUND


def test_clef_502_never_replays_an_uncertain_response():
    from types import SimpleNamespace

    from qajev import providers

    calls = []

    def http(*_, **kw):
        calls.append(kw)
        if len(calls) == 1:
            return SimpleNamespace(status_code=502, is_error=True)
        return SimpleNamespace(is_error=False, json=lambda: {"result": {"answers": {}}})

    ledger = Ledger(1)
    env = {"CLOUDFLARE_ACCOUNT_ID": "fake", "CLOUDFLARE_API_TOKEN": "fake", "QAJEV_JEV_PROVIDER": "cloudflare"}
    post = providers.route(providers.metered(SimpleNamespace(post=http), ledger), providers.resolve(env), env)
    with pytest.raises(RuntimeError, match="HTTP 502"):
        post(TS, "fake", {"questions": {"q": {"type": "choice", "criteria": {"a": "A", "b": "B"}}}})
    assert len(calls) == 1
    assert ledger.summary()["unknown_usd"] == 0.000934


@pytest.mark.parametrize("failure", ["timeout", "invalid_json"])
def test_uncertain_transport_errors_are_distinct_and_never_replayed(failure):
    from types import SimpleNamespace

    import httpx

    from qajev import providers

    calls = []

    def invalid_json():
        raise ValueError("invalid JSON")

    def http(*_, **__):
        calls.append(True)
        if failure == "timeout":
            raise httpx.ReadTimeout("upstream timeout")
        return SimpleNamespace(is_error=False, json=invalid_json)

    ledger = Ledger(1)
    with pytest.raises(providers.ModelAttemptUncertain) as error:
        providers.metered(SimpleNamespace(post=http), ledger, sleep=lambda _: None)(TS, "fake", BODY.copy())
    assert error.value.cost_uncertain is True
    assert calls == [True]
    assert ledger.summary()["unknown_usd"] == BOUND


def test_local_certain_choice_bypasses_transport_and_cost():
    from qajev import providers

    env = {"CLOUDFLARE_ACCOUNT_ID": "fake", "CLOUDFLARE_API_TOKEN": "fake", "QAJEV_JEV_PROVIDER": "cloudflare"}
    ledger = Ledger(0)
    post = providers.route(ledger.wrap(lambda *_: pytest.fail("local answer must not call provider")),
                           providers.resolve(env), env)
    result = post(TS, "fake", {"questions": {"q": {"type": "choice", "criteria": {"a": "A"}}}})
    assert result["usage"]["cost_usd"] == 0
    assert ledger.summary()["usd"] == 0


@pytest.mark.parametrize("name,expected", [("clef", 0.015729), ("clef-flash", 0.000934),
                                          ("clef-omni", 0.0096)])
def test_cloudflare_documented_context_reservations(name, expected):
    url = "https://api.cloudflare.com/client/v4/accounts/fake/ai/run/@cf/cloudflare/" + name
    ledger = Ledger(1)
    ledger.before(url, {"model": name})
    assert ledger.summary()["reserved_usd"] == expected


def test_gemma_output_is_bounded_and_both_sides_are_priced():
    url = "https://api.cloudflare.com/client/v4/accounts/fake/ai/run/@cf/google/gemma-4-26b-a4b-it"
    body = {"messages": []}
    ledger = Ledger(1)
    attempt = ledger.before(url, body)
    assert body["max_tokens"] == 16384
    assert ledger.summary()["reserved_usd"] == 0.030516
    ledger.after(url, {"usage": {"prompt_tokens": 10000, "completion_tokens": 1000}}, attempt)
    assert ledger.summary()["estimated_usd"] == 0.0013


def test_report_merge_preserves_liability_basis_across_workers():
    from qajev import report

    ledger = Ledger(1)
    ledger.wrap(lambda *_: {})(TS, "fake", BODY.copy())
    merged = report.merge_ledgers([ledger.summary(), Ledger(1).summary()])
    assert merged["unknown_usd"] == BOUND
    assert merged["cost_basis"]["unknown"] == 1


def test_cloud_adapter_bounds_apply_only_to_exact_authenticated_final_url():
    url = "https://cloud.example.test/api/v1/jobs/r_fake123/decide"
    ledger = Ledger(1)
    post = ledger.wrap(lambda *_: {}, verified_bounds={url: 15729})
    post(url, "fake", {"model": "client-cannot-select-price"})
    assert ledger.summary()["unknown_usd"] == 0.015729
    assert ledger.summary()["reservation_basis"]["cloud_enforced"] == 1
    for other in (url + "?redirect=1", url.replace("r_fake123", "r_other456"), url.replace("cloud.", "evil.")):
        with pytest.raises(CostCapReached, match="bound"):
            post(other, "fake", {"model": "jev-latest"})


def test_cloud_adapter_cannot_lower_native_documented_bound():
    with pytest.raises(CostCapReached):
        Ledger(BOUND - 0.000001).wrap(lambda *_: {}, verified_bounds={TS: 1})(TS, "fake", BODY.copy())


def test_cached_session_passes_authenticated_bounds_to_real_attempt_transport(monkeypatch):
    from types import SimpleNamespace

    from qajev import session

    url = "https://cloud.example.test/api/v1/jobs/r_fake123/decide"
    calls = []
    client = SimpleNamespace(post=lambda *a, **kw: calls.append(a[0]) or SimpleNamespace(
        is_error=False, json=lambda: {"usage": {"cost_usd": 0, "cost_basis": "reported", "cost_uncertain": False}}))
    model = SimpleNamespace(CLIENT=client)
    resolved = {"jev": "qajev-cloud", "verified_model_bounds": {url: 15729}}
    monkeypatch.setattr(session, "_jev", SimpleNamespace(model=model, providers=resolved))
    ledger = Ledger(1)
    session.load(ledger).model.post_json(url, "fake", {})
    assert calls == [url]
    assert ledger.summary()["cost_basis"]["reported"] == 1


@pytest.mark.parametrize("bad", [-1, True, "10", float("nan"), float("inf"), 1.5])
def test_invalid_token_usage_retains_unknown_liability(bad):
    ledger = Ledger(1)
    ledger.wrap(lambda *_: {"usage": {"input_tokens": bad}})(TS, "fake", BODY.copy())
    assert ledger.summary()["unknown_usd"] == BOUND


def test_provider_cost_exceeding_documented_bound_blocks_further_attempts():
    ledger = Ledger(1)
    ledger.wrap(lambda *_: {"usage": {"cost": 0.1}})(TS, "fake", BODY.copy())
    assert ledger.summary()["bound_violated"]
    with pytest.raises(CostCapReached):
        ledger.wrap(lambda *_: pytest.fail("bound failure must stop"))(TS, "fake", BODY.copy())


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_cli_invalid_budget_is_plain_error_before_network_or_browser(value, monkeypatch, capsys):
    from qajev import cli

    monkeypatch.setattr(cli, "_route", lambda _: pytest.fail("invalid cap reached Cloud"))
    assert cli.main(["check", "https://example.test", "--cost-cap", value, "--json"]) == 3
    assert "finite, non-negative" in capsys.readouterr().out


def test_cost_basis_report_does_not_describe_unknown_liability_as_free():
    from qajev import report, report_html

    ledger = Ledger(1)
    ledger.wrap(lambda *_: {})(TS, "fake", BODY.copy())
    data = {"cost": report.merge_ledgers([ledger.summary()])}
    assert "$0.002753 unknown liability" in report_html.cost_basis_note(data)
    assert "not confirmed spend" in report_html.cost_basis_note(data)
    assert "estimated per call" not in report_html.decisions_cost(data)


@pytest.mark.parametrize("basis,uncertain,cost,expected", [
    ("reported", False, 0, "reported"),
    ("token_priced", False, 0.001, "token_priced"),
    ("local_free", False, 0, "local_free"),
    ("local_free", False, 0.1, "unknown"),
    ("conservative_unknown", True, 0.000934, "unknown"),
    ("reported", True, 0.000934, "unknown"),
    (None, False, 0.000934, "unknown"),
])
def test_cloud_normalized_cost_basis_preserves_uncertainty(basis, uncertain, cost, expected):
    url = "https://cloud.example.test/api/v1/jobs/r_fake123/decide"
    ledger = Ledger(1)
    ledger.wrap(lambda *_: {"usage": {"cost_usd": cost, "cost_basis": basis, "cost_uncertain": uncertain}},
                verified_bounds={url: 15729})(url, "fake", {})
    summary = ledger.summary()
    assert summary["cost_basis"][expected] == 1
    if expected == "unknown":
        assert summary["unknown_usd"] == 0.015729
        assert summary["actual_usd"] == 0
    if expected == "token_priced":
        assert summary["estimated_usd"] == 0.001 and summary["actual_usd"] == 0


def test_runner_report_keeps_liability_when_worker_dies_before_summary(monkeypatch, tmp_path):
    import concurrent.futures

    from qajev import runner, suite

    class LostWorkers:
        def __init__(self, *, initargs, **_):
            self.shared = initargs[0]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def submit(self, *_):
            with self.shared.get_lock():
                self.shared.value = round(self.shared.value + BOUND, 6)
            future = concurrent.futures.Future()
            future.set_exception(RuntimeError("worker exited after paid attempt"))
            return future

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", LostWorkers)
    monkeypatch.setattr(runner.chrome, "reap", lambda: [])
    monkeypatch.setattr(runner.chrome, "version", lambda _: {"Browser": "fake"})
    monkeypatch.setattr(runner.chrome, "start", lambda *_: pytest.fail("offline test must not start Chrome"))
    monkeypatch.setattr(runner.providers, "resolve", lambda: {})
    monkeypatch.setattr(runner.providers, "describe", lambda _: {"jev": "none", "text": "none", "decider": "Jev"})
    parsed = suite.parse({"name": "lost workers", "base_url": "https://example.test", "devices": ["desktop"],
                          "scenarios": [{"name": "a", "url": "/a", "expect": {"text": "A"}},
                                        {"name": "b", "url": "/b", "expect": {"text": "B"}}]})
    result = runner.run(parsed, runner.Options(cdp_url="http://127.0.0.1:9999", out_dir=tmp_path, jobs=2))
    assert result["counts"]["harness"] == 2
    assert result["cost"]["usd"] == 2 * BOUND
    assert result["cost"]["unknown_usd"] == 2 * BOUND
    assert result["cost"]["unattributed_worker_usd"] == 2 * BOUND
