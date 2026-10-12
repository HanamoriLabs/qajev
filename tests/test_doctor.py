"""`qajev doctor` on a fresh install (the 11 Oct new-user test, findings 2-4): a machine with no key yet is not a
failure, it gets a plain next step; an optional key that is not set gets a neutral mark, not "ok"; and no internal role
names."""

import json

import pytest

from qajev import chrome, cli, cloud

KEYS = ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "TEXT_MODEL_API_KEY",
        "TEXT_MODEL_BASE_URL", "QAJEV_JEV_PROVIDER", "QAJEV_ENV_FILE", "QAJEV_PLAN_APPROVER")


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    """A fresh install: no env file, no key in the shell, Chrome found, the cloud not set up."""
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(f"QAJEV_{key}", raising=False)
    monkeypatch.setattr("qajev.config.HOME", tmp_path / "home")
    monkeypatch.setattr(chrome, "binary", lambda: "/Applications/Google Chrome.app")
    monkeypatch.setattr(chrome, "free_port", lambda: 9350)
    monkeypatch.setattr(chrome, "reap", lambda: [])
    monkeypatch.setattr(chrome, "status", lambda: [])
    monkeypatch.setattr(cloud, "doctor", lambda env_file=None, offline=False: (True, "no cloud key: runs stay here"))


def test_a_fresh_install_is_not_a_failure_and_says_what_to_add_next(fresh, capsys):
    assert cli.main(["doctor", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "NO " not in out
    assert "Next:" in out and "~/.qajev/.env" in out and "TYPESAFE_API_KEY or OPENROUTER_API_KEY" in out
    assert "qajev mode cloud" in out


def test_an_optional_key_that_is_not_set_has_a_neutral_mark(fresh, capsys):
    cli.main(["doctor", "--offline"])
    lines = capsys.readouterr().out.splitlines()
    key = next(line for line in lines if " TYPESAFE_API_KEY " in f" {line} ")
    assert key.startswith("-- ") and "not set" in key and not key.startswith("ok ")


def test_doctor_names_no_internal_role(fresh, capsys, monkeypatch):
    monkeypatch.setenv("QAJEV_PLAN_APPROVER", "the Orchestrator")
    cli.main(["doctor", "--offline"])
    out = capsys.readouterr().out
    assert "Orchestrator" not in out and "approver" not in out
    assert "approved test plan" in out


def test_the_json_says_ready_and_the_next_steps_and_keeps_ok(fresh, capsys):
    assert cli.main(["doctor", "--offline", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["ready"] is False and data["next"]
    levels = {c["check"]: c["level"] for c in data["checks"]}
    assert levels["TYPESAFE_API_KEY"] == "info" and levels["jev route"] == "todo" and levels["chrome"] == "ok"


def test_a_real_problem_still_fails(fresh, capsys, monkeypatch):
    def missing():
        raise chrome.ChromeError("no Chrome found")

    monkeypatch.setattr(chrome, "binary", missing)
    assert cli.main(["doctor", "--offline"]) == cli.EXIT_CONFIG
    assert any(line.startswith("NO ") and "chrome" in line for line in capsys.readouterr().out.splitlines())


def test_a_key_from_the_shell_is_ready_without_an_env_file(fresh, capsys, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts_test_key_value")
    assert cli.main(["doctor", "--offline", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ready"] is True and data["next"] == []
    assert {c["check"]: c["level"] for c in data["checks"]}["env files"] == "info"
