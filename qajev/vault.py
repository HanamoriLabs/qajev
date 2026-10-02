"""Test-account passwords by reference: the macOS Keychain (the secret service on Linux), 1Password, or an environment
variable. A suite or project says where a password lives, never the password itself. QAJev reads it at sign-in and
types it into the password field itself: Jev, the reports and the logs never see it (a value read here is redacted
from everything QAJev writes).

    keychain:SERVICE/ACCOUNT   macOS: security find-generic-password; Linux: secret-tool lookup
    op://VAULT/ITEM/FIELD      1Password: op read (the 1Password CLI, signed in)
    env:NAME                   an environment variable (CI secrets)
"""

import os
import re
import shutil
import subprocess
import sys

from . import config

REF_HELP = '"keychain:SERVICE/ACCOUNT", "op://VAULT/ITEM/FIELD" or "env:NAME"'


class VaultError(ValueError):
    """A reference is malformed, or its store could not give the value. Messages never contain the value."""


def parse(ref):
    """-> ("keychain", service, account) | ("op", ref) | ("env", name)."""
    if not isinstance(ref, str):
        raise VaultError(f"a secret reference must be text: {REF_HELP}")
    if ref.startswith("keychain:"):
        service, _, account = ref.removeprefix("keychain:").partition("/")
        if not service.strip():
            raise VaultError(f"{ref!r}: keychain references are keychain:SERVICE/ACCOUNT")
        return ("keychain", service, account)
    if ref.startswith("op://"):
        if len([p for p in ref.removeprefix("op://").split("/") if p]) < 3:
            raise VaultError(f"{ref!r}: 1Password references are op://VAULT/ITEM/FIELD")
        return ("op", ref)
    if ref.startswith("env:"):
        name = ref.removeprefix("env:")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise VaultError(f"{ref!r}: env references are env:NAME")
        return ("env", name)
    raise VaultError(f"not a secret reference (use {REF_HELP}, never the value itself)")


def is_ref(value):
    try:
        parse(value)
    except VaultError:
        return False
    return True


def _run(cmd, ref, **kw):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)
    except FileNotFoundError:
        raise VaultError(f"{ref}: {cmd[0]} is not installed") from None
    except subprocess.TimeoutExpired:
        raise VaultError(f"{ref}: {cmd[0]} timed out (waiting for an unlock or a Touch ID prompt?)") from None
    if p.returncode != 0:
        why = ((p.stderr or "").strip().splitlines() or ["no output"])[-1][:200]
        raise VaultError(f"{ref}: {cmd[0]} failed: {why}")
    return p.stdout


def _keychain_cmd(service, account, *, store=False):
    if sys.platform == "darwin":
        who = ["-a", account] if account else []
        if store:  # -w last and empty: security prompts for the value itself, so it never sits in argv
            return ["security", "add-generic-password", "-U", "-l", f"QAJev: {service}/{account}", "-s", service,
                    *who, "-w"]
        return ["security", "find-generic-password", "-s", service, *who, "-w"]
    if shutil.which("secret-tool"):
        who = ["account", account] if account else []
        if store:  # reads the value from the terminal
            return ["secret-tool", "store", f"--label=QAJev: {service}/{account}", "service", service, *who]
        return ["secret-tool", "lookup", "service", service, *who]
    raise VaultError("keychain: references need the macOS Keychain, or secret-tool (libsecret) on Linux")


def resolve(ref):
    """The value behind a reference, remembered for redaction. Raises VaultError (never echoing the value)."""
    kind, *rest = parse(ref)
    if kind == "env":
        value = os.environ.get(rest[0], "")
        if not value:
            raise VaultError(f"{ref}: ${rest[0]} is not set")
    elif kind == "keychain":
        value = _run(_keychain_cmd(*rest), ref).removesuffix("\n")
    else:
        value = _run(["op", "read", "--no-newline", rest[0]], ref)
    if not value:
        raise VaultError(f"{ref}: the stored value is empty")
    config.remember_secret(value)
    return value


def store(ref):
    """Save a keychain: password, typed at the store's own prompt (QAJev never holds it). 1Password items are made
    in 1Password; env values live wherever the environment comes from."""
    kind, *rest = parse(ref)
    if kind != "keychain":
        raise VaultError(f"{ref}: QAJev stores keychain: references only; make 1Password items in 1Password and "
                         "set env: values in your environment or CI secrets")
    cmd = _keychain_cmd(*rest, store=True)
    try:
        code = subprocess.run(cmd, timeout=300).returncode  # the terminal, so the store can prompt
    except FileNotFoundError:
        raise VaultError(f"{ref}: {cmd[0]} is not installed") from None
    if code != 0:
        raise VaultError(f"{ref}: {cmd[0]} exited {code}")
