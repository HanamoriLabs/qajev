import io
import struct
import zlib

import pytest
from PIL import Image

from qajev.crops import CropError, crop, validate_region


def encoded(colors, *, size=None, mode="RGB", format="PNG"):
    image = Image.new(mode, size or (len(colors), 1))
    image.putdata(colors)
    output = io.BytesIO()
    options = {"lossless": True} if format == "WEBP" else {}
    image.save(output, format=format, **options)
    return output.getvalue()


def decoded(data):
    assert isinstance(data, bytes) and data.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG" and image.mode == "RGB"
        image.load()
        return image.size, list(image.get_flattened_data())


def region(**changes):
    return {"name": "detail", "x": 0, "y": 0, "width": 1, "height": 1, **changes}


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png_header(width, height):
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", b"") + chunk(b"IEND", b"")


def test_crop_uses_exact_frame_pixels_and_preserves_region_input():
    colors = [(n, n + 1, n + 2) for n in range(6)]
    selected = region(x=1, y=1, width=2)
    original = dict(selected)

    assert decoded(crop(encoded(colors, size=(3, 2)), selected)) == ((2, 1), colors[4:6])
    assert selected == original


def test_pixels_outside_the_region_do_not_affect_the_result():
    off = encoded([(0, 0, 0), (20, 30, 40), (0, 0, 0)])
    on = encoded([(255, 0, 0), (20, 30, 40), (0, 255, 0)])

    assert crop(off, region(x=1)) == crop(on, region(x=1))
    assert crop(off) != crop(on)


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP"])
def test_full_frame_supports_only_the_documented_decodable_formats(format):
    raw = encoded([(30, 60, 90)] * 4, size=(2, 2), format=format)
    with Image.open(io.BytesIO(raw)) as source:
        expected = list(source.convert("RGB").get_flattened_data())

    assert decoded(crop(raw)) == ((2, 2), expected)


@pytest.mark.parametrize("format", ["PNG", "WEBP"])
def test_alpha_is_composited_on_white(format):
    raw = encoded([(255, 0, 0, 0), (0, 0, 0, 128)], mode="RGBA", format=format)

    assert decoded(crop(raw)) == ((2, 1), [(255, 255, 255), (127, 127, 127)])


@pytest.mark.parametrize("format", ["PNG", "JPEG"])
def test_grayscale_is_normalized_to_rgb(format):
    raw = encoded([42, 42], mode="L", format=format)

    assert decoded(crop(raw)) == ((2, 1), [(42, 42, 42), (42, 42, 42)])


def test_validate_region_returns_an_independent_copy():
    value = region(x=2, y=3, width=4, height=5)

    validated = validate_region(value)

    assert validated == value and validated is not value
    validated["x"] = 100
    assert value["x"] == 2


@pytest.mark.parametrize("key", ["x", "y", "width", "height"])
@pytest.mark.parametrize("value", [True, False, 0.5, float("nan"), float("inf"), "1", None])
def test_coordinates_must_be_non_bool_integers(key, value):
    invalid = region(**{key: value})
    with pytest.raises(CropError):
        validate_region(invalid)
    with pytest.raises(CropError):
        crop(encoded([(0, 0, 0)]), invalid)


@pytest.mark.parametrize("changes", [{"x": -1}, {"y": -1}, {"width": 0}, {"height": 0}, {"width": -1}])
def test_rejects_negative_origins_and_empty_dimensions(changes):
    with pytest.raises(CropError):
        validate_region(region(**changes))


@pytest.mark.parametrize("name", ["", "  ", None, 5, False])
def test_region_requires_a_nonempty_name(name):
    with pytest.raises(CropError):
        validate_region(region(name=name))


@pytest.mark.parametrize("bad", [None, [], "detail", {}, {"name": "detail"}, region(extra=1)])
def test_region_shape_is_strict(bad):
    with pytest.raises(CropError):
        validate_region(bad)
    if bad is not None:
        with pytest.raises(CropError):
            crop(encoded([(0, 0, 0)]), bad)


@pytest.mark.parametrize("changes", [{"x": 2}, {"y": 2}, {"x": 1, "width": 2}, {"y": 1, "height": 2}])
def test_rejects_regions_outside_the_frame(changes):
    raw = encoded([(0, 0, 0)] * 4, size=(2, 2))
    with pytest.raises(CropError):
        crop(raw, region(**changes))
    assert decoded(crop(raw, region(x=1, y=1)))[0] == (1, 1)


