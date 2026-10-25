"""Restored, assertion-based faults for lost browser/daemon transport handling."""

import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "transport_fault_helpers", Path(__file__).with_name("check-evidence-faults.py"))
assert spec is not None and spec.loader is not None
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

S = "qajev/session.py"
R = "qajev/runner.py"
T = "tests/test_transport_loss.py::"
base.FAULTS = [
    ("T01-resend-after-loss", S,
     "def call(method, session_id=None, **params):\n        if transport.error is not None:",
     "def call(method, session_id=None, **params):\n        if False:",
     T + "test_poisoned_transport_never_sends_a_second_raw_command"),
    ("T02-forget-lost-transport", S, "if lost:", "if False:",
     T + "test_each_transport_failure_opens_the_fuse_before_a_second_command"),
    ("T03-reuse-lost-session", R, "if lost_after is not None:", "if False:",
     T + "test_mid_suite_transport_loss_keeps_one_harness_and_never_reuses_or_replays"),
    ("T04-swallowed-error-pass", R, 'if (error := session_mod.transport_error()) is not None:', "if False:",
     T + "test_swallowed_optional_probe_error_cannot_turn_transport_loss_into_pass"),
    ("T05-timeout-is-transport-loss", S, "(ConnectionError, EOFError, FileNotFoundError)",
     "(ConnectionError, EOFError, FileNotFoundError, TimeoutError)",
     T + "test_ordinary_command_failure_or_timeout_does_not_poison_later_scenarios"),
    ("T06-no-session-cleanup", R, "            session.close()\n        session_mod.stop_daemon()",
     "            pass\n        session_mod.stop_daemon()",
     T + "test_mid_suite_transport_loss_keeps_one_harness_and_never_reuses_or_replays"),
    ("T07-retry-after-not-run", R, '                _emit(opts, "scenario", result=result)\n                continue\n',
     '                _emit(opts, "scenario", result=result)\n',
     T + "test_mid_suite_transport_loss_keeps_one_harness_and_never_reuses_or_replays"),
    ("T08-discard-earlier-results", R,
     "result = _transport_result(scenario, error, started, result)\n                    lost_after = scenario.name\n",
     "result = _transport_result(scenario, error, started, result)\n                    lost_after = scenario.name\n"
     "                    results.clear()\n",
     T + "test_mid_suite_transport_loss_keeps_one_harness_and_never_reuses_or_replays"),
    ("T09-line-limit-is-browser-loss", S, "if said:", "if False:",
     T + "test_daemon_line_limit_diagnostic_does_not_poison_a_healthy_transport"),
    ("T10-large-request-hides-websocket-loss", S, "if not lost or isinstance(error, BrokenPipeError) else None", "",
     T + "test_an_explicit_websocket_loss_with_a_large_request_still_opens_the_fuse"),
    ("T11-load-has-a-different-fuse", S, "providers=resolved, transport=transport)",
     "providers=resolved, transport=SimpleNamespace(error=None))",
     T + "test_load_wires_the_same_fuse_into_jev_and_the_runner"),
    ("T12-next-chain-reuses-transport", R,
     'lost_after = "an earlier scenario" if session_mod.transport_error() is not None else None',
     'lost_after = None', T + "test_a_second_group_on_the_same_lost_transport_never_constructs_a_session"),
    ("T13-empty-daemon-reply-is-success", S,
     'if request.get("method") and (not isinstance(response, dict) or "result" not in response):', "if False:",
     T + "test_empty_cut_or_invalid_daemon_reply_never_acknowledges_an_action_or_reconnects"),
    ("T14-load-misses-reply-validation", S,
     "helpers._send = _checked_send(helpers._send, transport)", "pass",
     T + "test_load_wires_the_same_fuse_into_jev_and_the_runner"),
    ("T15-non-cdp-hook-after-loss", S,
     "def run_hook(self, hook, current_url):\n        require_transport()",
     "def run_hook(self, hook, current_url):\n        pass",
     T + "test_a_lost_transport_prevents_even_non_cdp_hooks_from_starting"),
    ("T16-shell-after-loss", S, "def command(self, run, current_url):\n        require_transport()",
     "def command(self, run, current_url):\n        pass",
     T + "test_a_lost_transport_prevents_shell_commands_from_starting"),
    ("T17-swallowed-probe-starts-next-hook", R, "        require_transport()\n", "",
     T + "test_swallowed_optional_probe_error_cannot_turn_transport_loss_into_pass"),
]

if __name__ == "__main__":
    sys.argv += ["--endpoint", "http://127.0.0.1:9"]
    raise SystemExit(base.main())
