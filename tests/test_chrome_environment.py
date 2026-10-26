"""Actual child environments for local serial/spawn runs; synthetic credentials, no model calls."""

import base64
import functools
import json
import multiprocessing
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from qajev import browser_pool, chrome, providers, runner, session, suite
from qajev.ledger import Ledger

FORBIDDEN = (
    "TYPESAFE_API_KEY", "QAJEV_TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "TEXT_MODEL_API_KEY",
    "CLOUDFLARE_API_TOKEN", "QAJEV_CLOUD_JOB_TOKEN", "AWS_SECRET_ACCESS_KEY", "fixture_password",
    "AUTHORIZATION", "COOKIE", "FIXTURE_OPAQUE_VALUE", "LC_FIXTURE_TOKEN", "XDG_FIXTURE_SECRET",
    "MESA_FIXTURE_KEY", "PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES",
)
_barrier = None


def _bootstrap(barrier):
    global _barrier
    _barrier = barrier


def _work(payload):
    scenarios, _, _, _, _, cap, per_call = payload
    if _barrier is not None:
        _barrier.wait(timeout=30)
    return {"ledger": Ledger(cap, per_call, shared=runner._shared_cost).summary(), "results": [
        {"name": s.name, "url": s.url, "outcome": "pass", "reason": "fixture", "checks": [],
         "findings": [], "screens": [], "seconds": 0, "worker_pid": os.getpid()} for s in scenarios]}


@pytest.fixture
def launch_env(monkeypatch, tmp_path):
    # RFC 4648 input bytes; no real credential is read or recorded.
    fake = base64.b64encode(bytes(range(32))).decode()
    for name in FORBIDDEN:
        if name not in {"PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES"}:
            monkeypatch.setenv(name, fake)
    parent_temp = tmp_path / "parent-temp"
    parent_temp.mkdir()
    for name in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(name, str(parent_temp))
    monkeypatch.setattr(chrome, "STATE", tmp_path / "state")
    monkeypatch.setattr(chrome, "PROFILES", tmp_path / "profiles")
    monkeypatch.setattr(chrome, "EPHEMERAL_ROOT", tmp_path / "ephemeral")
    fixture = Path(__file__).parent / "fixtures/chrome_environment.py"
    executable = tmp_path / "fixture-chrome"
    executable.write_text("#!" + sys.executable + "\n" + fixture.read_text())
    executable.chmod(0o700)
    monkeypatch.setenv("QAJEV_CHROME", str(executable))
    monkeypatch.setenv("QAJEV_RUNNER", "local")
    return parent_temp


@pytest.mark.parametrize("jobs", [1, 2])
@pytest.mark.parametrize("private_temp", [False, True])
def test_local_jobs_keep_credentials_out_of_the_launched_chrome(monkeypatch, tmp_path, launch_env,
                                                               jobs, private_temp):
    monkeypatch.setattr(browser_pool, "parallel", lambda: private_temp)
    monkeypatch.setattr(chrome, "reap", lambda: [])
    monkeypatch.setattr(runner, "_worker", _work)
    monkeypatch.setattr(runner, "run_group", lambda scenarios, **kw: _work(
        (scenarios, None, None, None, None, kw["ledger"].cap_usd, 0)))
    barrier = multiprocessing.get_context("spawn").Barrier(2) if jobs == 2 else None
    monkeypatch.setattr(providers, "WORKER_BOOTSTRAP", functools.partial(_bootstrap, barrier))
    records, observations = [], []
    original_start = chrome.start

    def start(*args, **kwargs):
        record = original_start(*args, **kwargs)
        records.append(record)
        observations.append(json.loads((Path(record["profile_dir"]) / "environment.json").read_text()))
        return record

    monkeypatch.setattr(chrome, "start", start)
    parsed = suite.parse({"name": "environment", "base_url": "http://localhost", "scenarios": [
        {"name": f"check-{i}", "url": "/", "about": "Fixture", "expect": {"text": "fixture"}} for i in range(2)]})
    try:
        result = runner.run(parsed, runner.Options(jobs=jobs, headless=True, ephemeral=True, out_dir=tmp_path / "runs"))
        assert len(result["scenarios"]) == 2
        assert len({row["worker_pid"] for row in result["scenarios"]}) == jobs
        assert result["cost"]["usd"] == 0
        assert len(observations) == 1
        child_names = set(observations[0]["names"])
        assert not child_names.intersection(FORBIDDEN), sorted(child_names.intersection(FORBIDDEN))
        assert records[0].get("environment_isolated") is True
        assert {"PATH", "HOME"} <= child_names
        expected_temp = records[0]["temp_dir"] if private_temp else str(launch_env)
        assert set(observations[0]["temp"].values()) == {expected_temp}
        assert all(os.environ.get(name) for name in FORBIDDEN[:14])
    finally:
        for record in records:
            chrome.stop(record["state_key"])


