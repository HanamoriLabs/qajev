"""Shared options for deterministic web UX checks; explicit run values beat suite values."""

import math
from collections.abc import Mapping

CHECKS = ("cover", "keyboard", "shift", "input-kind", "tap", "clip")
DEFAULTS = {
    "ux": "advisory",
    "ux_checks": list(CHECKS),
    "ux_keyboard_scope": "typed",
    "ux_keyboard_ratio": 0.45,
    "ux_keyboard_main": "reachable",
    "ux_keyboard_press_return": False,
    "ux_settle_ms": 750,
    "ux_settle_timeout_ms": 10000,
    "ux_shift_px": 8,
    "ux_tap_min_px": 44,
    "ux_max_findings": 10,
}
KEYS = tuple(DEFAULTS)
ENUMS = {"ux": ("off", "advisory", "strict"), "ux_keyboard_scope": ("typed", "all"),
         "ux_keyboard_main": ("reachable", "visible")}


def parse(values=None, where="ux"):
    """Keep and validate explicitly supplied UX options. None means inherit, including on Options objects."""
    if values is None:
        return {}
    values = values if isinstance(values, Mapping) else vars(values)
    out = {key: value for key, value in values.items() if key in KEYS and value is not None}
    if isinstance(out.get("ux"), bool):  # the existing smoke/MCP option was a boolean
        out["ux"] = "advisory" if out["ux"] else "off"
    for key, choices in ENUMS.items():
        if key in out and out[key] not in choices:
            raise ValueError(f"{where}: {key} must be {' | '.join(choices)}")
    if "ux_checks" in out:
        checks = out["ux_checks"]
        if isinstance(checks, str):
            checks = [value.strip() for value in checks.split(",") if value.strip()]
        if not isinstance(checks, (list, tuple)) or any(check not in CHECKS for check in checks):
            raise ValueError(f"{where}: ux_checks must list only {', '.join(CHECKS)}")
        out["ux_checks"] = list(dict.fromkeys(checks))
    if "ux_keyboard_press_return" in out and not isinstance(out["ux_keyboard_press_return"], bool):
        raise ValueError(f"{where}: ux_keyboard_press_return must be true or false")
    for key in ("ux_keyboard_ratio", "ux_shift_px", "ux_tap_min_px"):
        if key not in out:
            continue
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{where}: {key} must be a finite number")
        if key == "ux_keyboard_ratio" and not 0 < value < 1:
            raise ValueError(f"{where}: {key} must be greater than 0 and less than 1")
        if key == "ux_shift_px" and value < 0:
            raise ValueError(f"{where}: {key} must be at least 0")
        if key == "ux_tap_min_px" and value <= 0:
            raise ValueError(f"{where}: {key} must be greater than 0")
    for key, minimum in (("ux_settle_ms", 0), ("ux_settle_timeout_ms", 0), ("ux_max_findings", 1)):
        if key in out and (isinstance(out[key], bool) or not isinstance(out[key], int) or out[key] < minimum):
            raise ValueError(f"{where}: {key} must be a whole number of at least {minimum}")
    if out.get("ux_settle_timeout_ms", 0) > 10000:
        raise ValueError(f"{where}: ux_settle_timeout_ms must be at most 10000")
    return out


def resolve(opts=None, inherited=None):
    """Return all spec options, with run flags > inherited scenario/suite values > defaults."""
    return parse({**DEFAULTS, **parse(inherited), **parse(opts)})


def cli_args(values=None):
    """Serialize explicit values only: adding defaults here would override a suite's own options."""
    out = []
    for key, value in parse(values).items():
        flag = "--" + key.replace("_", "-")
        if key == "ux_keyboard_press_return":
            out.append(flag if value else "--no-ux-keyboard-press-return")
        else:
            out += [flag, ",".join(value) if key == "ux_checks" else str(value)]
    return out
