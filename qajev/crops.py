"""Validate frame-pixel regions and produce bounded, lossless image crops."""

import io
import struct
import warnings

from PIL import Image

from .pixels import PixelError, _decode

_MAX_BYTES = 32 * 1024 * 1024
_MAX_DIMENSION = 16384
_MAX_PIXELS = 16777216
_REGION_KEYS = {"name", "x", "y", "width", "height"}


class CropError(ValueError):
    """An image or named region cannot produce a valid crop."""


def validate_region(value: dict) -> dict:
    """Validate a named rectangle in integer frame pixels, without an image."""
    if not isinstance(value, dict) or set(value) != _REGION_KEYS:
        raise CropError("region must contain only name, x, y, width and height")
    if not isinstance(value["name"], str) or not value["name"].strip():
        raise CropError("region name must be a nonempty string")
    for key in ("x", "y", "width", "height"):
        coordinate = value[key]
        if isinstance(coordinate, bool) or not isinstance(coordinate, int):
            raise CropError("region coordinates and dimensions must be integers")
        if coordinate < 0 or (key in ("width", "height") and coordinate == 0):
            raise CropError("region origins must be nonnegative and dimensions must be positive")
    return dict(value)


def _check_dimensions(width: int, height: int) -> None:
    if not (
        0 < width <= _MAX_DIMENSION and 0 < height <= _MAX_DIMENSION and width * height <= _MAX_PIXELS
    ):
        raise CropError("image dimensions exceed the crop limits")


def _check_jpeg(raw: bytes) -> None:
    # Walk markers, not image pixels, so the first actual EOI must terminate the input.
    offset = 2
    in_scan = False
    while offset < len(raw):
        from_scan = in_scan
        if in_scan:
            offset = raw.find(b"\xff", offset)
            if offset < 0:
                break
        if raw[offset] != 255:
            break
        while offset < len(raw) and raw[offset] == 255:
            offset += 1
        if offset == len(raw):
            break
        marker = raw[offset]
        offset += 1
        if from_scan and (marker == 0 or 0xD0 <= marker <= 0xD7):
            continue
        in_scan = False
        if marker == 0xD9:
            if offset == len(raw):
                return
            break
        if marker in (0, 0xD8) or offset + 2 > len(raw):
            break
        if marker == 1:
            in_scan = from_scan
            continue
        length = struct.unpack_from(">H", raw, offset)[0]
        if length < 2 or offset + length > len(raw):
            break
        offset += length
        in_scan = marker == 0xDA or (from_scan and marker == 0xDC)
    raise CropError("JPEG must be complete and end at its first EOI marker")


def _check_webp(raw: bytes) -> None:
    if len(raw) < 20 or struct.unpack_from("<I", raw, 4)[0] + 8 != len(raw):
        raise CropError("WebP must contain a complete RIFF container")
    offset = 12
    saw_image = False
    while offset + 8 <= len(raw):
        kind = raw[offset : offset + 4]
        length = struct.unpack_from("<I", raw, offset + 4)[0]
        start = offset + 8
        end = start + length
        padded_end = end + (length & 1)
        if padded_end > len(raw) or (length & 1 and raw[end] != 0):
            raise CropError("WebP contains an incomplete chunk")
        if kind in (b"ANIM", b"ANMF"):
            raise CropError("image must not be animated")
        if kind == b"VP8X":
            if length != 10:
                raise CropError("WebP has an invalid extended header")
            if raw[start] & 2:
                raise CropError("image must not be animated")
            width = int.from_bytes(raw[start + 4 : start + 7], "little") + 1
            height = int.from_bytes(raw[start + 7 : start + 10], "little") + 1
            _check_dimensions(width, height)
        elif kind in (b"VP8 ", b"VP8L"):
            if saw_image:
                raise CropError("WebP must contain a single image")
            saw_image = True
            if kind == b"VP8L" and length >= 5 and raw[start] == 0x2F:
                bits = struct.unpack_from("<I", raw, start + 1)[0]
                width, height = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            elif kind == b"VP8 " and length >= 10 and raw[start + 3 : start + 6] == b"\x9d\x01\x2a":
                width, height = struct.unpack_from("<HH", raw, start + 6)
                width, height = width & 0x3FFF, height & 0x3FFF
            else:
                raise CropError("WebP has an invalid image header")
            _check_dimensions(width, height)
        offset = padded_end
    if offset != len(raw) or not saw_image:
        raise CropError("WebP must contain one complete image")


def _decode_image(raw: bytes) -> Image.Image:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        try:
            return _decode(raw)
        except PixelError as error:
            raise CropError(str(error)) from error
    if raw.startswith(b"\xff\xd8"):
        expected_format = "JPEG"
        _check_jpeg(raw)
    elif raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        expected_format = "WEBP"
        _check_webp(raw)
    else:
        raise CropError("image must be PNG, JPEG or WebP")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format != expected_format:
                    raise CropError("image format does not match its header")
                _check_dimensions(*image.size)
                if getattr(image, "is_animated", False):
                    raise CropError("image must not be animated")
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
                with image.convert("RGBA") as rgba, Image.new("RGBA", image.size, (255, 255, 255, 255)) as white:
                    with Image.alpha_composite(white, rgba) as composited:
                        return composited.convert("RGB")
    except CropError:
        raise
    except (
        OSError,
        SyntaxError,
        ValueError,
        struct.error,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise CropError("image is not valid and complete") from error


def crop(raw: bytes, region: dict | None = None) -> bytes:
    """Return an RGB PNG cropped in source-frame pixels; None selects the full frame."""
    selected = validate_region(region) if region is not None else None
    if not isinstance(raw, bytes) or not raw:
        raise CropError("image must contain bytes")
    if len(raw) > _MAX_BYTES:
        raise CropError("image exceeds the 32 MiB byte limit")
    with _decode_image(raw) as image:
        width, height = image.size
        if selected is not None:
            x, y = selected["x"], selected["y"]
            right, bottom = x + selected["width"], y + selected["height"]
            if right > width or bottom > height:
                raise CropError("region must fit within the image frame")
            bounds = (x, y, right, bottom)
        else:
            bounds = (0, 0, width, height)
        with image.crop(bounds) as result:
            result.info.clear()
            output = io.BytesIO()
            result.save(output, format="PNG")
            return output.getvalue()