def test_runtime_allowlist_is_exact_and_does_not_mutate_the_parent(monkeypatch, tmp_path):
    allowed = {"PATH": "/fixture/bin", "HOME": str(tmp_path), "LANG": "en_GB.UTF-8", "LC_TIME": "C",
               "DISPLAY": ":91", "WAYLAND_DISPLAY": "wayland-9", "XAUTHORITY": "/fixture/xauth",
               "DBUS_SESSION_BUS_ADDRESS": "unix:path=/fixture/bus", "XDG_RUNTIME_DIR": "/fixture/runtime",
               "FONTCONFIG_PATH": "/fixture/fonts", "LIBGL_DRIVERS_PATH": "/fixture/drivers", "TZ": "UTC"}
    for name, value in allowed.items():
        monkeypatch.setenv(name, value)
    for name in FORBIDDEN:
        monkeypatch.setenv(name, "fixture-value")
    before = dict(os.environ)
    env = chrome.child_env(temporary=tmp_path / "owned-temp")
    assert all(env.get(name) == value for name, value in allowed.items())
    assert not set(env).intersection(FORBIDDEN)
    assert env["TMPDIR"] == env["TMP"] == env["TEMP"] == str(tmp_path / "owned-temp")
    assert dict(os.environ) == before


@pytest.mark.parametrize("marker", [None, False, "true"])
def test_legacy_browser_requires_restart_without_spawning_or_reading_its_environment(monkeypatch, marker):
    record = {"alive": True, "headless": True, "password_services_disabled": True,
              "background_services_disabled": True, "environment_isolated": marker}
    monkeypatch.setattr(chrome, "status", lambda _: [record])
    with pytest.raises(chrome.ChromeError, match="Restart.*environment"):
        chrome.start("fixture", headless=True)


def test_command_hooks_keep_their_existing_authorized_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(session, "_jev", None)
    monkeypatch.setenv("FIXTURE_HOOK_KEY", base64.b64encode(bytes(range(32))).decode())
    target = SimpleNamespace(allow_commands=False, cwd=tmp_path)
    code = "import os,json;print(json.dumps({'present':bool(os.environ.get('FIXTURE_HOOK_KEY'))}))"
    command = shlex.join([sys.executable, "-c", code])
    with pytest.raises(session.HookFailed, match="--allow-commands"):
        session.Session.command(target, command, "http://localhost")
    target.allow_commands = True
    result = session.Session.command(target, command, "http://localhost")
    assert result["exit"] == 0 and json.loads(result["stdout"]) == {"present": True}


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("QAJEV_LIVE") != "1", reason="explicit isolated Linux Chrome proof only")
def test_real_linux_chrome_starts_with_filtered_environment(monkeypatch, tmp_path):
    if sys.platform != "linux" or not Path("/usr/lib/chromium/chromium").is_file():
        pytest.skip("Linux Chromium fixture required")
    monkeypatch.setattr(chrome, "STATE", tmp_path / "state")
    monkeypatch.setattr(chrome, "EPHEMERAL_ROOT", tmp_path / "ephemeral")
    monkeypatch.setenv("QAJEV_CHROME", "/usr/lib/chromium/chromium")
    monkeypatch.setenv("QAJEV_CHROME_FLAGS", "--no-sandbox --disable-gpu")
    monkeypatch.setenv("FIXTURE_MODEL_KEY", base64.b64encode(bytes(range(32))).decode())
    record = chrome.start("environment", headless=True, ephemeral=True, port=0)
    try:
        raw = (Path("/proc") / str(record["pid"]) / "environ").read_bytes()
        names = {row.split(b"=", 1)[0] for row in raw.split(b"\0")}
        assert b"FIXTURE_MODEL_KEY" not in names
        assert chrome.version(record["cdp_url"])
    finally:
        chrome.stop(record["state_key"])