@pytest.mark.parametrize("raw", [None, "image", bytearray(b"image"), b"", b"not an image"])
def test_rejects_missing_or_invalid_bytes(raw):
    with pytest.raises(CropError):
        crop(raw)


def test_rejects_more_than_32_mib_before_opening_a_decoder(monkeypatch):
    def must_not_open(*args, **kwargs):
        raise AssertionError("oversized bytes must be rejected before decoding")

    monkeypatch.setattr(Image, "open", must_not_open)
    with pytest.raises(CropError, match="32 MiB"):
        crop(b"x" * (32 * 1024 * 1024 + 1))


@pytest.mark.parametrize("size", [(20000, 1), (1, 20000), (8192, 8192), (0, 1)])
def test_png_dimension_limits_are_checked_before_pixel_load(monkeypatch, size):
    def must_not_load(*args, **kwargs):
        raise AssertionError("oversized dimensions must be rejected before loading pixels")

    monkeypatch.setattr(Image.Image, "load", must_not_load)
    with pytest.raises(CropError):
        crop(png_header(*size))


def test_jpeg_dimension_limits_are_checked_before_pixel_load(monkeypatch):
    raw = encoded([(0, 0, 0)], format="JPEG")
    start = raw.index(b"\xff\xc0")
    huge = raw[:start + 5] + struct.pack(">HH", 8192, 8192) + raw[start + 9:]

    def must_not_load(*args, **kwargs):
        raise AssertionError("oversized JPEG must be rejected before loading pixels")

    monkeypatch.setattr(Image.Image, "load", must_not_load)
    with pytest.raises(CropError):
        crop(huge)


def test_webp_dimension_limits_are_checked_before_decoder_construction(monkeypatch):
    dimensions = (8191).to_bytes(3, "little") * 2
    body = b"WEBPVP8X" + struct.pack("<I", 10) + b"\x00" * 4 + dimensions
    raw = b"RIFF" + struct.pack("<I", len(body)) + body

    def must_not_open(*args, **kwargs):
        raise AssertionError("oversized WebP header must be rejected before opening its decoder")

    monkeypatch.setattr(Image, "open", must_not_open)
    with pytest.raises(CropError, match="dimensions"):
        crop(raw)


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP"])
def test_rejects_truncated_images(format):
    raw = encoded([(0, 0, 0), (255, 255, 255)], format=format)
    with pytest.raises(CropError):
        crop(raw[:-1])


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP"])
def test_rejects_trailing_data_even_with_a_second_end_marker(format):
    raw = encoded([(0, 0, 0)], format=format)
    suffix = raw[-12:] if format == "PNG" else b"\xff\xd9" if format == "JPEG" else b""
    with pytest.raises(CropError):
        crop(raw + b"trailing data" + suffix)


def test_supports_progressive_jpeg_scan_markers():
    output = io.BytesIO()
    Image.new("RGB", (2, 2), (30, 60, 90)).save(output, format="JPEG", progressive=True)
    raw = output.getvalue()
    with Image.open(io.BytesIO(raw)) as source:
        expected = list(source.convert("RGB").get_flattened_data())

    assert decoded(crop(raw)) == ((2, 2), expected)


def test_supports_lossy_webp_headers():
    output = io.BytesIO()
    Image.new("RGB", (2, 2), (30, 60, 90)).save(output, format="WEBP", lossless=False)
    raw = output.getvalue()
    with Image.open(io.BytesIO(raw)) as source:
        expected = list(source.convert("RGB").get_flattened_data())

    assert decoded(crop(raw)) == ((2, 2), expected)


@pytest.mark.parametrize("format", ["GIF", "BMP", "TIFF"])
def test_rejects_other_decodable_formats(format):
    with pytest.raises(CropError):
        crop(encoded([(0, 0, 0)], format=format))


@pytest.mark.parametrize("format", ["PNG", "WEBP"])
def test_rejects_animation(format):
    output = io.BytesIO()
    first = Image.new("RGB", (2, 1), "black")
    second = Image.new("RGB", (2, 1), "white")
    first.save(output, format=format, save_all=True, append_images=[second], duration=100, loop=0)

    with pytest.raises(CropError):
        crop(output.getvalue())


def test_reuses_strict_png_structure_and_checksum_validation():
    raw = encoded([(0, 0, 0)])
    corrupt = raw[:29] + bytes([raw[29] ^ 1]) + raw[30:]
    for invalid in [corrupt, raw + b"trailing bytes" + raw[-12:]]:
        with pytest.raises(CropError):
            crop(invalid)
