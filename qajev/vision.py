"""What the screen looks like, for Clef: it reads images, Jev does not.

`vision: true` sends the current screenshot with every decision (websites and games), so the model sees what a
visitor sees: labels drawn as pixels, a canvas, an icon. `expect: {looks: [...]}` judges plain-language statements
from the final screenshot ("the Pro plan shows $29 per month", "the Sign up button is not cut off"), one check each.
Both need Clef as the decision model (QAJEV_JEV_PROVIDER=cloudflare): QAJev refuses instead of silently skipping.
"""

import base64
import contextlib
import os

from . import providers

MAX_BYTES = 4 * 1024 * 1024  # Workers AI: 4 MiB per image
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


def data_url(raw):
    """Image bytes as the data URL Clef takes (PNG, JPEG or WebP)."""
    if not raw:
        raise VisionError("no screenshot to look at")
    if len(raw) > MAX_BYTES:
        raise VisionError(f"the screenshot is {len(raw) / 1e6:.1f} MB; Clef takes images up to 4 MiB")
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


def look(post_json, image, statements, context):
    """Judge each statement from the screenshot. -> one check per statement, with Clef's probability.
    post_json is the run's own (ledgered, routed to Clef); context names the page or screen, not its text, so the
    answer comes from the picture."""
    questions = {
        f"look_{i}": {"type": "noul", "instructions":
                      f"Judge only from the screenshot, as a person looking at the screen would. Is this true: {s}"}
        for i, s in enumerate(statements)
    }
    body = {"model": "jev-latest", "state": context, "images": [image], "questions": questions}
    answers = (post_json(providers.TYPESAFE_URL, os.environ.get("TYPESAFE_API_KEY", ""), body) or {}).get(
        "answers") or {}
    checks = []
    for i, statement in enumerate(statements):
        p = (answers.get(f"look_{i}") or {}).get("noul")
        ok = isinstance(p, (int, float)) and p >= PASS_AT
        detail = f"p {p:.2f}" if isinstance(p, (int, float)) else "no answer"
        checks.append({"check": f"looks: {statement}", "ok": ok, "detail": detail})
    return checks
