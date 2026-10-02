"""`qajev account`: a stored test account set up in one step, by the person (an agent asks them to run it).

add: the password goes into the Keychain at its own prompt (or is checked where it already lives: 1Password, an
environment variable); the account is written into a project as references only (`[accounts.NAME]`), or printed as
a suite's `account:` block; then QAJev signs in once with it, to prove it works.
check: that sign-in alone.

Nothing here ever reads a password into QAJev's own output: vault.py resolves it only to type it, at sign-in.
"""

import json
import re
import sys
import tomllib
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

from . import vault

NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class AccountError(ValueError):
    pass


def default_ref(name):
    return f"keychain:qajev/{name}"


def save_password(ref, *, replace=False, interactive=None):
    """Make sure `ref` can be read: a keychain: reference is saved at the Keychain's own prompt unless it already
    holds one (or `replace`). -> "saved" | "kept" | "found"."""
    kind = vault.parse(ref)[0]
    readable = _readable(ref)
    if kind != "keychain":
        if not readable:
            vault.resolve(ref)  # raises with what is wrong (op not signed in, variable not set...)
        return "found"
    if readable and not replace:
        return "kept"
    if not (sys.stdin.isatty() if interactive is None else interactive):
        raise AccountError("the Keychain asks for the password in a terminal: run this in your own terminal "
                           "(in Claude Code, type it after a !)")
    vault.store(ref)
    vault.resolve(ref)  # prove it is there
    return "saved"


def _readable(ref):
    try:
        vault.resolve(ref)
        return True
    except vault.VaultError:
        return False


def block(name, email, password, login_url):
    """The suite `account:` block; validated as a suite would validate it."""
    from .suite import SuiteError, _account

    raw = {"name": name, "email": email, "password": password, "login": {"url": login_url}}
    try:
        _account(raw, None if urlsplit(login_url).scheme else "https://placeholder.invalid")
    except SuiteError as e:
        raise AccountError(str(e)) from None
    return raw


def add_to_project(path, name, email, password, login_url, *, default=False):
    """Append `[accounts.NAME]` (references only) to a project file; with `default`, every env signs in with it.
    The file is re-read through project.load and restored if that fails."""
    from . import project as project_mod

    text = path.read_text()
    data = tomllib.loads(text)
    if name in (data.get("accounts") or {}):
        raise AccountError(f"{path}: accounts.{name} already exists; edit it there, or pick another name")
    entry = (f"\n[accounts.{name}]\nemail = {json.dumps(email)}\npassword = {json.dumps(password)}\n"
             f"login = {{ url = {json.dumps(login_url)} }}\n")
    new = text.rstrip("\n") + "\n" + entry
    if default and not data.get("account"):
        # A top-level key must come before the first table: put it just above it.
        lines = new.splitlines(keepends=True)
        first = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
        lines.insert(first, f"account = {json.dumps(name)}\n")
        new = "".join(lines)
    path.write_text(new)
    try:
        return project_mod.load(str(path))
    except project_mod.ProjectError:
        path.write_text(text)
        raise


def project_account(project, name, env=None):
    """A project's account as a suite block with an absolute sign-in URL, and the hosts it may use."""
    from . import project as project_mod

    if name not in project.accounts:
        raise AccountError(f"{project.name} has no account {name!r}; have {sorted(project.accounts)}")
    acct = project_mod.sign_in_account(project, name)
    env_cfg = project.envs[env or project.default_env]
    url = urljoin(env_cfg["base_url"], acct["login"]["url"])
    hosts = {urlsplit(env_cfg["base_url"]).netloc, *env_cfg.get("hosts", []), urlsplit(url).netloc}
    return {**acct, "login": {**acct["login"], "url": url}}, hosts


def try_sign_in(account, hosts, *, headless=True, cdp_url=None):
    """Sign in once, in a throwaway QAJev Chrome (or `cdp_url`'s), the way a run does before its scenarios.
    -> what happened (no secrets)."""
    from . import chrome, runner
    from . import session as session_mod
    from .suite import _account

    account = _account(account, None)
    owned = None if cdp_url else chrome.start("default", headless=headless, ephemeral=True)
    if owned:
        cdp_url, headless = owned["cdp_url"], owned["headless"]
    try:
        suite = SimpleNamespace(account=account, hosts=set(hosts) | {urlsplit(account["login"]["url"]).netloc},
                                guard={})
        return runner.sign_in_first(suite, opts=runner.Options(), cdp_url=cdp_url, headless=headless,
                                    motion="reduce")
    finally:
        session_mod.stop_daemon()
        if owned:
            chrome.stop(owned["state_key"])
