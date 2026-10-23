"""Run UX faults on private source copies and one owned Linux-only Chrome.

Each fault follows a passing named control. An assertion failure (not collection,
syntax or fixture setup failure) is required. No models or live service is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

JS = "qajev/ux-probe.js"
PY = "qajev/ux_checks.py"
DOM = "tests/test_ux_dom_live.py::"
UNIT = "tests/test_ux_checks.py::"
WIRE = "tests/test_ux_wires.py::"


def row(name, test, path, before, after, *more):
    return {"id": name, "test": test, "edits": [(path, before, after), *more]}


FAULTS = [
    row("I04-search-type", DOM + "test_search_fields_keep_text_keyboards_without_hiding_numeric_controls",
        JS, 'el.type === "search" || el.getAttribute("role") === "searchbox"',
        'el.getAttribute("role") === "searchbox"'),
    row("I05-search-role", DOM + "test_search_fields_keep_text_keyboards_without_hiding_numeric_controls",
        JS, 'el.type === "search" || el.getAttribute("role") === "searchbox" ||', 'el.type === "search" ||'),
    row("I06-search-label", DOM + "test_search_fields_keep_text_keyboards_without_hiding_numeric_controls",
        JS, '/\\bsearch\\b/i.test(label)', 'false'),
    row("I07-search-guard", DOM + "test_search_fields_keep_text_keyboards_without_hiding_numeric_controls",
        JS, '&& !search &&', '&&'),
    row("K11-neutral-controls", DOM + "test_keyboard_neutral_point_never_clicks_backdrop_controls_even_past_the_cap",
        JS, 'isControl(p) || p.tagName === "LABEL"', 'p.tagName === "LABEL"'),
    row("K12-default-return", DOM + "test_default_keyboard_probe_sends_no_enter_event_or_app_send_action",
        "qajev/ux_options.py", '"ux_keyboard_press_return": False,', '"ux_keyboard_press_return": True,'),
    row("K13-desktop-width", DOM + "test_typed_desktop_keyboard_probe_makes_no_click_or_metrics_change",
        PY, '        if width > 500:\n            self.skipped', '        if False:\n            self.skipped'),
    row("E01-crop-context", DOM + "test_inserted_tray_shifts_other_control_and_names_cause", JS,
        "for (const r of regions) if (area(r) > 0)", "for (const r of []) if (area(r) > 0)"),
    row("E02-old-outline", DOM + "test_inserted_tray_shifts_other_control_and_names_cause", JS,
        "if (Array.isArray(spec.beforeRect)) outline", "if (false) outline"),
    row("E03-current-outline", DOM + "test_inserted_tray_shifts_other_control_and_names_cause", JS,
        "if (el) outline(rect(el.getBoundingClientRect())", "if (false) outline(rect(el.getBoundingClientRect())"),
    row("E04-cause-outline", DOM + "test_inserted_tray_shifts_other_control_and_names_cause", JS,
        "if (cause) outline", "if (false) outline"),
    row("E05-cover-outline", DOM + "test_covered_control_has_exact_geometry_and_first_crop", JS,
        "if (cover) outline", "if (false) outline"),
    row(
        "D01-cover",
        DOM + "test_covered_control_has_exact_geometry_and_first_crop",
        JS,
        "add(coverFinding(el, covered))",
        "add(null)",
    ),
    row(
        "D02-keyboard-focus",
        DOM + "test_hidden_focused_field_is_reported_and_metrics_are_restored",
        JS,
        'if (!ignored(field, "UX-KEYBOARD-FOCUS")',
        'if (false && !ignored(field, "UX-KEYBOARD-FOCUS")',
    ),
    row(
        "D03-keyboard-main",
        DOM + "test_keyboard_real_resize_and_tap_outside_guard_find_both_faults",
        JS,
        "if (main && main.isConnected && main !== field",
        "if (false && main && main.isConnected && main !== field",
    ),
    row(
        "D04-keyboard-dismiss",
        DOM + "test_keyboard_real_resize_and_tap_outside_guard_find_both_faults",
        JS,
        "if (opts.dismissFailed &&",
        "if (false && opts.dismissFailed &&",
    ),
    row(
        "D05-shift",
        DOM + "test_inserted_tray_shifts_other_control_and_names_cause",
        JS,
        "if (Math.max(Math.abs(dx), Math.abs(dy)) > minimum)",
        "if (false && Math.max(Math.abs(dx), Math.abs(dy)) > minimum)",
    ),
    row(
        "D06-date",
        DOM + "test_near_label_date_and_money_use_the_correct_input_modes",
        JS,
        'if ((!check || check === "UX-INPUT-DATE")',
        'if (false && (!check || check === "UX-INPUT-DATE")',
    ),
    row(
        "D07-number",
        DOM + "test_near_label_date_and_money_use_the_correct_input_modes",
        JS,
        'if ((!check || check === "UX-INPUT-NUMBER")',
        'if (false && (!check || check === "UX-INPUT-NUMBER")',
    ),
    row(
        "D08-tap",
        DOM + "test_small_target_and_primary_ellipsis_each_have_a_crop",
        JS,
        "if (r.width >= minimum - 0.5 && r.height >= minimum - 0.5)",
        "if (true)",
    ),
    row(
        "D09-clip",
        DOM + "test_small_target_and_primary_ellipsis_each_have_a_crop",
        JS,
        "if (!primary || (!cutX && !cutY))",
        "if (true)",
    ),
    row(
        "C01-center",
        DOM + "test_cover_center_or_two_inset_points_boundary",
        JS,
        "if (!covered.some(point => point.center) && covered.length < 2)",
        "if (covered.length < 2)",
    ),
    row(
        "C02-two-insets",
        DOM + "test_cover_center_or_two_inset_points_boundary",
        JS,
        "covered.length < 2",
        "covered.length < 3",
    ),
    row(
        "C03-modal",
        DOM + "test_cover_skips_other_screens_and_live_notices",
        JS,
        ">= 0.8 * innerWidth * innerHeight",
        ">= 2 * innerWidth * innerHeight",
    ),
    row(
        "C04-notice",
        DOM + "test_cover_skips_other_screens_and_live_notices",
        JS,
        'if (["status", "alert"].includes(p.getAttribute("role")) || p.hasAttribute("aria-live"))',
        "if (false)",
    ),
    row(
        "C05-animation-retest",
        DOM + "test_cover_rechecks_after_animation_and_does_not_report_transient_overlay",
        JS,
        "await delay(1000)",
        "await delay(0)",
    ),
    row(
        "C06-scroll-retest",
        WIRE + "test_rendered_cover_scroll_retest_avoids_a_control_under_a_fixed_header",
        JS,
        'el.scrollIntoView({block: "center", inline: "nearest", behavior: "instant"});',
        "void 0;",
    ),
    row(
        "K01-hidden-container-scroll",
        WIRE + "test_rendered_hidden_container_cannot_make_the_main_button_finger_reachable",
        JS,
        '["auto", "scroll", "overlay"].includes(css(p).overflowY)',
        '["auto", "scroll", "overlay", "hidden"].includes(css(p).overflowY)',
    ),
    row(
        "K02-native-blur",
        DOM + "test_keyboard_real_resize_and_tap_outside_guard_find_both_faults",
        JS,
        "mouse: event => event.preventDefault()",
        "mouse: event => void event",
    ),
    row(
        "K03-textarea-return",
        DOM + "test_textarea_return_hint_alone_does_not_prove_keyboard_dismissal",
        JS,
        'return_eligible: field.tagName !== "TEXTAREA" &&',
        "return_eligible:",
    ),
    row(
        "S01-scroll-coordinates",
        DOM + "test_scroll_is_not_a_content_shift_and_eight_pixels_is_exclusive",
        JS,
        "function contentPosition(el) {\n    const r = rect(el.getBoundingClientRect());",
        "function contentPosition(el) {\n    const r = rect(el.getBoundingClientRect());\n"
        "    return {x:r.x,y:r.y,scrollX:0,scrollY:0};",
    ),
    row(
        "S02-action-target",
        WIRE + "test_rendered_action_target_is_excluded_from_shift",
        JS,
        "const all = controlCandidates().filter(el => !target || "
        "!(el === target || el.contains(target) || target.contains(el)));",
        "const all = controlCandidates();",
    ),
    row(
        "S03-screen-transition",
        DOM + "test_whole_screen_translation_is_not_an_action_layout_fault",
        JS,
        "if (saved.tracked.length >= 4 &&",
        "if (false && saved.tracked.length >= 4 &&",
    ),
    row(
        "S04-eight-pixel-boundary",
        DOM + "test_scroll_is_not_a_content_shift_and_eight_pixels_is_exclusive",
        JS,
        "Math.max(Math.abs(dx), Math.abs(dy)) > minimum",
        "Math.max(Math.abs(dx), Math.abs(dy)) >= minimum",
    ),
    row(
        "I01-readonly-picker",
        DOM + "test_picker_phone_and_numeric_keyboard_exemptions",
        JS,
        '["text", "search"].includes(el.type) || el.readOnly ||',
        '["text", "search"].includes(el.type) ||',
        (JS, '["text", "search"].includes(el.type) && !el.readOnly &&', '["text", "search"].includes(el.type) &&'),
    ),
    row(
        "I02-phone-account",
        DOM + "test_picker_phone_and_numeric_keyboard_exemptions",
        JS,
        "&& !notNumber.test(label) &&",
        "&&",
    ),
    row(
        "I03-keyboard-mode",
        DOM + "test_picker_phone_and_numeric_keyboard_exemptions",
        JS,
        '!ignored(el, "UX-INPUT-NUMBER") && !["numeric", "decimal", "tel"].includes(mode)',
        '!ignored(el, "UX-INPUT-NUMBER")',
    ),
    row(
        "T01-label-union",
        DOM + "test_tap_rounding_checkbox_label_union_and_inline_sentence_exemption",
        JS,
        'if (el.tagName === "INPUT" && ["checkbox", "radio"].includes(el.type) && el.labels)',
        "if (false)",
    ),
    row(
        "T02-tap-boundary",
        DOM + "test_tap_rounding_checkbox_label_union_and_inline_sentence_exemption",
        JS,
        "r.width >= minimum - 0.5 && r.height >= minimum - 0.5",
        "r.width >= minimum && r.height >= minimum",
    ),
    row(
        "L01-secondary-ellipsis",
        DOM + "test_secondary_ellipsis_is_allowed_but_partial_hard_clip_is_reported",
        JS,
        "if (!primary || (!cutX && !cutY))",
        "if (!cutX && !cutY)",
    ),
    row(
        "L02-hard-clip",
        DOM + "test_secondary_ellipsis_is_allowed_but_partial_hard_clip_is_reported",
        JS,
        "if (hidden <= 2 || visible <= 0 || visible >= 0.98)",
        "if (true)",
    ),
    row(
        "O01-overlay-mutations",
        DOM + "test_overlay_mark_and_unmark_do_not_count_as_page_mutations",
        JS,
        ".filter(node => !isOverlay(node))",
        ".filter(node => true)",
    ),
    row(
        "B01-incomplete-status",
        WIRE + "test_rendered_scan_cap_names_every_remaining_control_and_field",
        JS,
        '    output.status = "incomplete";\n    output.skipped.push',
        "    void 0;\n    output.skipped.push",
    ),
    row(
        "B02-unchecked-fields",
        DOM + "test_input_kind_ten_replays_recover_one_forced_scan_budget_pause",
        JS,
        'reason: "scan incomplete: " + reason, unchecked}',
        'reason: "scan incomplete: " + reason, unchecked: []}',
    ),
    row(
        "B03-shift-incomplete",
        WIRE + "test_rendered_shift_cap_and_budget_name_unchecked_controls",
        JS,
        'incomplete(output, "UX-SHIFT", reason, pending);',
        "void 0;",
    ),
    row(
        "W01-strict",
        UNIT + "test_advisory_never_changes_verdict_and_strict_fails",
        PY,
        'if self.config["ux"] == "strict" and self.result["findings"]:',
        "if False:",
    ),
    row(
        "W02-dedupe",
        UNIT + "test_static_dedupe_keeps_first_crop_and_counts_repeats",
        PY,
        "previous = self.seen.get(key)",
        "previous = None",
    ),
    row(
        "W03-per-id-cap",
        UNIT + "test_cap_is_per_id_and_reports_omitted_findings",
        PY,
        'if self.counts[check] >= self.config["ux_max_findings"]:',
        "if False:",
    ),
    row(
        "W04-restore-metrics",
        UNIT + "test_keyboard_restores_metrics_focus_scroll_and_listeners_on_failure",
        PY,
        'width=saved["width"], height=saved["height"],',
        'width=saved["width"], height=100,',
    ),
    row(
        "W05-clear-metrics",
        WIRE + "test_keyboard_failure_clears_override_when_no_original_metrics_exist",
        PY,
        'self.session.call("Emulation.clearDeviceMetricsOverride")',
        'self.session.call("Page.bringToFront")',
    ),
    row(
        "W06-unmark-finally",
        UNIT + "test_crop_always_removes_overlay_on_failed_capture",
        PY,
        'self.probe("unmark")',
        'self.probe("state")',
    ),
    row(
        "W07-attach",
        WIRE + "test_attach_installs_the_probe_before_new_documents_without_model_calls",
        PY,
        'session.ux_script_id = session.call("Page.addScriptToEvaluateOnNewDocument", source=SOURCE).get("identifier")',
        'session.ux_script_id = "missing"',
    ),
    row(
        "W08-post-action-quiet",
        WIRE + "test_post_action_waits_for_quiet_before_shift_and_static_measurements",
        PY,
        "began = time.monotonic()\n            self.settle()",
        "began = time.monotonic()",
    ),
    row(
        "W09-runner-start",
        WIRE + "test_suite_runner_attaches_starts_finishes_and_applies_deterministic_observer",
        "qajev/runner.py",
        "ux_observer.safe(ux_observer.start)",
        "pass",
    ),
    row(
        "W10-runner-apply",
        WIRE + "test_suite_runner_attaches_starts_finishes_and_applies_deterministic_observer",
        "qajev/runner.py",
        "        ux_observer.apply(result)\n    return result",
        "        pass\n    return result",
    ),
    row(
        "W11-smoke-apply",
        WIRE + "test_smoke_page_starts_and_applies_deterministic_observer",
        "qajev/smoke.py",
        "observer.apply(result)",
        "pass",
    ),
    row(
        "W12-hook-boundary",
        WIRE + "test_session_hook_wraps_the_user_action_and_runs_after_even_on_failure",
        "qajev/session.py",
        'with self.ux_action(None, action=f"{kind} {value}" if kind == "key" else kind):',
        "with contextlib.nullcontext():",
    ),
    row(
        "W13-fill-boundary",
        WIRE + "test_session_fill_probes_keyboard_before_relocating_and_typing",
        "qajev/session.py",
        'with self.ux_action({"x": found["x"], "y": found["y"]}, typing=True, action=f"type into {selector}"):',
        "with contextlib.nullcontext():",
    ),
    row(
        "W14-jev-boundary",
        WIRE + "test_jev_act_uses_chosen_node_and_typing_boundary_without_constructing_a_model",
        "qajev/session.py",
        "with boundary:",
        "with contextlib.nullcontext():",
    ),
    row(
        "W15-guard-coverage",
        UNIT + "test_guard_changed_controls_are_unchecked_without_weakening_the_guard",
        PY,
        'if isinstance(getattr(self.session, "guard_cfg", None), dict):\n            guarded =',
        "if False:\n            guarded =",
    ),
    row(
        "W16-no-model",
        WIRE + "test_static_observer_does_not_request_a_model",
        PY,
        "    def scan(self):\n",
        '    def scan(self):\n        self.session.agent.command("predict")\n',
    ),
    row(
        "O02-ignore-attributes",
        DOM + "test_app_ignore_attributes_suppress_only_the_named_checks",
        JS,
        'if (ignores.includes("all") || ignores.includes(check))',
        "if (false)",
    ),
    row(
        "W17-terminal-ux",
        "tests/test_ux_console.py::test_terminal_verdict_keeps_the_ux_count_or_incomplete_status_on_stderr",
        "qajev/cli.py",
        '(f" · {ux}" if ux else "")',
        '""',
    ),
    row(
        "W18-signin-wall-finalize",
        WIRE + "test_suite_runner_attaches_starts_finishes_and_applies_deterministic_observer",
        "qajev/runner.py",
        "        if ux_observer:\n            ux_observer.safe(ux_observer.finish)\n"
        "            ux_observer.apply(result)",
        "        if False:\n            ux_observer.safe(ux_observer.finish)\n            ux_observer.apply(result)",
    ),
    row(
        "W19-smoke-post-legacy-finalize",
        WIRE + "test_smoke_page_starts_and_applies_deterministic_observer",
        "qajev/smoke.py",
        "observer.safe(observer.finish)",
        "pass",
    ),
    row(
        "W20-keyboard-guard-helper",
        UNIT + "test_keyboard_scope_all_requires_the_guard_before_any_probe_input",
        PY,
        "            self.session.require_guard()",
        "            pass",
    ),
    row(
        "W21-keyboard-tap-guard",
        WIRE + "test_keyboard_rechecks_the_current_guard_before_each_input_path",
        PY,
        'if w > 0 and h > 0 and field.get("center_clear", True):\n                self.require_guard()',
        'if w > 0 and h > 0 and field.get("center_clear", True):\n                pass',
    ),
    row(
        "W22-keyboard-focus-guard",
        WIRE + "test_keyboard_rechecks_the_current_guard_before_each_input_path",
        PY,
        'if not focused.get("active_is_field") and not field.get("center_clear", True):\n'
        "                self.require_guard()",
        'if not focused.get("active_is_field") and not field.get("center_clear", True):\n                pass',
    ),
    row(
        "W23-keyboard-return-guard",
        WIRE + "test_keyboard_rechecks_the_current_guard_before_each_input_path",
        PY,
        'if self.config["ux_keyboard_press_return"]:\n                    self.require_guard()',
        'if self.config["ux_keyboard_press_return"]:\n                    pass',
    ),
    row(
        "W24-keyboard-outside-guard",
        WIRE + "test_keyboard_rechecks_the_current_guard_before_each_input_path",
        PY,
        "if point:\n                    self.require_guard()",
        "if point:\n                    pass",
    ),
    row(
        "W25-clear-tap-fallback",
        UNIT + "test_a_clear_tap_that_does_not_focus_is_skipped_without_forcing_focus",
        PY,
        'if not focused.get("active_is_field") and not field.get("center_clear", True):',
        'if not focused.get("active_is_field"): ',
    ),
    row(
        "W26-missing-crop-status",
        UNIT + "test_missing_crop_is_incomplete_and_strict_refuses_missing_evidence",
        PY,
        'if not item["screenshot"]:\n                self.result["status"] = "incomplete"',
        'if not item["screenshot"]:\n                pass',
    ),
    row(
        "W27-client-incomplete-status",
        "tests/test_ux_clients.py::test_incomplete_client_scan_is_preserved_and_strict_cannot_pass",
        "qajev/clients.py",
        '"status": "incomplete" if any(ux.get("status") == "incomplete" for _, ux in ux_results)\n'
        '                        else "complete",',
        '"status": "complete",',
    ),
    row(
        "W28-client-strict-incomplete",
        "tests/test_ux_clients.py::test_incomplete_client_scan_is_preserved_and_strict_cannot_pass",
        "qajev/clients.py",
        'if result["ux"]["mode"] == "strict" and result["ux"]["status"] == "incomplete":',
        "if False:",
    ),
    row(
        "W29-click-coordinate-boundary",
        WIRE + "test_session_click_uses_the_resolved_hit_point_for_its_ux_boundary",
        "qajev/session.py",
        'with self.ux_action({"x": found["x"], "y": found["y"]}, action=f"click {value}"):',
        'with self.ux_action(value, action=f"click {value}"):',
    ),
    row(
        "W30-fill-coordinate-boundary",
        WIRE + "test_session_fill_probes_keyboard_before_relocating_and_typing",
        "qajev/session.py",
        'with self.ux_action({"x": found["x"], "y": found["y"]}, typing=True, action=f"type into {selector}"):',
        'with self.ux_action(selector, typing=True, action=f"type into {selector}"):',
    ),
    row(
        "W31-click-at-coordinate-boundary",
        WIRE + "test_session_click_at_uses_the_resolved_hit_point_for_selector_and_js",
        "qajev/session.py",
        'with self.ux_action({"x": at["x"], "y": at["y"]}, action=what):',
        'with self.ux_action({"x": point["x"], "y": point["y"]}, action=what):',
    ),
    row(
        "W32-jev-nonaction-boundary",
        WIRE + "test_jev_act_uses_chosen_node_and_typing_boundary_without_constructing_a_model",
        "qajev/session.py",
        'if action.get("kind") in {"fill", "click", "key", "press"} else contextlib.nullcontext()',
        "if True else contextlib.nullcontext()",
    ),
    row(
        "W33-ignored-keyboard-input",
        DOM + "test_ignored_keyboard_checks_do_not_trigger_their_app_handlers",
        JS,
        'if (["UX-KEYBOARD-FOCUS", "UX-KEYBOARD-MAIN", "UX-KEYBOARD-DISMISS"].every(check => ignored(field, check)))',
        "if (false)",
    ),
    row(
        "W34-ignored-keyboard-dismiss",
        DOM + "test_ignored_keyboard_checks_do_not_trigger_their_app_handlers",
        PY,
        'dismissed = bool(opened.get("done")) or bool(opened.get("dismiss_ignored"))',
        'dismissed = bool(opened.get("done"))',
    ),
    row(
        "W35-ignored-main-scroll",
        WIRE + "test_rendered_ignored_main_check_never_scrolls_the_form",
        JS,
        '&& !mainIgnored && mode !== "visible"',
        '&& mode !== "visible"',
    ),
    row(
        "W36-three-label-dedupe",
        UNIT + "test_keyboard_dedupe_merges_three_field_labels_once_and_keeps_first_crop",
        PY,
        'previous["message"] = self.messages[key].replace(labels[0], ", ".join(labels), 1)',
        'previous["message"] = previous["message"].replace(labels[0], ", ".join(labels), 1)',
    ),
    row(
        "K04-done-control",
        WIRE + "test_rendered_done_control_or_explicit_dismiss_marker_is_a_non_destructive_pass",
        JS,
        "/^(done|hide keyboard|close keyboard|dismiss keyboard)$/i.test(name(el))",
        "false",
    ),
    row(
        "K05-dismiss-marker",
        WIRE + "test_rendered_done_control_or_explicit_dismiss_marker_is_a_non_destructive_pass",
        JS,
        'el.hasAttribute("data-qa-dismiss-keyboard")',
        "false",
    ),
    row(
        "K06-main-must-be-visible",
        WIRE + "test_rendered_main_button_policy_distinguishes_reachable_from_visible",
        JS,
        '&& mode !== "visible" ?',
        "&& true ?",
    ),
    row(
        "K07-main-reachable",
        WIRE + "test_rendered_main_button_policy_distinguishes_reachable_from_visible",
        JS,
        'main && main.isConnected && !mainIgnored && mode !== "visible"',
        "false",
    ),
    row(
        "K08-return-actual-press",
        WIRE + "test_rendered_return_requires_actual_blur_and_prevents_form_submission",
        PY,
        'if self.config["ux_keyboard_press_return"]:',
        "if False:",
    ),
    row(
        "K09-return-submit-guard",
        WIRE + "test_rendered_return_requires_actual_blur_and_prevents_form_submission",
        JS,
        'if (opts.submit) window.addEventListener("submit", dismiss.submit, {capture: true, once: true});',
        'if (false) window.addEventListener("submit", dismiss.submit, {capture: true, once: true});',
    ),
    row(
        "W37-client-strict-findings",
        "tests/test_ux_clients.py::test_incomplete_client_scan_is_preserved_and_strict_cannot_pass",
        "qajev/clients.py",
        'if result["ux"]["mode"] == "strict" and result["ux"]["findings"]:',
        "if False:",
    ),
    row(
        "K10-hidden-document-root",
        WIRE + "test_rendered_hidden_document_root_is_never_programmatically_scrolled",
        JS,
        "if (!rootBlocked && root &&",
        "if (root &&",
    ),
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_test(source, test, out, endpoint):
    env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "TMPDIR", "PYTHONPATH") if key in os.environ}
    env.update(PYTHONDONTWRITEBYTECODE="1", QAJEV_UX_CDP=endpoint, QAJEV_UX_ALLOW_HEADLESS="1")
    env["PYTHONPATH"] = str(source) + os.pathsep + env.get("PYTHONPATH", "")
    command = [
        sys.executable,
        "-m",
        "pytest",
        test,
        "-q",
        "--tb=short",
        f"--junitxml={out}.xml",
        f"--basetemp={out}-tmp",
    ]
    with Path(str(out) + ".log").open("wb") as stream:
        result = subprocess.run(command, cwd=source, env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=180)
    xml = ET.parse(str(out) + ".xml")
    cases = xml.findall(".//testcase")
    failures = [
        {"name": node.get("name"), "message": failure.get("message"), "text": failure.text}
        for node in cases
        for failure in node.findall("failure")
    ]
    errors = [node.get("name") for node in cases if node.find("error") is not None]
    return {
        "exit": result.returncode,
        "tests": len(cases),
        "skipped": sum(node.find("skipped") is not None for node in cases),
        "failures": failures,
        "errors": errors,
        "assertion_failure": any(
            "assert " in (f["text"] or "")
            or "AssertionError" in (f["text"] or "")
            or "DID NOT RAISE" in (f["text"] or "")
            for f in failures
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--chrome", type=Path, required=True)
    parser.add_argument("--only", help="comma-separated fault IDs")
    args = parser.parse_args()
    if sys.platform != "linux":
        raise SystemExit("REFUSED: faults/browser are restricted to the approved Linux box")
    os.setpriority(os.PRIO_PROCESS, 0, 19)
    if os.getloadavg()[0] > 60:
        raise SystemExit("REFUSED: load1 exceeds 60")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    source = out / "source"
    shutil.copytree(
        args.source,
        source,
        ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", "node_modules", "*.pyc"),
    )
    chosen = set(args.only.split(",")) if args.only else None
    faults = [fault for fault in FAULTS if chosen is None or fault["id"] in chosen]
    if chosen and chosen != {fault["id"] for fault in faults}:
        raise SystemExit("Unknown fault ID")
    paths = {relative for fault in faults for relative, _, _ in fault["edits"]}
    originals = {relative: (source / relative).read_bytes() for relative in paths}
    hashes = {relative: digest(source / relative) for relative in paths}
    test_paths = {fault["test"].partition("::")[0] for fault in faults}
    test_paths.add("tests/test_ux_wires.py")
    record = {
        "faults": [],
        "source_hashes": hashes,
        "test_hashes": {relative: digest(source / relative) for relative in test_paths},
        "driver_sha256": digest(Path(__file__)),
        "model_calls": 0,
        "profile": str(out / "profile"),
    }
    chrome = None
    try:
        with (out / "chrome.log").open("wb") as stream:
            chrome = subprocess.Popen(
                [
                    str(args.chrome),
                    "--headless=new",
                    "--disable-gpu",
                    "--remote-debugging-port=0",
                    f"--user-data-dir={out / 'profile'}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-networking",
                    "about:blank",
                ],
                stdout=stream,
                stderr=stream,
            )
        record["chrome_pid"] = chrome.pid
        deadline = time.monotonic() + 20
        active_port = out / "profile" / "DevToolsActivePort"
        while not active_port.exists():
            if chrome.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Owned Chrome failed to start")
            time.sleep(0.1)
        endpoint = "http://127.0.0.1:" + active_port.read_text().splitlines()[0]
        with urllib.request.urlopen(endpoint + "/json/version", timeout=5) as response:
            record["browser"] = json.load(response)["Browser"]
        for fault in faults:
            if os.getloadavg()[0] > 60:
                raise RuntimeError("STOPPED: load1 exceeds 60")
            name = fault["id"]
            control = run_test(source, fault["test"], out / (name + "-control"), endpoint)
            if control["exit"] or control["skipped"] or not control["tests"]:
                record["faults"].append({"id": name, "test": fault["test"], "control": control, "caught": False})
                raise RuntimeError("Passing control required: " + name)
            try:
                for relative, before, after in fault["edits"]:
                    path = source / relative
                    text = path.read_text()
                    if text.count(before) != 1:
                        raise RuntimeError(f"Fault anchor must occur once: {name}: {relative}: {text.count(before)}")
                    path.write_text(text.replace(before, after, 1))
                result = run_test(source, fault["test"], out / (name + "-fault"), endpoint)
                caught = (
                    result["exit"] == 1
                    and not result["errors"]
                    and not result["skipped"]
                    and result["assertion_failure"]
                )
                record["faults"].append(
                    {"id": name, "test": fault["test"], "control": control, "fault": result, "caught": caught}
                )
                print(json.dumps({"fault": name, "caught": caught}), flush=True)
            finally:
                for relative, data in originals.items():
                    (source / relative).write_bytes(data)
            if not caught:
                raise RuntimeError("Fault was missed or failed outside an assertion: " + name)
        final_control = run_test(source, "tests/test_ux_wires.py", out / "restored-wires-control", endpoint)
        record["restored_control"] = final_control
        if final_control["exit"] or final_control["skipped"] or not final_control["tests"]:
            raise RuntimeError("Restored wire controls must all pass")
        record["gate"] = "PASS"
        return 0
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ET.ParseError) as error:
        record.update(gate="FAIL", error=str(error))
        return 1
    finally:
        for relative, data in originals.items():
            (source / relative).write_bytes(data)
        record["sources_restored"] = all(digest(source / relative) == value for relative, value in hashes.items())
        if chrome is not None and chrome.poll() is None:
            chrome.terminate()
            try:
                chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome.kill()
                chrome.wait(timeout=5)
        record["chrome_closed"] = chrome is None or chrome.poll() is not None
        record["caught"] = sum(fault["caught"] for fault in record["faults"])
        record["total"] = len(faults)
        (out / "RESULT.json").write_text(json.dumps(record, indent=2))
        print(
            json.dumps(
                {
                    key: record.get(key)
                    for key in ("gate", "caught", "total", "sources_restored", "chrome_closed", "error")
                }
            )
        )


if __name__ == "__main__":
    raise SystemExit(main())
