"""Actual child environments for local serial/spawn runs; synthetic credentials, no model calls."""

import base64
import functools
import json
import multiprocessing
import os
import shlex
import subprocess
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
    "all_proxy", "ALL_PROXY",
)
PROXY_NAMES = (
    "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "no_proxy", "NO_PROXY", "all_proxy", "ALL_PROXY",
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


@pytest.mark.parametrize("browser", ["shared", "fresh"])
@pytest.mark.parametrize("jobs", [1, 2])
@pytest.mark.parametrize("private_temp", [False, True])
def test_local_jobs_keep_credentials_out_of_the_launched_chrome(monkeypatch, tmp_path, launch_env,
                                                               jobs, private_temp, browser):
    # fresh (the default) starts each chain's Chrome in that chain: here a thread (chain_threads), the real code
    from chain_threads import start_chain

    monkeypatch.setattr(runner, "_start_chain", start_chain)
    monkeypatch.setitem(globals(), "_barrier", None)  # a chain in a thread sets it in this process: never left over
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
    parsed = suite.parse({"name": "environment", "base_url": "http://localhost", "browser": browser, "scenarios": [
        {"name": f"check-{i}", "url": "/", "about": "Fixture", "expect": {"text": "fixture"}} for i in range(2)]})
    try:
        result = runner.run(parsed, runner.Options(jobs=jobs, headless=True, ephemeral=True, out_dir=tmp_path / "runs"))
        assert len(result["scenarios"]) == 2
        if browser == "shared":  # one Chrome; with jobs 2, a worker process for each chain
            assert len({row["worker_pid"] for row in result["scenarios"]}) == jobs
        assert result["cost"]["usd"] == 0
        assert len(observations) == (1 if browser == "shared" else 2)  # fresh: one Chrome for each chain
        for record, observed in zip(records, observations, strict=True):
            child_names = set(observed["names"])
            assert not child_names.intersection(FORBIDDEN), sorted(child_names.intersection(FORBIDDEN))
            assert record.get("environment_isolated") is True
            assert {"PATH", "HOME"} <= child_names
            # a fresh Chrome always has its own temp folder: several can run side by side
            expected_temp = record["temp_dir"] if private_temp or browser == "fresh" else str(launch_env)
            assert set(observed["temp"].values()) == {expected_temp}
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
    for name in (*FORBIDDEN, *PROXY_NAMES):
        monkeypatch.setenv(name, "fixture-value")
    before = dict(os.environ)
    env = chrome.child_env(temporary=tmp_path / "owned-temp")
    assert all(env.get(name) == value for name, value in allowed.items())
    assert not set(env).intersection((*FORBIDDEN, *PROXY_NAMES))
    assert all(env.get(name) == str(tmp_path / "owned-temp") for name in ("TMPDIR", "TMP", "TEMP"))
    parent_unchanged = dict(os.environ) == before
    assert parent_unchanged, "Chrome environment construction changed the parent"


@pytest.mark.parametrize("marker", [None, False, "true"])
def test_legacy_browser_requires_restart_without_spawning_or_reading_its_environment(monkeypatch, marker):
    record = {"alive": True, "headless": True, "password_services_disabled": True,
              "background_services_disabled": True, "environment_isolated": marker}
    monkeypatch.setattr(chrome, "status", lambda _: [record])
    with pytest.raises(chrome.ChromeError, match="Restart.*environment"):
        chrome.start("fixture", headless=True)


@pytest.mark.parametrize("profile", ["fixture", "fixture's browser"])
def test_legacy_browser_names_a_shell_safe_stop_command(monkeypatch, profile):
    record = {"alive": True, "headless": True, "password_services_disabled": True,
              "background_services_disabled": True}
    monkeypatch.setattr(chrome, "status", lambda _: [record])
    with pytest.raises(chrome.ChromeError) as error:
        chrome.start(profile, headless=True)
    command = str(error.value).partition(": run ")[2]
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = []
    assert argv == ["qajev", "browser", "stop", f"--profile={profile}"]


@pytest.mark.parametrize("profile", ["-x", "fixture's browser", "a $(b)", "a `b`", "a $HOME"])
def test_restart_command_reaches_the_real_parser_without_shell_expansion(monkeypatch, tmp_path, profile):
    record = {"alive": True, "headless": True, "password_services_disabled": True,
              "background_services_disabled": True}
    monkeypatch.setattr(chrome, "status", lambda _: [record])
    with pytest.raises(chrome.ChromeError) as error:
        chrome.start(profile, headless=True)
    command = str(error.value).partition(": run ")[2]
    programs = tmp_path / "bin"
    programs.mkdir()
    arguments, expanded = tmp_path / "arguments.json", tmp_path / "expanded"
    script = tmp_path / "parser.py"
    script.write_text(
        "import json,os,sys\n"
        "from qajev.cli import build_parser\n"
        "args=build_parser().parse_args(sys.argv[1:])\n"
        "assert args.command == 'browser' and args.action == 'stop'\n"
        "with open(os.environ['FIXTURE_ARGUMENTS'], 'w') as f: json.dump({'profile':args.profile}, f)\n"
    )
    fake_qajev = programs / "qajev"
    fake_qajev.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n')
    fake_qajev.chmod(0o700)
    # A faulty quote can invoke only this inert, local substitution marker.
    substitution = programs / "b"
    substitution.write_text('#!/bin/sh\nprintf expanded > "$FIXTURE_EXPANDED"\nprintf replaced\n')
    substitution.chmod(0o700)
    env = {"PATH": str(programs), "HOME": str(tmp_path / "home"),
           "PYTHONPATH": str(Path(chrome.__file__).resolve().parent.parent),
           "QAJEV_HOME": str(tmp_path / "qajev-home"), "FIXTURE_ARGUMENTS": str(arguments),
           "FIXTURE_EXPANDED": str(expanded)}
    result = subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not expanded.exists(), "Restart guidance executed a shell substitution"
    assert json.loads(arguments.read_text())["profile"] == profile


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
