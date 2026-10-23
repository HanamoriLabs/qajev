"""Local-fixture faults for complete check diagnostics; CPU-only Linux, no models."""

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

UNIT = 'tests/test_check_evidence.py::'
LIVE = 'tests/test_check_evidence_live.py::'
V = 'qajev/verdict.py'
D = 'qajev/diagnostics.py'
G = 'qajev/js/guard.js'
S = 'qajev/session.py'
H = 'qajev/report_html.py'
M = 'qajev/report.py'
F = 'qajev/report_files.py'

FAULTS = [
    ('D01-string-detail', V, 'return False, public or "returned an empty string", public',
     'return False, public[:190] or "returned an empty string", public', UNIT+'test_failed_js_keeps_the_complete_reason_and_value'),
    ('D02-error-detail', V, 'clean_data(f"error: {r[\'error\']}")',
     'clean_data(f"error: {r[\'error\']}")[:190]', UNIT+'test_failed_js_keeps_the_complete_reason_and_value'),
    ('D03-object-value', V, 'bound_text(json.dumps(clean_data(value)))', 'bound_text(json.dumps(clean_data(value)))[:200]',
     UNIT+'test_unexpected_object_keeps_its_full_value_without_becoming_truthy'),
    ('D04-shown-value', S, 'shown: String(v)', 'shown: String(v).slice(0, 200)',
     LIVE+'test_browser_keeps_the_complete_shown_value_of_non_data_results'),
    ('D05-truthy-ok', V, 'type(value.get("ok")) is bool', 'bool(value.get("ok"))',
     UNIT+'test_malformed_structured_js_results_still_fail'),
    ('D06-says-type', V, 'isinstance(value.get("says"), str)', 'True',
     UNIT+'test_malformed_structured_js_results_still_fail'),
    ('D07-evidence-shape', V, 'isinstance(value.get("evidence"), dict)', 'True',
     UNIT+'test_malformed_structured_js_results_still_fail'),
    ('D08-measured-verdict', V, 'return value["ok"], None if value["ok"] else bound_text(public["says"]), bound_text(json.dumps(public))',
     'return True, None, bound_text(json.dumps(public))', UNIT+'test_explicit_js_result_keeps_measured_evidence_and_the_authors_plan_words'),
    ('D09-evidence-wire', V, 'evidence=clean_evidence(measured["evidence"]), evidence_type="js"',
     'evidence={}, evidence_type="js"', UNIT+'test_explicit_js_result_keeps_measured_evidence_and_the_authors_plan_words'),
    ('D10-evidence-marker', V, 'evidence_type="js"', 'evidence_type="image"',
     UNIT+'test_explicit_js_result_keeps_measured_evidence_and_the_authors_plan_words'),
    ('D11-measured-value', V, 'bound_text(json.dumps(public))', 'bound_text(json.dumps(public))[:200]',
     UNIT+'test_explicit_js_result_keeps_measured_evidence_and_the_authors_plan_words'),
    ('D12-secret-cleaning', D, 'return redact(value)', 'return value',
     UNIT+'test_structured_diagnostics_clean_secrets_and_paths_without_losing_numbers'),
    ('D13-path-cleaning', D, 'json.loads(sanitize_upload(encoded))', 'json.loads(encoded)',
     UNIT+'test_structured_diagnostics_clean_secrets_and_paths_without_losing_numbers'),
    ('D14-artifact-boundary', F, 'if evidence and check.get("evidence_type") != "js":', 'if evidence:',
     UNIT+'test_json_and_html_keep_full_failed_values_and_escape_measurements'),
    ('D15-html-artifact-boundary', H, 'if check.get("evidence_type") != "js":', 'if True:',
     UNIT+'test_json_and_html_keep_full_failed_values_and_escape_measurements'),
    ('D16-markdown-artifact-boundary', M, 'if check.get("evidence_type") != "js":', 'if True:',
     UNIT+'test_json_and_html_keep_full_failed_values_and_escape_measurements'),
    ('D17-html-value-escape', H, '{_e(text)}</pre></details>', '{text}</pre></details>',
     UNIT+'test_json_and_html_keep_full_failed_values_and_escape_measurements'),
    ('D18-html-evidence-escape', H, '_e(json.dumps(evidence, ensure_ascii=False, sort_keys=True))',
     'json.dumps(evidence, ensure_ascii=False, sort_keys=True)',
     UNIT+'test_json_and_html_keep_full_failed_values_and_escape_measurements'),
    ('D19-error-envelope', V, ' and "error" not in raw', '',
     UNIT+'test_error_envelope_wins_over_an_apparent_structured_success'),
    ('D20-full-error-finding', V, 'clean_data(str(err.get("detail", "")))',
     'clean_data(str(err.get("detail", "")))[:190]', UNIT+'test_console_error_findings_keep_the_complete_diagnostic'),
    ('D21-full-browser-error', G, "{ kind: 'console', detail: text }", "{ kind: 'console', detail: text.slice(0, 500) }",
     LIVE+'test_browser_keeps_long_check_values_and_errors'),
    ('D22-key-cleaning', D, 'name = redact(key)', 'name = key',
     UNIT+'test_diagnostic_cleaning_preserves_json_numbers_and_cleans_nested_keys'),
    ('D23-nested-list-cleaning', D, 'return [_redacted(item) for item in value]', 'return value',
     UNIT+'test_diagnostic_cleaning_preserves_json_numbers_and_cleans_nested_keys'),
    ('D24-json-types', D, 'return redact(value)', 'return redact(str(value))',
     UNIT+'test_diagnostic_cleaning_preserves_json_numbers_and_cleans_nested_keys'),
    ('D25-key-collision', D, 'if name in out:', 'if False:',
     UNIT+'test_diagnostic_cleaning_refuses_colliding_redacted_keys'),
    ('C01-warn-capture', G, "for (const level of ['log', 'info', 'warn'])", 'for (const level of [])',
     LIVE+'test_measured_js_executes_once_and_keeps_object_console_numbers'),
    ('C02-error-capture', G, "if (!framed) keepPageConsole('error', args);", '',
     LIVE+'test_console_buffer_counts_omissions_and_keeps_text_until_privacy_cleaning'),
    ('C03-object-numbers', G, "typeof a === 'string' ? a : JSON.stringify(a) ?? String(a)", 'String(a)',
     LIVE+'test_measured_js_executes_once_and_keeps_object_console_numbers'),
    ('C04-premature-text-cut', G, 'text: consoleText(args)', 'text: consoleText(args).slice(0, 500)',
     LIVE+'test_console_buffer_counts_omissions_and_keeps_text_until_privacy_cleaning'),
    ('C05-source-record-cap', G, 'state.pageConsole.length < 50', 'state.pageConsole.length < 500',
     LIVE+'test_console_buffer_counts_omissions_and_keeps_text_until_privacy_cleaning'),
    ('C06-source-omissions', G, 'else pageConsoleOmitted++;', 'else {}',
     LIVE+'test_console_buffer_counts_omissions_and_keeps_text_until_privacy_cleaning'),
    ('C07-drain-once', G, 'page_console: state.pageConsole.splice(0)', 'page_console: state.pageConsole.slice(0)',
     LIVE+'test_console_buffer_counts_omissions_and_keeps_text_until_privacy_cleaning'),
    ('C08-after-check-drain', S, 'if (probe && q.drainPageConsole)', 'if (false)',
     LIVE+'test_measured_js_executes_once_and_keeps_object_console_numbers'),
    ('C09-navigation-carry', G, 'dialogs: state.dialogs.splice(0), ...state.drainPageConsole()',
     'dialogs: state.dialogs.splice(0)',
     LIVE+'test_console_survives_navigation_inside_a_scenario_and_does_not_leak_to_the_next'),
    ('C10-scenario-isolation', S, 'if leaving_last_test and key in ("page_console", "page_console_omitted"):',
     'if False:', LIVE+'test_console_survives_navigation_inside_a_scenario_and_does_not_leak_to_the_next'),
    ('C11-carried-lines-reset', S, '        self.carried.pop("page_console", None)\n', '',
     UNIT+'test_a_new_scenario_clears_carried_console_but_preserves_blocked_writes'),
    ('C12-carried-omissions-reset', S, '        self.carried.pop("page_console_omitted", None)\n', '',
     UNIT+'test_a_new_scenario_clears_carried_console_but_preserves_blocked_writes'),
    ('C13-aggregate-cap', D, 'if len(kept) >= CONSOLE_LINES:', 'if False:',
     UNIT+'test_console_limit_applies_across_multiple_reads'),
    ('C14-aggregate-omissions', D, 'result.get("page_console_omitted", 0) + count',
     'result.get("page_console_omitted", 0)', UNIT+'test_console_limit_applies_across_multiple_reads'),
    ('C15-clean-before-cut', D, 'sanitize_text(redact(item["text"]))',
     'sanitize_text(redact(item["text"][:CONSOLE_CHARACTERS]))',
     UNIT+'test_scenario_keeps_bounded_console_after_cleaning_complete_text'),
    ('C16-console-path-cleaning', D, 'sanitize_text(redact(item["text"]))', 'redact(item["text"])',
     UNIT+'test_scenario_keeps_bounded_console_after_cleaning_complete_text'),
    ('C17-console-html-escape', H, '_e("\\n".join(lines))', '"\\n".join(lines)',
     UNIT+'test_console_is_visible_and_escaped_in_failed_and_harness_reports'),
    ('C18-warn-forwarded', G, 'return real.apply(this, args);\n      };\n    }\n  }\n  try {',
     'return undefined;\n      };\n    }\n  }\n  try {',
     LIVE+'test_guard_console_wrapper_preserves_original_calls_and_handles_circular_arguments'),
    ('C19-error-forwarded', G, 'return consoleError.apply(this, args);', 'return undefined;',
     LIVE+'test_guard_console_wrapper_preserves_original_calls_and_handles_circular_arguments'),
    ('C20-runner-wire', 'qajev/runner.py', '        keep_console(result, probe)\n', '',
     UNIT+'test_scenario_keeps_bounded_console_after_cleaning_complete_text'),
    ('C21-html-omitted-count', H, "{r['page_console_omitted']} console lines omitted", '0 console lines omitted',
     UNIT+'test_console_is_visible_and_escaped_in_failed_and_harness_reports'),
    ('C22-md-omitted-count', M, "{r['page_console_omitted']} console lines omitted", '0 console lines omitted',
     UNIT+'test_console_is_visible_and_escaped_in_failed_and_harness_reports'),
    ('C23-reset-omissions', G, '    pageConsoleOmitted = 0;\n', '',
     LIVE+'test_console_buffer_counts_omissions_and_keeps_text_until_privacy_cleaning'),
    ('C24-character-boundary', D, 'if len(text) > CONSOLE_CHARACTERS else', 'if len(text) >= CONSOLE_CHARACTERS else',
     UNIT+'test_console_character_limit_marks_only_lines_that_were_shortened'),
    ('C25-frame-isolation', G, "if (!framed) keepPageConsole('error', args);", "keepPageConsole('error', args);",
     LIVE+'test_frame_console_error_stays_out_of_the_top_page_console'),
    ('B01-item-cap', D, 'if len(encoded) <= DIAGNOSTIC_BYTES:', 'if True:',
     UNIT+'test_js_reason_and_value_have_a_visible_per_item_boundary'),
    ('B02-item-exact-boundary', D, 'if len(encoded) <= DIAGNOSTIC_BYTES:', 'if len(encoded) < DIAGNOSTIC_BYTES:',
     UNIT+'test_js_reason_and_value_have_a_visible_per_item_boundary'),
    ('B03-visible-marker', D, 'return kept + f"\\n[truncated {len(text) - len(kept)} characters]"', 'return kept',
     UNIT+'test_js_reason_and_value_have_a_visible_per_item_boundary'),
    ('B04-cut-count', D, '{len(text) - len(kept)} characters', '{len(text) - len(kept) + 1} characters',
     UNIT+'test_js_reason_and_value_have_a_visible_per_item_boundary'),
    ('B05-utf8-byte-bound', D, 'encoded[:DIAGNOSTIC_BYTES].decode("utf-8", errors="ignore")', 'text[:DIAGNOSTIC_BYTES]',
     UNIT+'test_item_bound_counts_utf8_bytes_without_splitting_a_character'),
    ('B06-clean-before-item-cut', V, 'public = bound_text(clean_data(value))', 'public = clean_data(bound_text(value))',
     UNIT+'test_item_bound_cleans_the_whole_secret_and_path_before_cutting'),
    ('B07-page-error-wire', V, 'bound_text(clean_data(str(err.get("detail", ""))))', 'clean_data(str(err.get("detail", "")))',
     UNIT+'test_page_error_findings_bound_each_oversized_item'),
    ('B08-error-wire', V, 'bound_text(clean_data(f"error: {r[\'error\']}"))', 'clean_data(f"error: {r[\'error\']}")',
     UNIT+'test_all_js_diagnostic_wires_bound_oversized_text[error]'),
    ('B09-shown-wire', V, 'bound_text(clean_data(str(r.get("shown") or r["type"])))', 'clean_data(str(r.get("shown") or r["type"]))',
     UNIT+'test_all_js_diagnostic_wires_bound_oversized_text[shown]'),
    ('B10-measured-reason-wire', V, 'else bound_text(public["says"])', 'else public["says"]',
     UNIT+'test_all_js_diagnostic_wires_bound_oversized_text[measured]'),
    ('B11-measured-value-wire', V, 'bound_text(json.dumps(public))', 'json.dumps(public)',
     UNIT+'test_all_js_diagnostic_wires_bound_oversized_text[measured]'),
    ('B12-object-value-wire', V, 'bound_text(json.dumps(clean_data(value)))', 'json.dumps(clean_data(value))',
     UNIT+'test_all_js_diagnostic_wires_bound_oversized_text[object]'),
    ('B13-evidence-cap', D, 'if len(shown.encode("utf-8")) <= DIAGNOSTIC_BYTES:', 'if True:',
     UNIT+'test_structured_evidence_has_its_own_item_boundary'),
    ('B14-evidence-exact-boundary', D, 'if len(shown.encode("utf-8")) <= DIAGNOSTIC_BYTES:',
     'if len(shown.encode("utf-8")) < DIAGNOSTIC_BYTES:', UNIT+'test_structured_evidence_has_its_own_item_boundary'),
    ('B15-evidence-marker', D, 'f"[truncated {len(shown)} characters]"', '"truncated"',
     UNIT+'test_structured_evidence_has_its_own_item_boundary'),
    ('B16-character-count', D, '{len(text) - len(kept)} characters', '{len(encoded) - len(kept.encode("utf-8"))} characters',
     UNIT+'test_item_bound_counts_utf8_bytes_without_splitting_a_character'),
    ('B17-evidence-wire', V, 'evidence=clean_evidence(measured["evidence"])', 'evidence=clean_data(measured["evidence"])',
     UNIT+'test_structured_evidence_has_its_own_item_boundary'),
    ('L01-log-capture', G, "for (const level of ['log', 'info', 'warn'])", "for (const level of ['info', 'warn'])",
     'tests/test_page_console.py::test_page_log_and_info_reach_the_report_without_changing_the_verdict[log]'),
    ('L02-info-capture', G, "for (const level of ['log', 'info', 'warn'])", "for (const level of ['log', 'warn'])",
     'tests/test_page_console.py::test_page_log_and_info_reach_the_report_without_changing_the_verdict[info]'),
    ('L03-log-filter', D, '{"log", "info", "warn", "error"}', '{"info", "warn", "error"}',
     'tests/test_page_console.py::test_runner_keeps_page_log_and_info_measurements[log]'),
    ('L04-info-filter', D, '{"log", "info", "warn", "error"}', '{"log", "warn", "error"}',
     'tests/test_page_console.py::test_runner_keeps_page_log_and_info_measurements[info]'),
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--only')
    args = parser.parse_args()
    if sys.platform != 'linux':
        raise SystemExit('REFUSED: Linux fixture job only')
    os.setpriority(os.PRIO_PROCESS, 0, 19)
    root, out = args.source.resolve(), args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    stage = out / 'source'
    shutil.copytree(root, stage, ignore=shutil.ignore_patterns('.git', '.venv', '.rt', '__pycache__',
                                                              '.pytest_cache', '.ruff_cache', 'node_modules'))
    spec = importlib.util.spec_from_file_location('ux_fault_helpers', root / 'tools/check-ux-faults.py')
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    chosen = set(args.only.split(',')) if args.only else None
    faults = [row for row in FAULTS if chosen is None or row[0] in chosen]
    if chosen and chosen != {row[0] for row in faults}:
        raise SystemExit('Unknown fault ID')
    originals = {path: (stage / path).read_bytes() for _, path, *_ in faults}
    records = []
    try:
        for name, path, before, after, test in faults:
            if os.getloadavg()[0] > 60:
                raise RuntimeError('STOPPED: box load1 > 60')
            control = helper.run_test(stage, test, out / (name + '-control'), args.endpoint)
            assert control['exit'] == 0 and control['tests'] and not control['skipped'], (
                name, 'control failed', str(out / (name + '-control.log')), control)
            text = originals[path].decode()
            assert text.count(before) == 1, (name, 'mutation anchor is not unique', text.count(before))
            try:
                (stage / path).write_text(text.replace(before, after, 1))
                failed = helper.run_test(stage, test, out / (name + '-fault'), args.endpoint)
                assert failed['exit'] == 1 and not failed['errors'] and not failed['skipped'] and failed['assertion_failure'], (
                    name, 'fault missed or not an assertion failure', str(out / (name + '-fault.log')), failed)
                records.append({'fault': name, 'test': test, 'control': control, 'fault_result': failed, 'caught': True})
                print(json.dumps({'fault': name, 'caught': True}), flush=True)
            finally:
                (stage / path).write_bytes(originals[path])
    finally:
        for path, content in originals.items():
            (stage / path).write_bytes(content)
        receipt = {'caught': len(records), 'total': len(faults), 'matrix': records,
                   'gate': 'PASS' if len(records) == len(faults) else 'FAIL',
                   'sources_restored': all((stage / p).read_bytes() == b for p, b in originals.items()),
                   'source_hashes': {path: digest(stage / path) for path in originals}}
        (out / 'RESULT.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
