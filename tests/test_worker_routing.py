"""Real spawn workers preserve a trusted provider route; no browser or paid request."""

import base64
import functools
import multiprocessing
import os

import pytest

from qajev import chrome, config, providers, runner, suite
from qajev.ledger import Ledger

_barrier = None


def _resolved(env=None):
    return {"jev": "fixture", "jev_model": "fixture", "jev_key_name": "QAJEV_TEST_MODEL_TOKEN",
            "jev_url": "https://provider.invalid/decide", "text": False}


def _route(post, resolved, env=None):
    return lambda url, key, body: post(resolved["jev_url"], os.environ["QAJEV_TEST_MODEL_TOKEN"], body)


def _bootstrap(barrier=None):
    global _barrier
    _barrier = barrier
    providers.resolve = _resolved
    providers.route = _route


def _probe(payload):
    scenarios, _, _, _, _, cap, per_call = payload
    if _barrier is not None:
        _barrier.wait(timeout=30)

    def post(url, key, body):
        return {"url": url, "key_present": bool(key), "key_length": len(key), "pid": os.getpid()}

    metadata = providers.route(post, providers.resolve())(
        providers.TYPESAFE_URL, os.environ.get("TYPESAFE_API_KEY", ""), {})
    return {"ledger": Ledger(cap, per_call, shared=runner._shared_cost).summary(), "results": [
        {"name": s.name, "url": s.url, "outcome": "pass", "reason": "fixture", "checks": [],
         "findings": [], "screens": [], "seconds": 0, "routing": metadata} for s in scenarios]}


@pytest.mark.parametrize("jobs", [1, 4])
def test_each_worker_uses_the_parent_provider_route(monkeypatch, tmp_path, jobs):
    for key in list(os.environ):
        if config.SECRET_NAME.search(key):
            monkeypatch.delenv(key, raising=False)
    # RFC 4648 input bytes: synthetic, never a real credential. Only presence/length leave the fake provider.
    monkeypatch.setenv("QAJEV_TEST_MODEL_TOKEN", base64.b64encode(bytes(range(32))).decode())
    monkeypatch.setenv("TYPESAFE_API_KEY", config.ROUTING_PLACEHOLDER)
    bootstrap = functools.partial(_bootstrap, multiprocessing.get_context("spawn").Barrier(jobs) if jobs > 1 else None)
    monkeypatch.setattr(providers, "WORKER_BOOTSTRAP", bootstrap, raising=False)
    monkeypatch.setattr(providers, "resolve", _resolved)
    monkeypatch.setattr(providers, "route", _route)
    monkeypatch.setattr(runner, "_worker", _probe)
    monkeypatch.setattr(runner, "run_group", lambda scenarios, **kw: _probe(
        (scenarios, None, None, None, None, kw["ledger"].cap_usd, 0)))
    monkeypatch.setattr(chrome, "reap", lambda: [])
    monkeypatch.setattr(chrome, "version", lambda _: {"Browser": "fixture"})
    parsed = suite.parse({"name": "routing", "base_url": "http://localhost", "scenarios": [
        {"name": f"check-{i}", "url": "/", "about": "Fixture routing", "expect": {"text": "fixture"}}
        for i in range(4)]})
    built = runner.run(parsed, runner.Options(jobs=jobs, cdp_url="http://127.0.0.1:9", out_dir=tmp_path))
    assert len(built["scenarios"]) == 4
    observations = [row["routing"] for row in built["scenarios"]]
    assert all(row["url"] == "https://provider.invalid/decide" and row["key_present"]
               and row["key_length"] == 44 for row in observations), observations
    assert len({row["pid"] for row in observations}) == jobs
    assert built["cost"]["usd"] == 0


def test_default_worker_bootstrap_keeps_the_shared_cost(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(runner, "_shared_cost", None)
    runner._init_worker(sentinel)
    assert runner._shared_cost is sentinel


def test_worker_bootstrap_failure_is_not_ignored():
    def refused():
        raise RuntimeError("fixture bootstrap refused")

    with pytest.raises(RuntimeError, match="fixture bootstrap refused"):
        runner._init_worker(None, refused)
