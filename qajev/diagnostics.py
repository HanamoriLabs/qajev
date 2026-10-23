"""Public check measurements and bounded per-scenario page diagnostics."""

import json
from typing import Any

from qajev_upload_privacy import sanitize_text, sanitize_upload

from .config import redact

CONSOLE_LINES = 50
CONSOLE_CHARACTERS = 500
CONSOLE_LEVELS = {"warn", "error"}


def _redacted(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            name = redact(key)
            if name in out:
                raise ValueError("diagnostic key redaction would collide")
            out[name] = _redacted(item)
        return out
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    return redact(value)


def clean_data(value: Any) -> Any:
    """Keep JSON types and measured numbers while cleaning strings before retention."""
    encoded = json.dumps(_redacted(value), ensure_ascii=False).encode("utf-8")
    return json.loads(sanitize_upload(encoded))


def keep_console(result: dict, probe: dict) -> None:
    """Accumulate across reads and navigations, with one cap for the whole scenario."""
    for item in probe.get("page_console") or []:
        if not isinstance(item, dict) or item.get("level") not in CONSOLE_LEVELS or not isinstance(item.get("text"), str):
            continue
        kept = result.setdefault("page_console", [])
        if len(kept) >= CONSOLE_LINES:
            result["page_console_omitted"] = result.get("page_console_omitted", 0) + 1
            continue
        text = sanitize_text(redact(item["text"]))
        kept.append({"level": item["level"], "text": text[:CONSOLE_CHARACTERS],
                     **({"truncated": True} if len(text) > CONSOLE_CHARACTERS else {})})
    for count in probe.get("page_console_omitted") or []:
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            result["page_console_omitted"] = result.get("page_console_omitted", 0) + count
