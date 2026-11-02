"""A react policy slower than the CDP timeout, in a real Chrome through the real CLI: QAJev waits on the same policy
call, never calls the policy twice at once, and never calls the browser stalled (Twilight, 2 Nov)."""

import json
import os
import subprocess
import sys

import pytest
from test_live import browser, site  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(os.environ.get("QAJEV_LIVE") != "1", reason="set QAJEV_LIVE=1 (starts a local Chrome)")

# Each call takes 7 s: past two 3 s CDP timeouts in a row, which read as a stalled browser before the fix.
POLICY = ("(async () => { window.__calls++; window.__running++;"
          " window.__most = Math.max(window.__most, window.__running);"
          " await new Promise((r) => setTimeout(r, 7000)); window.__running--; return null; })()")


def test_a_policy_slower_than_the_cdp_timeout_runs_one_call_at_a_time(browser, site, tmp_path):  # noqa: F811
    spec = tmp_path / "suite.json"
    spec.write_text(json.dumps({"name": "Slow react policy", "base_url": site, "scenarios": [{
        "name": "slow policy", "url": "/", "settle": 0,
        "about": "A policy that takes 7 s a call is waited on, one call at a time, with a 3 s CDP timeout.",
        "before": [{"js": "window.__calls = 0; window.__running = 0; window.__most = 0"},
                   {"react": {"js": POLICY, "until": "window.__calls >= 2", "every_ms": 50, "for_s": 40}}],
        "expect": {"js": "window.__calls === 2 && window.__most === 1",
                   "says": "the slow policy ran twice, never two calls at once"}}]}))
    empty = tmp_path / "empty.env"
    empty.touch(mode=0o600)
    env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "PYTHONPATH") if key in os.environ}
    env.update(QAJEV_HOME=str(tmp_path / "home"), QAJEV_OUTLIVE_PARENT="1", PYTHONDONTWRITEBYTECODE="1",
               QAJEV_CDP_TIMEOUT="3")
    argv = [sys.executable, "-P", "-m", "qajev", "run", str(spec), "--runner", "local", "--headless",
            "--cdp-url", browser["cdp_url"], "--devices", "desktop", "--cost-cap", "0", "--ux", "off", "--no-shots",
            "--env-file", str(empty), "--out", str(tmp_path / "reports"), "--json", "--quiet"]
    with (tmp_path / "cli.stdout").open("w") as stdout, (tmp_path / "cli.stderr").open("w") as stderr:
        result = subprocess.run(argv, env=env, stdout=stdout, stderr=stderr, timeout=120)
    report = json.loads((tmp_path / "cli.stdout").read_text())
    row = report["scenarios"][0]
    assert "stopped answering" not in json.dumps(report), row
    assert row["outcome"] == "pass" and all(check["ok"] for check in row["checks"]), row
    assert result.returncode == 0 and report["gate"] == "PASS"
    assert report["cost"]["calls"] == {"typesafe": 0, "text": 0}
