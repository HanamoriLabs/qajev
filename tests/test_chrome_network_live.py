"""Opt-in local CLI fixture: product flags must stop nonlocal Chrome request starts."""

import http.server
import json
import os
import runpy
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("QAJEV_CHROME_NETWORK_LIVE") != "1" or sys.platform != "linux",
    reason="explicit network-isolated Linux Chrome fixture required",
)


@pytest.mark.parametrize("custom_feature", [False, True])
def test_product_launcher_stops_background_requests_with_plain_and_custom_features(tmp_path, custom_feature):
    assert os.getpriority(os.PRIO_PROCESS, 0) == 19
    assert os.environ.get("QAJEV_CHROME_FLAGS") is None, "proof must not supply service-suppression flags"
    body = b'''<!doctype html><title>Local launcher fixture</title><h1>Installed QAJev is ready</h1>
    <form id="login"><label>Fixture password<input id="password" name="password" type="password"
    autocomplete="current-password"></label><button id="submit" type="submit">Sign in</button></form>
    <script>
    window.passwordFixture = {inputEvents:0, characters:0, submitted:false};
    document.querySelector('#password').addEventListener('input', event => {
      passwordFixture.inputEvents++; passwordFixture.characters = event.target.value.length;
    });
    document.querySelector('#login').addEventListener('submit', event => {
      event.preventDefault(); passwordFixture.submitted = true; event.target.remove();
    });
    </script>'''

    class Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    suite = tmp_path / "fixture.json"
    suite.write_text(json.dumps({
        "name": "Managed Chrome background networking", "base_url": f"http://127.0.0.1:{server.server_port}",
        "devices": ["desktop"], "mode": "mutate", "guard": {"allow_secret_fields": True},
        "scenarios": [{"name": "local page", "url": "/fixture",
            "about": "The product launcher renders a local page without starting nonlocal requests.",
            "before": [{"fill": {"#password": "NOT-A-REAL-PASSWORD-Fixture123!"}}, {"click": "#submit"},
                       {"sleep": 30}, {"sleep": 30}],
            "expect": {"text": "Installed QAJev is ready", "js": "passwordFixture.inputEvents > 0 && "
                       "passwordFixture.characters === 31 && passwordFixture.submitted",
                       "says": {"js": "Real password input and submit reached the synthetic page."}}}],
    }))
    empty = tmp_path / "empty.env"
    empty.touch(mode=0o600)
    netlog = tmp_path / "chrome-netlog.json"
    flags = ["--disable-gpu", "--proxy-server=http://127.0.0.1:9",
             "--proxy-bypass-list=127.0.0.1;localhost;*.localhost",
             "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
             f"--log-net-log={netlog}"]
    if custom_feature:
        flags.append("--disable-features=FixtureUiFeature")
    env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "PYTHONPATH") if key in os.environ}
    env.update(QAJEV_HOME=str(tmp_path / "home"), QAJEV_CHROME_FLAGS=shlex.join(flags),
               QAJEV_CHROME=os.environ.get("QAJEV_CHROME", "/usr/bin/chromium"), QAJEV_LOCK_WAIT="3600",
               QAJEV_RUNNER="local", QAJEV_OUTLIVE_PARENT="1")
    argv = [sys.executable, "-P", "-m", "qajev", "run", str(suite), "--runner", "local", "--headless",
            "--ephemeral", "--browser-runs", "2", "--env-file", str(empty), "--cost-cap", "0", "--ux", "off",
            "--devices", "desktop", "--out", str(tmp_path / "reports"), "--no-shots", "--json", "--events"]
    child = None
    started = time.monotonic()
    try:
        with (tmp_path / "cli.stdout").open("w") as stdout, (tmp_path / "cli.stderr").open("w") as stderr:
            child = subprocess.Popen(argv, env=env, stdout=stdout, stderr=stderr)
            assert child.wait(timeout=180) == 0, "see cli.stdout and cli.stderr"
        report = json.loads((tmp_path / "cli.stdout").read_text())
        assert report["gate"] == "PASS"
        assert report["cost"]["calls"] == {"typesafe": 0, "text": 0}
        assert time.monotonic() - started >= 60
        audit = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/chrome-netlog-audit.py"))
        data = audit["load"](netlog)
        bad = audit["bad_hosts"](data)
        receipt = {"nonlocal_hosts": bad, "gate": "FAIL" if bad else "PASS",
                   "models": report["cost"]["calls"], "elapsed_seconds": time.monotonic() - started,
                   "custom_feature": custom_feature, "product_flags_only": True,
                   "password_input_and_submit_checked": True}
        (tmp_path / "request-hosts.json").write_text(json.dumps(receipt, indent=2) + "\n")
        assert not bad, receipt
    finally:
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGINT)
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
