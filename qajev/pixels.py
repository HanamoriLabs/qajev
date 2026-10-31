"""Compare cropped PNG captures without browser or filesystem access."""

import io
import math
import struct
import warnings

from PIL import Image, ImageChops

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_END = b"\x00\x00\x00\x00IEND\xaeB\x60\x82"
_MAX_DIMENSION = 16384
_MAX_PIXELS = 16777216


class PixelError(ValueError):
    """A capture or comparison threshold cannot produce a valid measurement."""


def _check_png_end(data: bytes) -> None:
    # Pillow can stop at the first IEND. Require that chunk to be the actual end of the input.
    offset = len(_PNG_SIGNATURE)
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + length
        if end > len(data):
            break
        if data[offset + 4 : offset + 8] == b"IEND":
            if length == 0 and end == len(data):
                return
            break
        offset = end
    raise PixelError("capture must end at its complete PNG IEND chunk")


def _decode(data: bytes) -> Image.Image:
    if not isinstance(data, bytes) or not data.startswith(_PNG_SIGNATURE) or not data.endswith(_PNG_END):
        raise PixelError("capture must contain complete PNG bytes")
    _check_png_end(data)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "PNG":
                    raise PixelError("capture must be a PNG image")
                width, height = image.size
                if not (
                    0 < width <= _MAX_DIMENSION
                    and 0 < height <= _MAX_DIMENSION
                    and width * height <= _MAX_PIXELS
                ):
                    raise PixelError("capture dimensions exceed the comparison limits")
                if getattr(image, "is_animated", False):
                    raise PixelError("capture must be a single PNG image")
                image.verify()
            # verify checks the PNG structure and checksums; load also checks the decoded pixel stream.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                with image.convert("RGBA") as rgba, Image.new("RGBA", image.size, (255, 255, 255, 255)) as white:
                    with Image.alpha_composite(white, rgba) as composited:
                        return composited.convert("RGB")
    except PixelError:
        raise
    except (
        OSError,
        SyntaxError,
        ValueError,
        struct.error,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise PixelError("capture is not a valid, complete PNG image") from error


def compare(off_png: bytes, on_png: bytes, *, tolerance: int, min_changed: float) -> tuple[dict, bytes]:
    """Count RGB pixels above tolerance after compositing both captures on white.

    The minimum changed fraction is inclusive. The difference PNG marks changed pixels
    in magenta and all other pixels in black; it contains no source-image pixels.
    """
    if isinstance(tolerance, bool) or not isinstance(tolerance, int) or not 0 <= tolerance <= 255:
        raise PixelError("tolerance must be an integer from 0 to 255")
    if (
        isinstance(min_changed, bool)
        or not isinstance(min_changed, (int, float))
        or not 0 < min_changed <= 1
        or not math.isfinite(min_changed)
    ):
        raise PixelError("min_changed must be a finite fraction greater than 0 and at most 1")

    with _decode(off_png) as off, _decode(on_png) as on:
        if off.size != on.size:
            raise PixelError("capture dimensions must match")
        width, height = off.size
        with ImageChops.difference(off, on) as delta:
            red, green, blue = delta.split()
            with red, green, blue, ImageChops.lighter(red, green) as red_green:
                with ImageChops.lighter(red_green, blue) as largest:
                    with largest.point([255 if value > tolerance else 0 for value in range(256)]) as mask:
                        changed = mask.histogram()[255]
                        total = width * height
                        fraction = changed / total
                        output = io.BytesIO()
                        with Image.new("RGB", off.size, (0, 0, 0)) as difference:
                            difference.paste((255, 0, 255), mask=mask)
                            difference.save(output, format="PNG")

    return {
        "ok": fraction >= min_changed,
        "changed": changed,
        "total": total,
        "fraction": fraction,
        "min_changed": min_changed,
        "tolerance": tolerance,
        "width": width,
        "height": height,
    }, output.getvalue()


def flat_dark(lumas, *, dark=24, spread=6, share=0.97, least=40):
    """A canvas read at sample points (grey levels 0-255) as one flat dark colour: nothing drawn, or a capture without
    its GPU layer. -> {points, median, flat} when at least `least` points were read, their median is under `dark` and
    at least `share` of them are within `spread` of it; else None."""
    if len(lumas) < least:
        return None
    middle = sorted(lumas)[len(lumas) // 2]
    near = sum(1 for value in lumas if abs(value - middle) <= spread) / len(lumas)
    if middle >= dark or near < share:
        return None
    return {"points": len(lumas), "median": middle, "flat": round(near, 3)}
