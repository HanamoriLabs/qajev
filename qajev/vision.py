"""What the screen looks like, for Clef: it reads images, Jev does not.

`vision: true` sends the current screenshot with every decision (websites and games), so the model sees what a
visitor sees: labels drawn as pixels, a canvas, an icon. `expect: {looks: [...]}` judges plain-language statements
from the final screenshot ("the Pro plan shows $29 per month", "the Sign up button is not cut off"), one check each.
Both need Clef as the decision model (QAJEV_JEV_PROVIDER=cloudflare): QAJev refuses instead of silently skipping.
A game run (`qajev play`) sees by default when Clef decides and the game has a window (for_play).
"""

import base64
import contextlib
import hashlib
import math
import os
from pathlib import Path, PurePosixPath

from . import providers

MAX_BYTES = 4 * 1024 * 1024  # Workers AI: 4 MiB per image
MAX_TOTAL_BYTES = 8 * 1024 * 1024  # Workers AI: combined embedded images in a request
PASS_AT = 0.5  # a statement holds when Clef gives it at least this probability


class VisionError(RuntimeError):
    pass


def require_clef(what):
    """`what` (vision, looks) needs a decision model that reads images. -> None, or the reason it cannot run."""
    try:
        resolved = providers.resolve()
    except providers.ProviderError as e:
        return str(e)
    if resolved["jev"] != "cloudflare":
        return (f"{what} needs Clef, the decision model that reads images (Jev reads text only): set "
                "QAJEV_JEV_PROVIDER=cloudflare with CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN")
    return None


def for_play(asked, *, window):
    """Whether a game run sends the screen with each decision. `asked`: True or False (--vision, --no-vision or the
    suite's `vision`), or None: on when Clef decides and there is a window to look at, else off without complaint.
    -> (sees, refused): refused is why an explicit ask cannot run."""
    if asked is False:
        return False, None
    if asked:
        refused = require_clef("vision") or (None if window else
                                             "vision needs the game's window, and a headless Godot game draws none: "
                                             "run it without --headless (MCP: headless=false)")
        return True, refused
    return window and require_clef("vision") is None, None


def data_url(raw, *, max_bytes=MAX_BYTES):
    """Image bytes as the data URL Clef takes (PNG, JPEG or WebP)."""
    if not raw:
        raise VisionError("no screenshot to look at")
    if len(raw) > max_bytes:
        raise VisionError(f"the screenshot is {len(raw) / 1e6:.1f} MB; this image limit is {max_bytes / 2**20:g} MiB")
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif raw[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise VisionError("the screenshot is not a PNG, JPEG or WebP image")
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


@contextlib.contextmanager
def seeing(screenshot):
    """While inside, every decision sent to Clef carries `screenshot()` (a data URL, or None to send none)."""
    before = providers.SEE
    providers.SEE = screenshot
    try:
        yield
    finally:
        providers.SEE = before


def normalize(value):
    """One explicit claim per entry; never turn a malformed mapping into prose."""
    from .crops import CropError, validate_region

    items = [] if value is None else [value] if isinstance(value, str) else value
    if not isinstance(items, list):
        raise VisionError("looks must be a list of statements or claim mappings")
    result = []
    for entry in items:
        if isinstance(entry, str) and entry.strip():
            result.append(entry)
            continue
        if not isinstance(entry, dict) or set(entry) - {"claim", "crop", "reference", "different_from"}:
            raise VisionError("looks entry needs one claim and its named crop")
        if not isinstance(entry.get("claim"), str) or not entry["claim"].strip():
            raise VisionError("looks.claim must name one visible claim")
        try:
            region = entry.get("crop")
            if not isinstance(region, dict):
                raise VisionError("looks.crop must be a named frame-pixel rectangle")
            item = {"claim": entry["claim"].strip(), "crop": validate_region(region)}
            if "reference" in entry:
                ref = entry["reference"]
                if (not isinstance(ref, dict) or set(ref) != {"path", "role"}
                        or not isinstance(ref.get("role"), str)
                        or ref.get("role") not in {"expected", "blockout"}):
                    raise VisionError("looks.reference needs an image path and role expected or blockout")
                _relative_image(ref.get("path"))
                item["reference"] = dict(ref)
            if "different_from" in entry:
                other = entry["different_from"]
                if not isinstance(other, dict) or set(other) - {"crop", "min_changed", "tolerance"}:
                    raise VisionError("looks.different_from needs a named crop and min_changed")
                minimum, tolerance = other.get("min_changed"), other.get("tolerance", 8)
                if (isinstance(minimum, bool) or not isinstance(minimum, (int, float))
                        or not 0 < minimum <= 1 or not math.isfinite(minimum)):
                    raise VisionError("looks.different_from.min_changed must be a finite fraction in (0, 1]")
                if isinstance(tolerance, bool) or not isinstance(tolerance, int) or not 0 <= tolerance <= 255:
                    raise VisionError("looks.different_from.tolerance must be an integer from 0 to 255")
                region = other.get("crop")
                if not isinstance(region, dict):
                    raise VisionError("looks.different_from.crop must be a named frame-pixel rectangle")
                item["different_from"] = {"crop": validate_region(region),
                                          "min_changed": minimum, "tolerance": tolerance}
        except CropError as e:
            raise VisionError(f"looks crop: {e}") from None
        result.append(item)
    return result


def planned(entry):
    return {"check": f"looks: {entry}" if isinstance(entry, str) else
            f"looks: {entry['claim']} (crop {entry['crop']['name']})"}


def _relative_image(value):
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value or "%" in value
            or value.startswith("/") or any(not part or part.startswith(".") for part in value.split("/"))
            or PurePosixPath(value).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}):
        raise VisionError("looks reference must be a relative PNG, JPEG or WebP path outside hidden folders")
    return value


