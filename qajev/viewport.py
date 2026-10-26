"""Check page geometry against native capture pixels before scenario work."""

import io
import math

from PIL import Image

MEASURE = """({innerWidth,innerHeight,devicePixelRatio,
  clientWidth:document.documentElement.clientWidth,clientHeight:document.documentElement.clientHeight,
  visualViewport:visualViewport?{width:visualViewport.width,height:visualViewport.height,
    scale:visualViewport.scale}:null})"""


class ViewportMismatch(RuntimeError):
    def __init__(self, reason, evidence):
        super().__init__(reason)
        self.evidence = evidence


def _positive(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def verify(before, after, raw, device, *, target=None, session=None):
    """CSS layout can be scaled on mobile; compare native pixels after accounting for visual zoom and DPR."""
    evidence = {"requested": dict(device) if isinstance(device, dict) else None,
                "before": before, "after": after, "target": target, "session": session, "capture_pixels": None}

    def refuse(reason):
        raise ViewportMismatch(reason, evidence)

    if (not isinstance(device, dict) or not all(_positive(device.get(k)) for k in ("width", "height"))
            or not _positive(device.get("scale", 1))):
        refuse("viewport geometry unavailable: no valid requested device")
    for view in (before, after):
        if (not isinstance(view, dict)
                or not all(_positive(view.get(k)) for k in (
                    "innerWidth", "innerHeight", "clientWidth", "clientHeight", "devicePixelRatio"))
                or not isinstance(view.get("visualViewport"), dict)
                or not all(_positive(view["visualViewport"].get(k)) for k in ("width", "height", "scale"))
                or not isinstance(view.get("layoutViewport"), dict)
                or not all(_positive(view["layoutViewport"].get(k)) for k in ("clientWidth", "clientHeight"))):
            refuse("viewport geometry unavailable: page dimensions or visual scale could not be measured")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            pixels = image.size
    except (OSError, ValueError, TypeError) as error:
        raise ViewportMismatch("viewport geometry unavailable: no valid captured image", evidence) from error
    evidence["capture_pixels"] = list(pixels)
    scale = after["visualViewport"]["scale"]
    # CDP layout dimensions avoid the DOM's floor/ceil disagreement on scaled mobile pages.
    gutters = (max(0, after["innerWidth"] - after["layoutViewport"]["clientWidth"]),
               max(0, after["innerHeight"] - after["layoutViewport"]["clientHeight"]))
    effective = (after["visualViewport"]["width"] * scale + gutters[0],
                 after["visualViewport"]["height"] * scale + gutters[1])
    evidence["scrollbar_css"] = list(gutters)
    evidence["effective_css"] = list(effective)
    expected = tuple(int(math.floor(device[k] * device.get("scale", 1) + .5)) for k in ("width", "height"))
    changed = before != after
    # Fractional CSS dimensions and visual scale can round by less than half a native capture pixel.
    tolerance = .5 / device.get("scale", 1)
    matches = (all(abs(got - device[key]) <= tolerance for got, key in zip(effective, ("width", "height")))
               and math.isclose(after["devicePixelRatio"], device.get("scale", 1), rel_tol=0, abs_tol=1e-6)
               and pixels == expected)
    if changed or not matches:
        reason = (f"viewport mismatch: page {after['innerWidth']:g} x {after['innerHeight']:g} CSS px, "
                  f"visual {after['visualViewport']['width']:g} x {after['visualViewport']['height']:g} "
                  f"at scale {scale:g}, DPR {after['devicePixelRatio']:g}; "
                  f"capture {pixels[0]} x {pixels[1]} px; requested {device['width']:g} x {device['height']:g} "
                  f"CSS px at {device.get('scale', 1):g}x")
        if changed:
            reason += "; page geometry changed during capture"
        refuse(reason)
    return evidence
