"""Paths, environment and secret handling. Stdlib only: imported on every CLI start."""

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

HOME = Path(os.environ.get("QAJEV_HOME", Path.home() / ".qajev"))

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


def env_files(explicit=None):
    candidates = [explicit, os.environ.get("QAJEV_ENV_FILE"), Path.cwd() / ".env", HOME / ".env"]
    return [Path(p).expanduser() for p in candidates if p]


def load_env(explicit=None):
    """First file wins per key; the process environment always wins over files."""
    # A value QAJev itself filled in for jev (see providers.PLACEHOLDER) is not a real setting: a child
    # process that inherits it must still pick up the real key from its env file.
    for key, value in list(os.environ.items()):
        if value == ROUTING_PLACEHOLDER:
            del os.environ[key]
    loaded = []
    for path in env_files(explicit):
        if not path.is_file():
            if explicit and path == Path(explicit).expanduser():
                raise FileNotFoundError(f"env file not found: {path}")
            continue
        for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().removeprefix("export ").strip()
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))
        loaded.append(str(path))
    for key, value in DEFAULTS.items():
        os.environ.setdefault(key, value)
    return loaded


def secret_values():
    return sorted(
        {v for k, v in os.environ.items() if SECRET_NAME.search(k) and v and len(v) >= 8},
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


def is_loopback(url):
    host = urlsplit(url).hostname or ""
    return bool(LOOPBACK.match(host))


def host_of(url):
    return urlsplit(url).netloc
