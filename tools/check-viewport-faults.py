"""Restored, assertion-based geometry faults in isolated CPU fixture copies."""

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

UNIT = "tests/test_viewport_integrity.py::"
V, S, R, C = "qajev/viewport.py", "qajev/session.py", "qajev/runner.py", "qajev/clients.py"
NEGATIVE = "test_mismatched_window_cannot_pass_even_when_every_scripted_check_would_pass"
MOBILE = "test_mobile_layout_zoom_is_normalized_without_changing_the_image_or_device"
FAULTS = [
    ("V01-no-preflight", R, '                result["viewport"] = session.verify_viewport()',
     '                pass', NEGATIVE),
    ("V02-shots-only", R, 'if hasattr(session, "verify_viewport"):',
     'if opts.shots and hasattr(session, "verify_viewport"):', NEGATIVE),
    ("V03-width-only", V, 'for got, key in zip(effective, ("width", "height"))',
     'for got, key in zip(effective[:1], ("width",))', NEGATIVE),
    ("V04-ignore-dpr", V,
     'and math.isclose(after["devicePixelRatio"], device.get("scale", 1), rel_tol=0, abs_tol=1e-6)',
     'and True', "test_matching_layout_cannot_hide_wrong_capture_size_or_dpr"),
    ("V05-ignore-capture", V, 'and pixels == expected', 'and True',
     "test_matching_layout_cannot_hide_wrong_capture_size_or_dpr"),
    ("V06-ignore-resize", V, 'changed = before != after', 'changed = False',
     "test_a_resize_during_capture_and_a_broken_image_are_refused"),
    ("V07-ignore-visual-zoom", V, 'scale = after["visualViewport"]["scale"]', 'scale = 1', MOBILE),
    ("V08-no-layout-rounding", V, 'tolerance = .5 / device.get("scale", 1)', 'tolerance = 0', MOBILE),
    ("V09-boolean-metrics", V, 'not isinstance(value, bool) and ', '',
     "test_boolean_dpr_is_invalid_even_when_true_equals_one"),
    ("V10-default-target", S,
     'data = self.call("Page.captureScreenshot", format="png", fromSurface=True, captureBeyondViewport=False)',
     'data = self.jev.cdp("Page.captureScreenshot", format="png", fromSurface=True, captureBeyondViewport=False)',
     "test_native_capture_and_declared_device_scale_keep_valid_scenarios_passing"),
    ("V11-clients-bypass", C, '                    entry["viewport"] = c.session.verify_viewport()',
     '                    pass', "test_a_client_with_wrong_geometry_stops_the_scenario_before_any_client_action"),
    ("V12-device-cache", S, '            before = measure()',
     '            if getattr(self, "_viewport_verified", False):\n                return {}\n'
     '            self._viewport_verified = True\n            before = measure()',
     "test_each_scenario_measures_even_when_the_device_profile_is_cached"),
    ("V13-mismatch-pass", R, '                       "harness", str(e), started)',
     '                       "pass", str(e), started)', NEGATIVE),
    ("V14-passed-check-wins", "qajev/verdict.py",
     'BROKEN_STOPS = {"browser_error", "hook_failed", "guard_missing", "left_site", '
     '"interrupted", "viewport_mismatch"}',
     'BROKEN_STOPS = {"browser_error", "hook_failed", "guard_missing", "left_site", "interrupted"}',
     "test_viewport_stop_is_harness_even_if_a_check_has_already_passed"),
    ("V15-ignore-visual-width", V, 'after["visualViewport"]["width"] * scale + gutters[0]',
     'after["innerWidth"]', "test_a_narrow_visual_viewport_cannot_hide_behind_correct_layout_dimensions"),
    ("V16-ignore-scrollbar", V, 'after["visualViewport"]["width"] * scale + gutters[0]',
     'after["visualViewport"]["width"] * scale',
     "test_scrollbars_and_pinch_zoom_keep_matching_native_captures_valid"),
    ("V17-dom-rounding-is-scrollbar", V, 'after["layoutViewport"]["clientHeight"]', 'after["clientHeight"]',
     "test_dom_rounding_is_not_a_scrollbar_on_scaled_mobile_pages"),
]


def guard():
    if os.getloadavg()[1] > 16:
        raise RuntimeError("STOPPED: load5 > 16")
    if shutil.disk_usage(Path.cwd()).free < 20 * 1024**3:
        raise RuntimeError("STOPPED: disk < 20 GiB")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "linux":
        raise SystemExit("REFUSED: CPU Linux fixture job only")
    os.setpriority(os.PRIO_PROCESS, 0, 19)
    guard()
    root, out = args.source.resolve(), args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    stage = out / "source"
    shutil.copytree(root, stage, ignore=shutil.ignore_patterns(
        ".git", ".venv", ".rt", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"))
    spec = importlib.util.spec_from_file_location("viewport_fault_helpers", root / "tools/check-ux-faults.py")
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    originals = {path: (stage / path).read_bytes() for _, path, *_ in FAULTS}
    records = []
    try:
        for name, path, before, after, test in FAULTS:
            guard()
            test = UNIT + test
            control = helper.run_test(stage, test, out / (name + "-control"), "http://127.0.0.1:9")
            assert control["exit"] == 0 and control["tests"] and not control["errors"] and not control["skipped"], (
                name, "control failed", control)
            text = originals[path].decode()
            assert text.count(before) == 1, (name, "mutation anchor is not unique", text.count(before))
            try:
                (stage / path).write_text(text.replace(before, after, 1))
                failed = helper.run_test(stage, test, out / (name + "-fault"), "http://127.0.0.1:9")
                assert failed["exit"] == 1 and not failed["errors"] and not failed["skipped"] \
                    and failed["assertion_failure"], (name, "fault missed or not an assertion failure", failed)
                records.append({"fault": name, "test": test, "control": control, "fault_result": failed,
                                "caught": True})
                print(json.dumps({"fault": name, "caught": True}), flush=True)
            finally:
                (stage / path).write_bytes(originals[path])
    finally:
        for path, content in originals.items():
            (stage / path).write_bytes(content)
        (out / "RESULT.json").write_text(json.dumps({
            "caught": len(records), "total": len(FAULTS), "matrix": records,
            "gate": "PASS" if len(records) == len(FAULTS) else "FAIL",
            "sources_restored": all((stage / p).read_bytes() == b for p, b in originals.items()),
            "source_hashes": {p: hashlib.sha256((stage / p).read_bytes()).hexdigest() for p in originals},
        }, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
