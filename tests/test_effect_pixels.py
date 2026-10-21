import io
import json
import struct
import zlib

import pytest
from PIL import Image

from qajev.pixels import PixelError, compare


def png(colors, *, size=None, mode="RGB"):
    image = Image.new(mode, size or (len(colors), 1))
    image.putdata(colors)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def header_png(width, height):
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", b"") + chunk(b"IEND", b"")


def read_diff(data, size):
    assert isinstance(data, bytes) and data.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG" and image.size == size
        image.load()
        return image.convert("RGB")


def test_same_images_fail_with_serializable_metrics_and_lossless_diff():
    data = png([(0, 20, 255), (100, 100, 100)])
    metrics, difference = compare(data, data, tolerance=0, min_changed=0.01)

    assert metrics == {
        "ok": False,
        "changed": 0,
        "total": 2,
        "fraction": 0.0,
        "min_changed": 0.01,
        "tolerance": 0,
        "width": 2,
        "height": 1,
    }
    assert json.loads(json.dumps(metrics, allow_nan=False)) == metrics
    assert len(set(read_diff(difference, (2, 1)).get_flattened_data())) == 1


def test_known_changed_pixels_pass_at_the_inclusive_fraction_boundary():
    off = png([(0, 0, 0)] * 4, size=(2, 2))
    on = png([(0, 0, 0), (0, 0, 0), (0, 0, 11), (0, 0, 0)], size=(2, 2))

    metrics, difference = compare(off, on, tolerance=10, min_changed=0.25)

    assert metrics["ok"] is True
    assert (metrics["changed"], metrics["total"], metrics["fraction"]) == (1, 4, 0.25)
    mask = read_diff(difference, (2, 2))
    assert mask.getpixel((0, 1)) != mask.getpixel((0, 0))
    assert mask.getpixel((0, 0)) == mask.getpixel((1, 0)) == mask.getpixel((1, 1))
    assert compare(off, on, tolerance=10, min_changed=0.25001)[0]["ok"] is False


@pytest.mark.parametrize("channel", [0, 1, 2])
def test_any_rgb_channel_strictly_above_tolerance_counts_once(channel):
    changed = [10, 10, 10]
    changed[channel] = 21
    off = png([(10, 10, 10), (10, 10, 10)])
    on = png([tuple(changed), (20, 20, 20)])

    metrics, _ = compare(off, on, tolerance=10, min_changed=0.5)

    assert metrics["changed"] == 1
    assert metrics["ok"] is True
    assert compare(on, off, tolerance=10, min_changed=0.5)[0] == metrics


def test_tolerance_and_full_fraction_boundaries():
    off = png([(0, 0, 0), (0, 0, 0)])
    on = png([(255, 255, 255), (0, 0, 1)])

    assert compare(off, on, tolerance=0, min_changed=1)[0]["ok"] is True
    metrics, _ = compare(off, on, tolerance=255, min_changed=1)
    assert metrics["changed"] == 0 and metrics["ok"] is False
    assert compare(off, on, tolerance=1, min_changed=1)[0]["ok"] is False


@pytest.mark.parametrize("value", [True, False, -1, 256, 1.0, 1.5, float("nan"), float("inf"), "1", None])
def test_rejects_invalid_tolerance(value):
    data = png([(0, 0, 0)])
    with pytest.raises(PixelError):
        compare(data, data, tolerance=value, min_changed=0.1)


@pytest.mark.parametrize("value", [True, False, 0, -0.1, 1.1, float("nan"), float("inf"), "0.1", None])
def test_rejects_invalid_min_changed(value):
    data = png([(0, 0, 0)])
    with pytest.raises(PixelError):
        compare(data, data, tolerance=0, min_changed=value)


@pytest.mark.parametrize("bad", [b"", b"not an image", b"\x89PNG\r\n\x1a\n", None, "image", bytearray(b"image")])
@pytest.mark.parametrize("side", ["off", "on"])
def test_rejects_bad_image_bytes_on_either_side(bad, side):
    valid = png([(0, 0, 0)])
    with pytest.raises(PixelError):
        compare(bad if side == "off" else valid, bad if side == "on" else valid, tolerance=0, min_changed=1)


def test_rejects_other_image_formats():
    output = io.BytesIO()
    Image.new("RGB", (1, 1)).save(output, format="JPEG")
    data = output.getvalue()
    with pytest.raises(PixelError):
        compare(data, data, tolerance=0, min_changed=1)


@pytest.mark.parametrize(
    "damage", ["missing_end", "bad_crc", "bad_end_crc", "trailing_bytes", "trailing_second_end", "truncated_pixels"]
)
def test_rejects_corrupt_png(damage):
    valid = png([(0, 0, 0), (255, 255, 255)])
    if damage == "missing_end":
        bad = valid[:-12]
    elif damage == "bad_crc":
        bad = valid[:29] + bytes([valid[29] ^ 1]) + valid[30:]
    elif damage == "bad_end_crc":
        bad = valid[:-1] + bytes([valid[-1] ^ 1])
    elif damage == "trailing_bytes":
        bad = valid + b"unexpected trailing data"
    elif damage == "trailing_second_end":
        bad = valid + b"unexpected trailing data" + valid[-12:]
    else:
        header = struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0)
        bad = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\x00"))
        bad += chunk(b"IEND", b"")
    with pytest.raises(PixelError):
        compare(valid, bad, tolerance=0, min_changed=1)


def test_rejects_unequal_and_empty_dimensions():
    with pytest.raises(PixelError):
        compare(png([(0, 0, 0)]), png([(0, 0, 0)] * 2), tolerance=0, min_changed=1)
    for width, height in [(0, 1), (1, 0), (0, 0)]:
        with pytest.raises(PixelError):
            compare(header_png(width, height), header_png(width, height), tolerance=0, min_changed=1)


@pytest.mark.parametrize("size", [(20000, 1), (1, 20000), (8192, 8192)])
def test_rejects_unbounded_dimensions_before_decoding_pixels(monkeypatch, size):
    def must_not_decode(*args, **kwargs):
        raise AssertionError("oversized input must be rejected before loading pixels")

    monkeypatch.setattr(Image.Image, "load", must_not_decode)
    data = header_png(*size)
    with pytest.raises(PixelError):
        compare(data, data, tolerance=0, min_changed=1)


def test_transparent_rgb_is_ignored_on_the_fixed_white_background():
    off = png([(255, 0, 0, 0), (0, 0, 0, 128)], mode="RGBA")
    on = png([(0, 0, 255, 0), (127, 127, 127, 255)], mode="RGBA")

    assert compare(off, on, tolerance=0, min_changed=0.5)[0]["changed"] == 0
    opaque = png([(255, 255, 255), (127, 127, 127)])
    assert compare(off, opaque, tolerance=0, min_changed=0.5)[0]["changed"] == 0


def test_palette_transparency_and_grayscale_use_the_same_rgb_comparison():
    image = Image.new("P", (1, 1))
    image.putpalette([255, 0, 0] + [0, 0, 0] * 255)
    image.info["transparency"] = 0
    output = io.BytesIO()
    image.save(output, format="PNG")

    assert compare(output.getvalue(), png([(255, 255, 255)]), tolerance=0, min_changed=1)[0]["changed"] == 0
    assert compare(png([42], mode="L"), png([(42, 42, 42)]), tolerance=0, min_changed=1)[0]["changed"] == 0
