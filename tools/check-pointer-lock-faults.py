"""Assertion-based pointer-lock preparation faults; CPU Linux fixtures only."""

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

UNIT = "tests/test_pointer_lock.py::"
CONDITION = ('if method == "Input.dispatchMouseEvent" and params.get("type") == "mousePressed" '
             'and session_id in sessions:')
ACTIVATE = '            raw_cdp("Page.bringToFront", session_id=session_id)\n'
FORWARD = '        return raw_cdp(method, session_id=session_id, **params)\n'
EXACT = "test_headless_mouse_press_activates_the_exact_target_before_unchanged_input"
LIFECYCLE = "test_real_session_lifecycle_registers_only_headless_targets_and_removes_closed_ones"
FAULTS = [
    ("P01-no-activation", ACTIVATE, '            pass\n', EXACT),
    ("P02-default-target", ACTIVATE, '            raw_cdp("Page.bringToFront")\n', EXACT),
    ("P03-after-input", '        ' + CONDITION + '\n' + ACTIVATE + FORWARD,
     '        result = raw_cdp(method, session_id=session_id, **params)\n        ' + CONDITION + '\n' +
     ACTIVATE + '        return result\n', EXACT),
    ("P04-headed-activation", ' and session_id in sessions:', ':',
     "test_unregistered_or_headed_sessions_keep_the_existing_focus_policy"),
    ("P05-all-mouse-events", ' and params.get("type") == "mousePressed"', '',
     "test_non_press_commands_are_not_activated_or_rewritten"),
    ("P06-swallowed-runtime", ACTIVATE,
     '            try:\n                raw_cdp("Page.bringToFront", session_id=session_id)\n'
     '            except RuntimeError:\n                pass\n', "test_failed_activation_sends_no_mouse_input"),
    ("P07-swallowed-timeout", ACTIVATE,
     '            try:\n                raw_cdp("Page.bringToFront", session_id=session_id)\n'
     '            except TimeoutError:\n                pass\n', "test_failed_activation_sends_no_mouse_input"),
    ("P08-dropped-input-arguments", FORWARD, '        return raw_cdp(method, session_id=session_id)\n', EXACT),
    ("P09-disabled-wiring",
     '_headless_mouse_cdp(_patient_cdp(jev_browser.cdp, timeout, transport), _HEADLESS_INPUT_SESSIONS)',
     '_patient_cdp(jev_browser.cdp, timeout, transport)',
     "test_load_uses_one_prepared_dispatcher_for_hooks_and_jev_direct_input"),
    ("P10-no-registration", '                _HEADLESS_INPUT_SESSIONS.add(input_session)\n', '                pass\n',
     LIFECYCLE),
    ("P11-register-headed", 'if self.headless and (input_session := getattr(self.browser, "session", None)):',
     'if input_session := getattr(self.browser, "session", None):', LIFECYCLE),
    ("P12-closed-session-retained", '            _HEADLESS_INPUT_SESSIONS.discard(input_session)\n',
     '            pass\n', LIFECYCLE),
    ("P13-no-headless-detection", 'self.headless = "Headless" in version.get("product", "")',
     'self.headless = False', LIFECYCLE),
    ("P14-only-first-press", CONDITION + '\n' + ACTIVATE,
     CONDITION + '\n' + ACTIVATE + '            sessions.discard(session_id)\n',
     "test_repeated_presses_reactivate_after_another_tab_took_the_foreground"),
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
    spec = importlib.util.spec_from_file_location("pointer_fault_helpers", root / "tools/check-ux-faults.py")
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    path = stage / "qajev/session.py"
    original = path.read_bytes()
    records = []
    try:
        for name, before, after, test in FAULTS:
            guard()
            test = UNIT + test
            control = helper.run_test(stage, test, out / (name + "-control"), "http://127.0.0.1:9")
            assert control["exit"] == 0 and control["tests"] and not control["skipped"], (
                name, "control failed", str(out / (name + "-control.log")), control)
            text = original.decode()
            assert text.count(before) == 1, (name, "mutation anchor is not unique", text.count(before))
            try:
                path.write_text(text.replace(before, after, 1))
                failed = helper.run_test(stage, test, out / (name + "-fault"), "http://127.0.0.1:9")
                assert failed["exit"] == 1 and not failed["errors"] and not failed["skipped"] \
                    and failed["assertion_failure"], (name, "fault missed", str(out / (name + "-fault.log")), failed)
                records.append({"fault": name, "test": test, "control": control, "fault_result": failed,
                                "caught": True})
                print(json.dumps({"fault": name, "caught": True}), flush=True)
            finally:
                path.write_bytes(original)
    finally:
        path.write_bytes(original)
        (out / "RESULT.json").write_text(json.dumps({
            "caught": len(records), "total": len(FAULTS), "matrix": records,
            "gate": "PASS" if len(records) == len(FAULTS) else "FAIL",
            "sources_restored": path.read_bytes() == original,
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
