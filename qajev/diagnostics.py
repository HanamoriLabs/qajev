"""Public check measurements and bounded per-scenario page diagnostics."""

import json
from typing import Any

from qajev_upload_privacy import sanitize_text, sanitize_upload

from .config import redact

CONSOLE_LINES = 50
CONSOLE_CHARACTERS = 500
CONSOLE_LEVELS = {"log", "info", "warn", "error"}
DIAGNOSTIC_BYTES = 64 * 1024


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


def bound_text(text: str) -> str:
    """Bound an already cleaned diagnostic, retaining whole UTF-8 characters and a visible cut count."""
    encoded = text.encode("utf-8")
    if len(encoded) <= DIAGNOSTIC_BYTES:
        return text
    kept = encoded[:DIAGNOSTIC_BYTES].decode("utf-8", errors="ignore")
    return kept + f"\n[truncated {len(text) - len(kept)} characters]"


def clean_evidence(value: dict) -> dict:
    """Keep typed measurements, or an explicit marker when the whole evidence object exceeds one item."""
    public = clean_data(value)
    shown = json.dumps(public, ensure_ascii=False)
    if len(shown.encode("utf-8")) <= DIAGNOSTIC_BYTES:
        return public
    return {"_qajev_truncated": f"[truncated {len(shown)} characters]"}


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
