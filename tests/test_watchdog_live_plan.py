"""CPU-only proof-tool preparation: never starts a browser, worker, or model."""

import base64
import hashlib
import importlib.util
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from qajev import chrome


def _tool():
    path = Path(__file__).resolve().parents[1] / "tools" / "prove-watchdog-live.py"
    spec = importlib.util.spec_from_file_location("watchdog_live_proof", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prepare_accepts_described_three_scenario_plan_without_starting_resources(tmp_path, monkeypatch):
    tool = _tool()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("CPU preparation must not start a process or browser")

    monkeypatch.setattr(tool.subprocess, "Popen", forbidden)
    monkeypatch.setattr(chrome, "start", forbidden)
    monkeypatch.delenv("QAJEV_REQUIRE_PLAN", raising=False)
    monkeypatch.delenv("QAJEV_REAL_DEVICES", raising=False)
    receipt = tool.prepare(tmp_path, "http://127.0.0.1:8877")
    assert receipt["plan_exit"] == 0
    assert receipt["scenario_names"] == tool.NAMES
    assert receipt["subprocesses_started"] == 0 and receipt["browser_started"] is False
    assert receipt["plan"]["not_described"] == []
    env = tmp_path / "empty.env"
    assert env.read_bytes() == b""
    assert stat.S_ISREG(env.lstat().st_mode) and stat.S_IMODE(env.stat().st_mode) == 0o600
    data = json.loads((tmp_path / "suite.json").read_text())
    assert data["devices"] == ["desktop"]
    assert data["settle"] == 0
    assert "HOME" in tool.SAFE_ENV
    assert all("goal" not in row for row in data["scenarios"])
    assert data["scenarios"][2]["url"] == "/fresh"
    command = data["scenarios"][1]["before"][0]["command"]
    assert '"$$"' in command and command.endswith("; exec sleep 120")


def test_prepare_refuses_existing_env_instead_of_overwriting(tmp_path, monkeypatch):
    tool = _tool()
    empty = tmp_path / "empty.env"
    empty.write_text("retained")
    with pytest.raises(FileExistsError):
        tool.prepare(tmp_path, "http://127.0.0.1:8877")
    assert empty.read_text() == "retained"


@pytest.mark.parametrize("fault", [None, "shadow", "record", "editable"])
def test_module_attestation_checks_installed_path_and_record(tmp_path, monkeypatch, fault):
    tool = _tool()
    module_file = tmp_path / "fixture_module.py"
    content = b"fixture = True\n"
    module_file.write_bytes(content)
    expected = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")

    class Entry:
        hash = SimpleNamespace(mode="sha256", value="changed" if fault == "record" else expected)

        def __str__(self):
            return "fixture_module.py"

    dist = SimpleNamespace(
        files=[Entry()], version="fixture", locate_file=lambda _entry: module_file,
        read_text=lambda _name: json.dumps({"dir_info": {"editable": True}}) if fault == "editable" else None,
    )
    module = SimpleNamespace(__file__=str(tmp_path / "shadow.py" if fault == "shadow" else module_file))
    monkeypatch.setattr(tool.importlib.metadata, "distribution", lambda _name: dist)
    monkeypatch.setattr(tool.importlib, "import_module", lambda _name: module)
    if fault:
        with pytest.raises(RuntimeError):
            tool.attest_module("fixture_module", "fixture-dist")
    else:
        receipt = tool.attest_module("fixture_module", "fixture-dist")
        assert receipt["sha256"] == hashlib.sha256(content).hexdigest()
        assert receipt["record_hash"]["verified"] is True
        assert receipt["imported_path"] == receipt["distribution_path"] == str(module_file)
