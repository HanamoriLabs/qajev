"""Public check measurements and bounded per-scenario page diagnostics."""

import json
from typing import Any

from qajev_upload_privacy import sanitize_text, sanitize_upload

from .config import redact

CONSOLE_LINES = 50
CONSOLE_KEPT_LINES = 200  # console_keep: the lines that match, on a cap of their own
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


def _console_line(item):
    return isinstance(item, dict) and item.get("level") in CONSOLE_LEVELS and isinstance(item.get("text"), str)


def _cleaned(item):
    text = sanitize_text(redact(item["text"]))
    return {"level": item["level"], "text": text[:CONSOLE_CHARACTERS],
            **({"truncated": True} if len(text) > CONSOLE_CHARACTERS else {})}


def _count(result, key, counts):
    for count in counts or []:
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            result[key] = result.get(key, 0) + count


def keep_console(result: dict, probe: dict) -> None:
    """Accumulate across reads and navigations, with one cap for the whole scenario. The lines that match the test's
    console_keep come apart (page_console_kept), on a cap of their own that no other line counts against."""
    for item in probe.get("page_console_kept") or []:
        if not _console_line(item):
            continue
        kept = result.setdefault("page_console_kept", [])
        if len(kept) >= CONSOLE_KEPT_LINES:
            result["page_console_kept_omitted"] = result.get("page_console_kept_omitted", 0) + 1
            continue
        kept.append(_cleaned(item))
    _count(result, "page_console_kept_omitted", probe.get("page_console_kept_omitted"))
    for pattern in probe.get("console_keep_broken") or []:
        if isinstance(pattern, str) and pattern not in result.setdefault("console_keep_broken", []):
            result["console_keep_broken"].append(pattern)
    for item in probe.get("page_console") or []:
        if not _console_line(item):
            continue
        kept = result.setdefault("page_console", [])
        if len(kept) >= CONSOLE_LINES:
            result["page_console_omitted"] = result.get("page_console_omitted", 0) + 1
            routine = next((i for i, row in enumerate(kept) if row["level"] in {"log", "info"}), None) \
                if item["level"] in {"warn", "error"} else None
            if routine is None:
                continue
            kept.pop(routine)
        kept.append(_cleaned(item))
    _count(result, "page_console_omitted", probe.get("page_console_omitted"))
