"""Draws qajev.com's social card (site/og.png, 1200x630) and app icons from the site's own fonts and colours.

    uv run --with pillow python scripts/og_card.py

Fonts (Bricolage Grotesque, DM Mono; OFL) are fetched once from github.com/google/fonts into ~/.cache/qajev-og.
The logo comes from site/favicon.svg through rsvg-convert (brew install librsvg).
"""

import subprocess
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SITE = Path(__file__).resolve().parent.parent / "site"
CACHE = Path.home() / ".cache" / "qajev-og"
FONTS = {
    "sans": "ofl/bricolagegrotesque/BricolageGrotesque%5Bopsz,wdth,wght%5D.ttf",
    "mono": "ofl/dmmono/DMMono-Regular.ttf",
    "mono-medium": "ofl/dmmono/DMMono-Medium.ttf",
}
BLUE, NAVY, NAVY_2, HI, PALE, MUTED, WHITE = "#2430c9", "#0e1240", "#1a1f5c", "#ddf75b", "#d3d6f5", "#9aa0d6", "#ffffff"


def font_file(key):
    path = CACHE / FONTS[key].rsplit("/", 1)[1].replace("%5B", "[").replace("%5D", "]")
    if not path.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(f"https://github.com/google/fonts/raw/main/{FONTS[key]}", path)
    return path


def sans(size, weight=800):
    f = ImageFont.truetype(font_file("sans"), size)
    want = {"optical size": 96, "width": 100, "weight": weight}
    axes = f.get_variation_axes()  # names come back as bytes, e.g. b"Weight"
    f.set_variation_by_axes([float(want.get((a["name"] or b"").decode().lower(), a["default"] or 0)) for a in axes])
    return f


def mono(size, medium=False):
    return ImageFont.truetype(font_file("mono-medium" if medium else "mono"), size)


def text(draw, xy, s, font, fill, tracking=0.0):
    """Text with letter-spacing (em), keeping kerning: each glyph sits where the prefix ends."""
    x, y = xy
    if not tracking:
        draw.text((x, y), s, font=font, fill=fill)
        return x + font.getlength(s)
    em = font.size * tracking
    for i, ch in enumerate(s):
        draw.text((x + font.getlength(s[:i]) + i * em, y), ch, font=font, fill=fill)
    return x + font.getlength(s) + len(s) * em


def logo(size):
    png = subprocess.run(["rsvg-convert", "-w", str(size), "-h", str(size), str(SITE / "favicon.svg")],
                         check=True, capture_output=True).stdout
    path = CACHE / f"logo-{size}.png"
    path.write_bytes(png)
    return Image.open(path).convert("RGBA")


def card():
    w, h, pad = 1200, 630, 72
    img = Image.new("RGB", (w, h), BLUE)
    d = ImageDraw.Draw(img)
    # brand row
    img.paste(m := logo(60), (pad, 56), m)
    text(d, (pad + 76, 58), "QAJev", sans(44), WHITE, -0.02)
    pill = "Open source · MIT"
    pf = mono(20)
    pw = pf.getlength(pill) + 36
    d.rounded_rectangle((w - pad - pw, 64, w - pad, 104), radius=20, outline=PALE, width=2)
    d.text((w - pad - pw + 18, 73), pill, font=pf, fill=PALE)
    # headline, as on the page: "a person" in the highlight colour
    hf, lh, y = sans(86), 84, 172
    text(d, (pad - 4, y), "Test your website", hf, WHITE, -0.02)
    x = text(d, (pad - 4, y + lh), "the way ", hf, WHITE, -0.02)
    text(d, (x, y + lh), "a person", hf, HI, -0.02)
    text(d, (pad - 4, y + 2 * lh), "uses it.", hf, WHITE, -0.02)
    # what it is
    sf = mono(24)
    d.text((pad, 488), "Plain-English goals · a real Chrome", font=sf, fill=PALE)
    d.text((pad, 524), "desktop and phone · CLI and MCP server", font=sf, fill=PALE)
    # the verdict card, like the hero's gate
    cx0, cy0, cx1, cy1 = 822, 172, w - pad, 452
    d.rounded_rectangle((cx0, cy0, cx1, cy1), radius=22, fill=NAVY)
    d.text((cx0 + 30, cy0 + 28), "GATE", font=mono(20, True), fill=MUTED)
    text(d, (cx0 + 26, cy0 + 56), "PASS", sans(84), HI, -0.03)
    rows = [("desktop", "price shown"), ("phone", "price shown")]
    rf = mono(19)
    for i, (lane, what) in enumerate(rows):
        ry = cy0 + 176 + i * 42
        d.ellipse((cx0 + 30, ry + 4, cx0 + 46, ry + 20), fill=HI)
        d.text((cx0 + 60, ry), lane, font=mono(19, True), fill=WHITE)
        d.text((cx0 + 60 + rf.getlength(lane) + 14, ry), what, font=rf, fill=PALE)
    # address
    af = mono(28, True)
    d.text((w - pad - af.getlength("qajev.com"), 512), "qajev.com", font=af, fill=HI)
    img.save(SITE / "og.png", optimize=True)
    return img


def icons():
    """apple-touch-icon.png (180) and icon-512.png (the web manifest, search result favicons)."""
    for name, size in (("apple-touch-icon.png", 180), ("icon-512.png", 512), ("icon-192.png", 192)):
        square = Image.new("RGB", (size, size), "#15140f")  # opaque: iOS fills transparent corners with black
        square.paste(m := logo(size), (0, 0), m)
        square.save(SITE / name, optimize=True)


if __name__ == "__main__":
    card()
    icons()
    print("wrote", ", ".join(p.name for p in sorted(SITE.glob("*.png"))))
