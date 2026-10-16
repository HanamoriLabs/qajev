"""Explicit end-of-case files. Full JSON travels in bounded chunks, outside check previews and model input."""
import base64
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import redact_tree, secret_values

if TYPE_CHECKING:
    from .suite import Scenario

MAX_BYTES = 50 * 1024 * 1024  # same per-file ceiling as QAJev Cloud
CHUNK_BYTES = 8192  # base64 reply stays well below the browser daemon's line limit
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
NAME = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
PATH = re.compile(rf"artifacts/[A-Za-z0-9][A-Za-z0-9_-]{{0,95}}/({NAME})\.(png|json)")


class CaptureError(RuntimeError):
    pass


def manifest_path(entry: object) -> str | None:
    """A report may link/download only a typed, named attachment within the case-artifact namespace."""
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
        return None
    match = PATH.fullmatch(entry["path"])
    if not match or (entry.get("name"), entry.get("kind")) != match.groups():
        return None
    size, digest = entry.get("bytes"), entry.get("sha256")
    if type(size) is not int or not 0 <= size <= MAX_BYTES or not isinstance(digest, str) \
            or not re.fullmatch(r"[a-f0-9]{64}", digest):
        return None
    return entry["path"]


def json_state(session: Any, expression: str, max_bytes: int = MAX_BYTES) -> bytes:
    """Freeze one JSON value in the page, then read its UTF-8 bytes without a large daemon message."""
    key = json.dumps("__qajev_export_" + uuid.uuid4().hex)
    begin = """(async () => {
      try {
        const text = JSON.stringify(await (%s));
        if (typeof text !== 'string') return {error: 'not_json'};
        const bytes = new TextEncoder().encode(text + '\\n');
        if (bytes.length > %d) return {error: 'too_big'};
        Object.defineProperty(globalThis, %s, {value: bytes, configurable: true});
        return {size: bytes.length};
      } catch { return {error: 'not_json'}; }
    })()""" % (expression, max_bytes, key)
    try:
        meta = session.evaluate(begin)
        if isinstance(meta, dict) and meta.get("error") == "too_big":
            raise CaptureError(f"JSON capture exceeds the {max_bytes}-byte file limit; no snapshot was written")
        if not isinstance(meta, dict) or type(meta.get("size")) is not int or not 0 < meta["size"] <= max_bytes:
            raise CaptureError("JSON capture must return a serializable JSON value")
        chunks = []
        for start in range(0, meta["size"], CHUNK_BYTES):
            expression = f"btoa(String.fromCharCode(...globalThis[{key}].subarray({start}, {start + CHUNK_BYTES})))"
            data = base64.b64decode(session.evaluate(expression), validate=True)
            if len(data) != min(CHUNK_BYTES, meta["size"] - start):
                raise CaptureError("JSON capture was incomplete; no snapshot was written")
            chunks.append(data)
        payload = b"".join(chunks)
        value = json.loads(payload)  # no invalid or partly transported JSON is published
        secrets = secret_values()
        if secrets:
            redacted = json.dumps(redact_tree(value, secrets), ensure_ascii=False, separators=(",", ":"))
            payload = (redacted + "\n").encode()
            if len(payload) > max_bytes:
                raise CaptureError(f"JSON capture exceeds the {max_bytes}-byte file limit; no snapshot was written")
        return payload
    except CaptureError:
        raise
    except (RuntimeError, TimeoutError, ValueError, TypeError, KeyError):
        raise CaptureError("JSON capture failed; no snapshot was written") from None
    finally:
        try:
            session.evaluate(f"delete globalThis[{key}]")
        except (RuntimeError, TimeoutError, ValueError, TypeError):
            pass


def _folder(run_dir: Path, case_name: str) -> tuple[Path, Path]:
    root = Path(run_dir).resolve()
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", case_name).strip("-_")[:48] or "case"
    case_id = slug + "-" + hashlib.sha256(case_name.encode()).hexdigest()[:12]
    folder = root
    for part in ("artifacts", case_id):
        folder /= part
        if folder.is_symlink():
            raise CaptureError("Capture folders cannot be symbolic links")
        folder.mkdir(exist_ok=True)
    return root, folder


def capture(session: Any, scenario: "Scenario", run_dir: Path | None,
            shots: bool = True) -> tuple[list[dict[str, Any]], list[str]]:
    """Return successful manifests plus plain errors; a failed export cannot discard another successful file."""
    files, errors = [], []
    for kind, spec in scenario.capture.items():
        name = spec["name"] if kind == "json" else spec
        temporary = None
        try:
            if kind == "png" and not shots:
                raise CaptureError("PNG capture requires screenshots; remove --no-shots")
            if run_dir is None:
                raise CaptureError("Capture requires a report folder")
            payload = json_state(session, spec["js"], max_bytes=MAX_BYTES) if kind == "json" else None
            root, folder = _folder(run_dir, scenario.name)
            target = folder / f"{name}.{kind}"
            fd, temporary_name = tempfile.mkstemp(prefix=".capture-", dir=folder)
            os.close(fd)
            temporary = Path(temporary_name)
            if kind == "png":
                if not session.screenshot(temporary, format="png"):
                    raise CaptureError("PNG capture did not produce a screenshot")
                if temporary.stat().st_size > MAX_BYTES:
                    raise CaptureError(f"PNG capture exceeds the {MAX_BYTES}-byte file limit")
                payload = temporary.read_bytes()
                if not payload.startswith(PNG_MAGIC):
                    raise CaptureError("PNG capture returned a different image kind; no PNG was written")
            else:
                assert isinstance(payload, bytes)
                temporary.write_bytes(payload)
            os.link(temporary, target)  # exclusive publication: never follow or replace an existing target
            files.append({"name": name, "kind": kind, "path": str(target.relative_to(root)),
                          "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
        except CaptureError as error:
            errors.append(str(error))
        except OSError:
            errors.append(f"{kind.upper()} capture could not write its attachment")
        except (RuntimeError, TimeoutError, ValueError, TypeError, KeyError):
            errors.append(f"{kind.upper()} capture failed")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return files, errors
