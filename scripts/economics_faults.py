#!/usr/bin/env python3
"""Plant budget faults in disposable source copies; never call a model/provider.

Run through machine-lane. Exit zero only when every named behavioral check catches
its fault. No source in the checkout is rewritten; test logs stay in the report.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = "tests/test_economics_reservations.py"
FAULTS = [
    ("missing_usage_is_free", "qajev/ledger.py", 'return None, "unknown", tokens, breach\n\n    def _settle',
     'return 0, "reported", tokens, breach\n\n    def _settle',
     "test_missing_text_cost_is_unknown_and_retains_conservative_liability"),
    ("ignore_thread_reservations", "qajev/ledger.py",
     'return sum(self.usd.values()) + sum(a.reserve for a in self.active.values()) + self.guard_hold',
     'return sum(self.usd.values()) + self.guard_hold', "test_atomic_thread_reserve_rejects_second_inflight_attempt"),
    ("ignore_shared_reservations", "qajev/ledger.py",
     'liability = _micros(self.shared.value) if self.shared is not None else self._liability()',
     'liability = 0 if self.shared is not None else self._liability()',
     "test_atomic_shared_budget_counts_another_ledgers_inflight_reservation"),
    ("release_uncertain_attempt", "qajev/ledger.py", 'cost = attempt.reserve if cost is None else cost',
     'cost = 0 if cost is None else cost', "test_possibly_billed_exception_never_releases_the_reservation"),
    ("settle_twice", "qajev/ledger.py",
     'if self.active.get(getattr(attempt, "serial", None)) is not attempt:\n'
     '                return  # idempotent settlement only on the ledger owning this attempt\n'
     '            del self.active[attempt.serial]',
     'self.active.pop(attempt.serial, None)', "test_settlement_is_idempotent_and_does_not_release_other_attempts"),
    ("unmetered_http_retry", "qajev/providers.py", 'attempt = ledger.wrap(once, verified_bounds=verified_bounds)',
     'attempt = once', "test_transport_retries_have_separate_reservations_and_stop_before_unaffordable_retry"),
    ("trust_url_prefix", "qajev/ledger.py", '(verified_bounds or {}).get(url)',
     'next((v for target, v in (verified_bounds or {}).items() if url.startswith(target)), None)',
     "test_cloud_adapter_bounds_apply_only_to_exact_authenticated_final_url"),
    ("lower_documented_bound", "qajev/ledger.py", 'reserve = max(reserve, operator_floor, verified_bound or 0)',
     'reserve = max(operator_floor, verified_bound or 0, 1)',
     "test_legacy_per_call_override_cannot_lower_documented_upper_bound"),
    ("cloud_uncertainty_becomes_actual", "qajev/ledger.py",
     'result, attempt.pricing, attempt.cloud_normalized, attempt.quote)',
     'result, attempt.pricing, False, attempt.quote)', "test_cloud_normalized_cost_basis_preserves_uncertainty"),
    ("lost_worker_liability_disappears", "qajev/runner.py",
     'if unreported > 0 or frozen and not any(led.get("bound_violated") for led in ledgers):',
     'if False:', "test_runner_report_keeps_liability_when_worker_dies_before_summary"),
    ("replay_uncertain_clef_response", "qajev/providers.py",
     'got = post_json(resolved["jev_url"], env[resolved["jev_key_name"]], send)',
     'try:\n                got = post_json(resolved["jev_url"], env[resolved["jev_key_name"]], send)\n'
     '            except RuntimeError:\n'
     '                got = post_json(resolved["jev_url"], env[resolved["jev_key_name"]], send)',
     "test_clef_502_never_replays_an_uncertain_response"),
    ("ignore_malformed_usage", "qajev/ledger.py",
     'if any(not _token(value) for value in supplied_tokens) or any(not _number(value) for value in supplied_money):',
     'if False:',
     "test_budget_integrity.py::test_malformed_supplied_usage_cannot_turn_a_reported_zero_into_free_spend"),
    ("ignore_shared_quote_freeze", "qajev/ledger.py",
     'frozen = isinstance(self.shared, SharedBudget) and self.shared.frozen.value', 'frozen = False',
     "test_budget_integrity.py::test_another_process_cannot_spend_after_a_quote_breach_even_after_an_inflight_refund[paired_flag]"),
    ("drop_bare_value_safety_hold", "qajev/ledger.py",
     'self.shared.value = round(self.shared.value + self.cap / 1e6, 6)',
     'self.shared.value = self.shared.value',
     "test_budget_integrity.py::test_another_process_cannot_spend_after_a_quote_breach_even_after_an_inflight_refund[bare_value]"),
    ("choose_smallest_usd_alias", "qajev/ledger.py", 'return max(valid_money), "reported", tokens, breach',
     'return min(valid_money), "reported", tokens, breach',
     "test_budget_integrity.py::test_clean_zero_remains_zero_and_valid_dollar_aliases_use_conservative_maximum"),
    ("hide_context_breach", "qajev/ledger.py",
     'breach |= any(value > context for value in valid_inputs) or bool(\n'
     '                output_price and any(value > maximum_output for value in valid_outputs))', 'breach |= False',
     "test_budget_integrity.py::test_context_breach_cannot_be_hidden_by_an_invalid_companion_field_or_zero_dollars"),
]


def main():
    results = []
    for name, relative, old, new, check in FAULTS:
        with tempfile.TemporaryDirectory(prefix="qajev-economics-fault-") as folder:
            root = Path(folder)
            shutil.copytree(ROOT / "qajev", root / "qajev", ignore=shutil.ignore_patterns("__pycache__"))
            (root / "tests").mkdir()
            shutil.copyfile(ROOT / TESTS, root / TESTS)
            shutil.copyfile(ROOT / "tests/test_budget_integrity.py", root / "tests/test_budget_integrity.py")
            source = root / relative
            text = source.read_text()
            if text.count(old) != 1:
                raise SystemExit(f"HOLD: {name}: expected exactly one fault anchor")
            source.write_text(text.replace(old, new, 1))
            env = {**os.environ, "PYTHONPATH": str(root)}
            env.pop("QAJEV_MODEL_MAX_USD", None)
            target = f"tests/{check}" if "::" in check else f"{TESTS}::{check}"
            ran = subprocess.run([sys.executable, "-m", "pytest", "-q", "--timeout", "20", target],
                                 cwd=root, env=env, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=40)
            caught = ran.returncode == 1 and f"FAILED {target}" in ran.stdout
            results.append({"fault": name, "check": check, "caught": caught, "exit_code": ran.returncode,
                            "output": ran.stdout})
    print(json.dumps({"caught": sum(r["caught"] for r in results), "total": len(results),
                      "results": results}, indent=2))
    return 0 if all(r["caught"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
