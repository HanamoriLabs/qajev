"""Browser source-budget faults plus the complete inherited diagnostics fault set."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('diagnostic_faults', Path(__file__).with_name('check-evidence-faults.py'))
assert spec is not None and spec.loader is not None
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

LIVE = 'tests/test_browser_diagnostic_cap.py::'
LIMIT = 'qajev/js/diagnostic_limit.js'
G = 'qajev/js/guard.js'
S = 'qajev/session.py'

NEW_FAULTS = [
    ('T01-source-limit', LIMIT, 'const MAX_BYTES = 1024 * 1024;', 'const MAX_BYTES = 8 * 1024 * 1024;',
     LIVE+'test_page_diagnostic_source_boundary'),
    ('T02-source-boundary', LIMIT, 'text.length > MAX_BYTES', 'text.length >= MAX_BYTES',
     LIVE+'test_page_diagnostic_source_boundary'),
    ('T03-source-byte-count', LIMIT, ' || new TextEncoder().encode(text).length > MAX_BYTES', '',
     LIVE+'test_source_limit_uses_utf8_bytes_and_counts_whole_characters'),
    ('T04-source-text', LIMIT, 'return tooLarge(value) ? marker(value) : value;', 'return value;',
     LIVE+'test_page_diagnostic_source_boundary'),
    ('T05-source-marker', LIMIT, 'return `[truncated ${characters} characters before transfer; exceeds 1 MiB]`;', "return '';",
     LIVE+'test_page_diagnostic_source_boundary'),
    ('T06-source-codepoints', LIMIT, 'for (const character of text) characters++;', 'characters = text.length;',
     LIVE+'test_source_limit_uses_utf8_bytes_and_counts_whole_characters'),
    ('T07-error-class', LIMIT, "return kept !== original && kind ? kind[1] + ': ' + kept : kept;", 'return kept;',
     LIVE+'test_source_omission_preserves_check_error_classification'),
    ('T08-object-budget', LIMIT, 'if (!tooLarge(shown)) return input;', 'return input;',
     LIVE+'test_js_replies_are_bounded_before_transfer_without_changing_failure_kind[object]'),
    ('T09-boolean-shape', LIMIT, "typeof input.ok === 'boolean'", 'true',
     LIVE+'test_source_limit_does_not_weaken_boolean_or_shape_rules'),
    ('T10-says-shape', LIMIT, "typeof input.says === 'string'", 'true',
     LIVE+'test_source_limit_does_not_weaken_boolean_or_shape_rules'),
    ('T11-evidence-array', LIMIT, '!Array.isArray(input.evidence)', 'true',
     LIVE+'test_source_limit_does_not_weaken_boolean_or_shape_rules'),
    ('T12-evidence-type', LIMIT, "typeof input.evidence === 'object'", 'true',
     LIVE+'test_source_limit_does_not_weaken_boolean_or_shape_rules'),
    ('T13-evidence-null', LIMIT, 'input.evidence !== null && ', '',
     LIVE+'test_source_limit_does_not_weaken_boolean_or_shape_rules'),
    ('T14-measured-reason', LIMIT, 'says: text(input.says)', 'says: input.says',
     LIVE+'test_large_measured_results_keep_the_original_boolean_verdict'),
    ('T15-measured-evidence', LIMIT, 'evidence: object(input.evidence)', 'evidence: input.evidence',
     LIVE+'test_large_measured_results_keep_the_original_boolean_verdict'),
    ('T16-aggregate-budget', LIMIT, 'if (tooLarge(JSON.stringify(kept)))', 'if (false)',
     LIVE+'test_measured_result_is_bounded_when_multiple_fields_together_exceed_the_cap'),
    ('T17-aggregate-reason', LIMIT, '        kept.says = marker(input.says);\n', '',
     LIVE+'test_measured_result_is_bounded_when_multiple_fields_together_exceed_the_cap'),
    ('T18-aggregate-evidence', LIMIT, '        kept.evidence = {_qajev_source_truncated: marker(JSON.stringify(input.evidence))};\n', '',
     LIVE+'test_measured_result_is_bounded_when_multiple_fields_together_exceed_the_cap'),
    ('T19-array-kind', LIMIT, 'return Array.isArray(input) ? [note] : {_qajev_source_truncated: note};',
     'return {_qajev_source_truncated: note};', LIVE+'test_js_replies_are_bounded_before_transfer_without_changing_failure_kind[array]'),
    ('T20-primitive-kind', LIMIT, "if (input === null || typeof input !== 'object') return input;",
     "if (input === null || typeof input !== 'object') return null;",
     'tests/test_page_console.py::test_page_log_and_info_reach_the_report_without_changing_the_verdict[log]'),
    ('T21-object-kind', LIMIT, 'return Array.isArray(input) ? [note] : {_qajev_source_truncated: note};',
     'return [note];', LIVE+'test_js_replies_are_bounded_before_transfer_without_changing_failure_kind[object]'),
    ('W01-page-error-budget', G, 'detail: diagnostic.text(item.detail)', 'detail: item.detail',
     LIVE+'test_page_diagnostic_source_boundary'),
    ('W02-console-budget', G, 'diagnostic.text(args.map', '((value) => value)(args.map',
     LIVE+'test_page_diagnostic_source_boundary'),
    ('W03-js-error-budget', S, 'js.error = diagnostic.error(js.error)', 'js.error = js.error',
     LIVE+'test_js_replies_are_bounded_before_transfer_without_changing_failure_kind[error]'),
    ('W04-js-value-budget', S, 'js.value = diagnostic.value(js.value)', 'js.value = js.value',
     LIVE+'test_js_replies_are_bounded_before_transfer_without_changing_failure_kind'),
    ('W05-js-shown-budget', S, 'js.shown = diagnostic.text(js.shown)', 'js.shown = js.shown',
     LIVE+'test_js_replies_are_bounded_before_transfer_without_changing_failure_kind[shown]'),
    ('W06-guard-source', G, 'const diagnostic = __QAJEV_DIAGNOSTIC_LIMIT__;',
     'const diagnostic = {text: value => String(value)};', LIVE+'test_page_diagnostic_source_boundary'),
    ('W07-probe-source', S, 'diagnostic_limit=guard_mod.DIAGNOSTIC_LIMIT',
     'diagnostic_limit="({text:v=>String(v), value:v=>v, error:v=>String(v)})"',
     LIVE+'test_js_replies_are_bounded_before_transfer_without_changing_failure_kind'),
]

base.__dict__["FAULTS"] += NEW_FAULTS

if __name__ == '__main__':
    raise SystemExit(base.main())
