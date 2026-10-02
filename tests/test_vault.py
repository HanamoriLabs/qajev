import subprocess
import sys
from types import SimpleNamespace

import pytest

from qajev import config, vault


def test_references_name_a_store_never_a_value():
    assert vault.parse("keychain:qajev/shop-tester") == ("keychain", "qajev", "shop-tester")
    assert vault.parse("keychain:qajev") == ("keychain", "qajev", "")
    assert vault.parse("op://QA/Shop tester/password") == ("op", "op://QA/Shop tester/password")
    assert vault.parse("env:SHOP_PASS") == ("env", "SHOP_PASS")
    for bad in ("hunter2", "keychain:", "op://QA/item", "env:not a name", 42):
        with pytest.raises(vault.VaultError):
            vault.parse(bad)
    with pytest.raises(vault.VaultError, match="never the value"):
        vault.parse("correct horse battery staple")


def test_each_store_is_read_with_its_own_tool_and_the_value_is_redacted(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout={"security": "kc-pass-1\n", "secret-tool": "kc-pass-1\n",
                                                     "op": "op-pass-22"}[cmd[0]], stderr="")

    monkeypatch.setattr(vault.subprocess, "run", fake_run)
    monkeypatch.setattr(vault.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setenv("SHOP_PASS", "env-pass-333")
    assert vault.resolve("keychain:qajev/shop-tester") == "kc-pass-1"  # the trailing newline is not the password
    assert vault.resolve("op://QA/Shop tester/password") == "op-pass-22"
    assert vault.resolve("env:SHOP_PASS") == "env-pass-333"
    if sys.platform == "darwin":
        assert calls[0] == ["security", "find-generic-password", "-s", "qajev", "-a", "shop-tester", "-w"]
    else:
        assert calls[0] == ["secret-tool", "lookup", "service", "qajev", "account", "shop-tester"]
    assert calls[1] == ["op", "read", "--no-newline", "op://QA/Shop tester/password"]
    text = config.redact("typed kc-pass-1 and op-pass-22")
    assert "kc-pass-1" not in text and "op-pass-22" not in text  # everything QAJev writes is redacted with these


def test_failures_say_what_is_wrong_without_the_value(monkeypatch):
    monkeypatch.delenv("NOPE_PASS", raising=False)
    with pytest.raises(vault.VaultError, match=r"\$NOPE_PASS is not set"):
        vault.resolve("env:NOPE_PASS")

    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(vault.subprocess, "run", missing)
    with pytest.raises(vault.VaultError, match="op is not installed"):
        vault.resolve("op://QA/item/password")

    def locked(cmd, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="[ERROR] you are not currently signed in\n")

    monkeypatch.setattr(vault.subprocess, "run", locked)
    with pytest.raises(vault.VaultError, match="not currently signed in"):
        vault.resolve("op://QA/item/password")

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 120)

    monkeypatch.setattr(vault.subprocess, "run", slow)
    with pytest.raises(vault.VaultError, match="Touch ID"):
        vault.resolve("op://QA/item/password")


def test_only_keychain_passwords_are_stored_and_the_store_prompts_for_them(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, kw=kw)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(vault.subprocess, "run", fake_run)
    monkeypatch.setattr(vault.shutil, "which", lambda name: "/usr/bin/" + name)
    vault.store("keychain:qajev/shop-tester")
    assert "capture_output" not in seen["kw"]  # the terminal: the store's own prompt, not QAJev, takes the value
    if sys.platform == "darwin":
        assert seen["cmd"][-1] == "-w"  # last and empty: security prompts, so the value never sits in argv
    with pytest.raises(vault.VaultError, match="keychain: references only"):
        vault.store("op://QA/item/password")
