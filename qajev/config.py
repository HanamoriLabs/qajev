"""Paths, environment and secret handling. Stdlib only: imported on every CLI start."""

import functools
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

HOME = Path(os.environ.get("QAJEV_HOME", Path.home() / ".qajev"))
SOURCE = Path(__file__).resolve().parent.parent  # the checkout an editable or source install runs from


def qajev_commit(root=None):
    """The commit this QAJev runs from, "+dirty" when its own code has changes no commit holds, or None (a wheel: no
    checkout). Every report carries it, so a verdict says which rules judged it (the audit of 6 Oct could not).
    root: another checkout (tests); QAJev's own is read once per process."""
    return _own_commit() if root is None else _commit(root)


@functools.cache
def _own_commit():
    return _commit(SOURCE)


def _commit(root):
    if not (Path(root) / ".git").exists():  # a worktree's .git is a file
        return None
    git = ["git", "-C", str(root)]
    try:
        head = subprocess.run([*git, "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True, timeout=5)
        dirty = subprocess.run([*git, "status", "--porcelain", "--untracked-files=no", "--", "qajev"],
                               capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if head.returncode != 0 or not head.stdout.strip():
        return None
    return head.stdout.strip() + ("+dirty" if dirty.stdout.strip() else "")

# Keys QAJev needs. Values are never printed; `doctor` reports presence only.
# Goals need a Jev route (TYPESAFE_API_KEY, or an OpenRouter key); see providers.py.
DEFAULTS = {
    "TYPESAFE_MODEL": "jev-latest",
    "TEXT_MODEL_BASE_URL": "https://openrouter.ai/api/v1",
    "TEXT_MODEL": "inception/mercury-2.5",
    "TEXT_MODEL_REASONING": "none",
}
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASS\b|CREDENTIAL", re.I)
ROUTING_PLACEHOLDER = "routed-to-openrouter"
LOOPBACK = re.compile(r"^(localhost|127(\.\d+){3}|::1|\[::1\]|[^.]+\.localhost)$", re.I)


# QAJev's own model and service settings (providers.py). They come from QAJev's env files, over the shell: a project's
# shell or .env often holds a Cloudflare or OpenRouter token for something else (Foley1, 6 Oct: Clef got Foley's
# Worker deploy token and answered 401). `QAJEV_<NAME>` in the shell pins one on purpose; a key no file sets keeps
# the shell's value (CI secrets).
OWN_KEYS = ("TYPESAFE_API_KEY", "TYPESAFE_MODEL", "OPENROUTER_API_KEY", "TEXT_MODEL_API_KEY", "TEXT_MODEL_BASE_URL",
            "TEXT_MODEL", "TEXT_MODEL_REASONING", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID")
_SOURCES: dict[str, str] = {}  # own key -> where its value came from (a file, QAJEV_<NAME>, the shell): names only
_SHELL_SET_ASIDE: list[str] = []  # own keys the shell set, not used


def env_files(explicit=None):
    """QAJev's own env files: one named on purpose (--env-file, QAJEV_ENV_FILE), then ~/.qajev/.env. Never the
    current folder's .env: a project's file can hold production secrets that are not QAJev's to load."""
    candidates = [explicit, os.environ.get("QAJEV_ENV_FILE"), HOME / ".env"]
    return [Path(p).expanduser() for p in candidates if p]


def _read(path):
    out = {}
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out.setdefault(key.strip().removeprefix("export ").strip(), value.strip().strip('"').strip("'"))
    return out


def load_env(explicit=None):
    """First file wins per key. Other settings: the shell wins over files. QAJev's own keys (OWN_KEYS): its files win
    over the shell, and `QAJEV_<NAME>` wins over both."""
    # A value QAJev itself filled in for jev (see providers.PLACEHOLDER) is not a real setting: a child
    # process that inherits it must still pick up the real key from its env file.
    for key, value in list(os.environ.items()):
        if value == ROUTING_PLACEHOLDER:
            del os.environ[key]
    shell = {k: os.environ[k] for k in OWN_KEYS if k in os.environ}
    loaded, own = [], {}
    for path in env_files(explicit):
        if not path.is_file():
            if explicit and path == Path(explicit).expanduser():
                raise FileNotFoundError(f"env file not found: {path}")
            continue
        for key, value in _read(path).items():
            if key in OWN_KEYS:
                own.setdefault(key, (value, str(path)))
            else:
                os.environ.setdefault(key, value)
        loaded.append(str(path))
    _SOURCES.clear()
    _SHELL_SET_ASIDE.clear()
    for key in OWN_KEYS:
        pinned = os.environ.get(f"QAJEV_{key}")
        if pinned:
            os.environ[key], _SOURCES[key] = pinned, f"QAJEV_{key}"
        elif key in own:
            value, path = own[key]
            if key in shell and shell[key] != value:
                _SHELL_SET_ASIDE.append(key)
            os.environ[key], _SOURCES[key] = value, path
        elif key in shell:
            _SOURCES[key] = "the shell"
    for key, value in DEFAULTS.items():
        os.environ.setdefault(key, value)
    return loaded


def key_sources():
    """Where each of QAJev's own keys came from, as lines for `qajev doctor` and a run's header: names, never values."""
    home = str(Path.home())
    lines = [f"{key}: {where.replace(home, '~', 1) if where.startswith(home) else where}"
             for key, where in _SOURCES.items()]
    lines += [f"the shell's {key} is not used: QAJev's own comes from {_SOURCES[key]} (set QAJEV_{key} to pin one)"
              for key in _SHELL_SET_ASIDE]
    return lines


_REMEMBERED: set[str] = set()  # values read from a vault this process (vault.resolve): redacted like env secrets


def remember_secret(value):
    if value and len(value) >= 4:
        _REMEMBERED.add(value)


def secret_values():
    return sorted(
        {v for k, v in os.environ.items() if SECRET_NAME.search(k) and v and len(v) >= 8} | _REMEMBERED,
        key=len,
        reverse=True,
    )


def redact(text, secrets=None):
    if not isinstance(text, str):
        return text
    for value in secrets if secrets is not None else secret_values():
        text = text.replace(value, "[redacted]")
    return text


def redact_tree(obj: Any, secrets=None) -> Any:
    secrets = secret_values() if secrets is None else secrets
    if isinstance(obj, dict):
        return {k: redact_tree(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_tree(v, secrets) for v in obj]
    return redact(obj, secrets)


def plain_host(url):
    """The host a browser would also read, or "" when it might read another (the review of #47: Chrome takes "\\" for
    "/" so `http://evil.com\\@localhost/` goes to evil.com while urlsplit says localhost). No backslash, control
    character, whitespace or "@" may come before the path."""
    text = str(url)
    if any(ord(c) < 33 or c == "\\" for c in text):
        return ""
    head = text.split("://", 1)[-1].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in head or "%" in head:
        return ""
    return (urlsplit(text).hostname or "").lower()


def is_loopback(url):
    return bool(LOOPBACK.match(plain_host(url)))


# What is_local_dev accepts, as `qajev doctor` prints it. Every other host is production: read-only, no override.
LOCAL_DEV_HOSTS = ("localhost", "127.0.0.1", "[::1]", "*.localhost", "*.test")


def is_local_dev(url):
    """A local development host: loopback, or a name under .localhost or .test (RFC 6761: never on the internet)."""
    host = plain_host(url)
    return bool(host) and (is_loopback(url) or host.endswith((".test", ".localhost")))


# Reserved for testing and examples (RFC 2606, RFC 6761): no real person signs in with an address here.
TEST_EMAIL_DOMAINS = ("example.com", "example.net", "example.org")
TEST_EMAIL_SUFFIXES = (".test", ".example", ".invalid", ".localhost")


def is_test_email(email):
    domain = str(email).rpartition("@")[2].strip().lower()
    return domain in TEST_EMAIL_DOMAINS or domain.endswith(TEST_EMAIL_SUFFIXES) or domain in ("test", "localhost")


def host_of(url):
    return urlsplit(url).netloc
