#!/usr/bin/env python3
"""Mutate isolated copies; require a named unittest failure, not a crash or timeout."""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FAULTS = [
    (
        "cross-run-delivery",
        "[run, machine, app, ready_nonce]",
        '["ignored-run", "ignored-machine", app, ready_nonce]',
        "test_encrypted_delivery_is_bound_to_run_machine_app_and_ready_nonce",
    ),
    (
        "mint-before-ready",
        "browser.ready()  # incognito target exists before the broker may mint\n        reply = deliver(policy)",
        "reply = deliver(policy)\n        browser.ready()",
        "test_bootstrap_uses_no_daemon_or_recording_and_disposes_context_on_failure",
    ),
    (
        "wrong-test-identity",
        'reply.get("uid") != self.uid',
        "False",
        "test_same_browser_identity_and_deadline_are_required_before_actions",
    ),
    (
        "accept-live-key",
        'startswith("pk_test_")',
        'startswith(("pk_test_", "pk_live_"))',
        "test_mint_reply_rejects_wrong_identity_live_keys_expiry_and_redirect_urls",
    ),
    (
        "ignore-session-deadline",
        "if self.now() >= self.expires_at:",
        "if False:",
        "test_same_browser_identity_and_deadline_are_required_before_actions",
    ),
    (
        "leak-driver-detail",
        'raise SessionError("test_session_bootstrap_rejected")',
        'raise SessionError("CANARY_LEAK")',
        "test_bootstrap_driver_error_never_exposes_the_code_or_secret",
    ),
    (
        "keep-failed-context",
        "        browser.close()\n        # Transport/browser",
        "        pass\n        # Transport/browser",
        "test_bootstrap_uses_no_daemon_or_recording_and_disposes_context_on_failure",
    ),
    (
        "cross-origin-target",
        'if any(urlsplit(u).scheme + "://" + urlsplit(u).netloc != policy.origin for u in urls):',
        "if False:",
        "test_missing_provider_and_wrong_origin_never_start_a_browser",
    ),
]


def run(folder):
    return subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_test_session.py", "-v"],
        cwd=folder,
        capture_output=True,
        text=True,
        timeout=30,
    )


def main():
    if not Path("/srv/boxtest").is_dir():
        raise SystemExit("Run with boxtest --box-only; no Mac fallback.")
    baseline = run(ROOT)
    if baseline.returncode:
        raise SystemExit(baseline.stderr)
    proof = []
    for name, old, new, required in FAULTS:
        with tempfile.TemporaryDirectory(prefix="test-session-fault-") as temp:
            folder = Path(temp)
            shutil.copytree(ROOT / "qajev", folder / "qajev")
            (folder / "tests").mkdir()
            shutil.copy2(ROOT / "tests/test_test_session.py", folder / "tests/test_test_session.py")
            source = folder / "qajev/test_session.py"
            text = source.read_text()
            if text.count(old) != 1:
                raise SystemExit(f"{name}: source must match once")
            source.write_text(text.replace(old, new))
            result = run(folder)
            caught = result.returncode == 1 and (f"FAIL: {required} " in result.stderr)
            proof.append({"fault": name, "caught": caught, "required_failure": required})
            if not caught:
                raise SystemExit(json.dumps(proof, indent=2) + "\n" + result.stderr[-2000:])
    Path("test-session-fault-proof.json").write_text(json.dumps(proof, indent=2) + "\n")
    print(f"PASS: {len(proof)}/{len(FAULTS)} named runner-session faults caught; network disabled.")


if __name__ == "__main__":
    main()