def reference_files(statements, folder):
    """Only explicitly named, bounded images below the suite/config folder may be sent to a runner."""
    result = []
    for entry in normalize(statements):
        if isinstance(entry, str) or "reference" not in entry:
            continue
        relative = _relative_image(entry["reference"]["path"])
        if folder is None:
            raise VisionError("looks reference needs a suite/config folder")
        base = Path(folder).resolve()
        path = base / relative
        try:
            try:
                resolved = path.resolve(strict=True)
            except RuntimeError:
                raise VisionError(f"looks reference {relative!r} contains a symlink loop") from None
            parts = path.relative_to(base).parts
            if (base not in resolved.parents or not resolved.is_file()
                    or any(part.startswith(".") for part in resolved.relative_to(base).parts)
                    or any(base.joinpath(*parts[:i]).is_symlink() for i in range(1, len(parts) + 1))
                    or resolved.stat().st_size > 32 * 1024 * 1024):
                raise VisionError(f"looks reference {relative!r} is outside its folder, hidden or oversized")
        except OSError:
            raise VisionError(f"looks reference {relative!r} cannot be read") from None
        result.append((relative, resolved))
    return result


def _image_bytes(image):
    try:
        prefix, encoded = image.split(",", 1)
        if prefix not in {"data:image/png;base64", "data:image/jpeg;base64", "data:image/webp;base64"}:
            raise ValueError("image format")
        if len(encoded) > ((32 * 1024 * 1024 + 2) // 3) * 4:
            raise ValueError("image size")
        return base64.b64decode(encoded, validate=True)
    except (AttributeError, TypeError, ValueError):
        raise VisionError("looks needs a valid, bounded screenshot data URL") from None


def _save_image(folder, raw):
    name = "shots/looks-" + hashlib.sha256(raw).hexdigest()[:32] + ".png"
    path = Path(folder) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return name


def look(post_json, image, statements, context, *, folder=None, evidence_dir=None):
    """Judge each claim alone, on its crop, using the run's ledgered model caller."""
    from .crops import CropError, crop
    from .pixels import PixelError, compare

    statements = normalize(statements)
    references = dict(reference_files(statements, folder))
    checks, raw = [], None
    for i, entry in enumerate(statements):
        evidence, state, images = {}, dict(context), [image]
        text = entry if isinstance(entry, str) else entry["claim"]
        instructions = "Judge only from the supplied image pixels, as a person looking at the screen would. "
        if isinstance(entry, dict):
            if evidence_dir is None:
                raise VisionError("structured looks needs a report folder for the exact evidence")
            try:
                raw = _image_bytes(image) if raw is None else raw
                capture = crop(raw, entry["crop"])
                image_bytes = len(capture)
                images = [data_url(capture)]
                evidence = {"crop": _save_image(evidence_dir, capture), "region": entry["crop"]}
                instructions += "Image 1 is only this claim's named crop. "
                if "reference" in entry:
                    ref = entry["reference"]
                    with references[ref["path"]].open("rb") as stream:
                        reference = crop(stream.read(32 * 1024 * 1024 + 1))
                    images.append(data_url(reference))
                    image_bytes += len(reference)
                    evidence.update(reference=_save_image(evidence_dir, reference), reference_role=ref["role"])
                    state["reference_role"] = ref["role"]
                    if ref["role"] == "blockout" and reference == capture:
                        checks.append({**planned(entry), "ok": False, "evidence": evidence,
                                       "detail": "scene crop has identical pixels to the block-out reference"})
                        continue
                    instructions += (f"Image {len(images)} is the {ref['role']} reference. "
                                     + ("It is an unfinished placeholder/block-out, not a passing target. "
                                        "Reject a scene that still has that placeholder quality. "
                                        if ref["role"] == "blockout" else
                                        "Use it to judge the requested relative or quality claim. "))
                if "different_from" in entry:
                    spec = entry["different_from"]
                    other = crop(raw, spec["crop"])
                    metrics, difference = compare(capture, other, tolerance=spec["tolerance"],
                                                  min_changed=spec["min_changed"])
                    evidence.update(panel=_save_image(evidence_dir, other),
                                    difference=_save_image(evidence_dir, difference), comparison=metrics,
                                    panel_region=spec["crop"])
                    if not metrics["ok"]:
                        checks.append({**planned(entry), "ok": False, "evidence": evidence,
                                       "detail": f"panels too similar: {metrics['changed']}/{metrics['total']} "
                                                 f"pixels changed; fraction {metrics['fraction']:.8g}, minimum "
                                                 f"{metrics['min_changed']:.8g}, tolerance {metrics['tolerance']}"})
                        continue
                    images.append(data_url(other))
                    image_bytes += len(other)
                    instructions += f"Image {len(images)} is the comparison panel from the same frame. "
                if image_bytes > MAX_TOTAL_BYTES:
                    raise VisionError("looks images exceed Clef's combined 8 MiB image limit")
            except (CropError, PixelError) as e:
                raise VisionError(f"looks {text!r}: {e}") from None
            except OSError as e:
                raise VisionError(f"looks {text!r}: could not read/write image evidence ({e.strerror})") from None
        instructions += f"Judge this one claim only; do not average it with another claim: {text}"
        question = f"look_{i}"
        body = {"model": "jev-latest", "state": state, "images": images,
                "questions": {question: {"type": "noul", "instructions": instructions}}}
        reply = post_json(providers.TYPESAFE_URL, os.environ.get("TYPESAFE_API_KEY", ""), body)
        answers = reply.get("answers") if isinstance(reply, dict) else None
        answer = answers.get(question) if isinstance(answers, dict) else None
        p = answer.get("noul") if isinstance(answer, dict) else None
        valid = (not isinstance(p, bool) and isinstance(p, (int, float)) and 0 <= p <= 1 and math.isfinite(p))
        check = {**planned(entry), "ok": isinstance(p, (int, float)) and valid and p >= PASS_AT,
                 "detail": f"p {p:.2f}" if valid else "no valid probability"}
        if evidence:
            check.update(evidence=evidence, probability=p if valid else None)
        checks.append(check)
    return checks
