"""Opt-in Linux fixture: QAJev types a synthetic password; no app or real account."""

import http.server
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit

import pytest

from qajev import chrome

pytestmark = pytest.mark.skipif(
    os.environ.get("QAJEV_CHROME_PASSWORD_LIVE") != "1" or sys.platform != "linux",
    reason="explicit Linux-only password-service fixture proof required",
)

# The fixture closes only its owned Chrome through CDP before the normal stop,
# so the netlog footer is flushed. Startup, password policy and input are real.
FLUSHING_CLI = '''
import json, sys, time
from qajev import chrome, cli
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect
normal_stop = chrome.stop
def flushing_stop(profile="default"):
    path = chrome._state_file(profile)
    try:
        if path.exists():
            record = json.loads(path.read_text())
            if chrome.owns(record):
                try:
                    version = chrome.version(record["cdp_url"]) or {}
                    endpoint = version.get("webSocketDebuggerUrl")
                    if endpoint:
                        with connect(endpoint, open_timeout=5) as socket:
                            socket.send(json.dumps({"id": 1, "method": "Browser.close"}))
                            try:
                                socket.recv(timeout=5)
                            except ConnectionClosed:
                                pass
                except (OSError, TimeoutError, ConnectionClosed):
                    pass
                for _ in range(50):
                    if chrome.reaped(record["pid"]):
                        break
                    time.sleep(0.1)
    finally:
        stopped = normal_stop(profile)
    return stopped
chrome.stop = flushing_stop
raise SystemExit(cli.main(sys.argv[1:]))
'''


def test_qajev_password_typing_has_no_nonlocal_request_starts(tmp_path, monkeypatch):
    assert os.readlink("/proc/self/fd/7") == "/tmp/gpu-heavy.lock"
    assert os.getpriority(os.PRIO_PROCESS, 0) == 19
    body = b'''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
    <title>Local synthetic password fixture</title>
    <form id="login" action="/signed-in" method="post">
      <label>Fixture email<input id="email" name="username" type="email"
        autocomplete="username" spellcheck="false"></label>
      <label>Fixture password<input id="password" name="password" type="password"
        autocomplete="current-password"></label>
      <button id="submit" type="submit">Sign in</button>
    </form><p id="status">Synthetic fixture only</p>
    <script>
    window.passwordFixture = {inputEvents:0, characters:0, submitted:false};
    document.querySelector('#password').addEventListener('input', event => {
      passwordFixture.inputEvents++; passwordFixture.characters = event.target.value.length;
    });
    document.querySelector('#login').addEventListener('submit', event => {
      event.preventDefault(); passwordFixture.submitted = true;
      history.pushState({}, '', '/signed-in'); event.target.remove();
      document.querySelector('#status').textContent = 'Fixture signed in';
    });
    </script>'''

    class Fixture(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    # Deliberately public synthetic input, never a credential or seeded app user.
    password = "NOT-A-REAL-PASSWORD-Fixture123!"
    suite = tmp_path / "fixture.json"
    suite.write_text(json.dumps({
        "name": "Synthetic password-service privacy", "base_url": origin, "devices": ["phone"],
        "mode": "mutate", "guard": {"allow_secret_fields": True},
        "scenarios": [{"name": "QAJev types and submits the local password field",
                       "url": "/login", "about": "Synthetic password entry triggers no online password check.",
                       "before": [{"fill": {"#email": "fixture@example.test"}}, {"fill": {"#password": password}},
                                  {"click": "#submit"}, {"sleep": 5}],
                       "expect": {"text": "Fixture signed in", "js": "passwordFixture.inputEvents > 0 && "
                                  f"passwordFixture.characters === {len(password)} && passwordFixture.submitted",
                                  "says": {"js": "Real input events and the synthetic submit reached the page."}}}],
    }))
    empty = tmp_path / "empty.env"
    empty.write_text("")
    empty.chmod(0o600)
    netlog = tmp_path / "chrome-netlog.json"
    flags = shlex.split(os.environ["QAJEV_CHROME_FLAGS"])
    assert "--proxy-server=127.0.0.1:9" in flags
    assert "--proxy-bypass-list=<-loopback>;127.0.0.1;localhost;*.localhost" in flags
    # The harness uses Unix sockets; an artifact path can exceed their length limit.
    runtime = tempfile.TemporaryDirectory(prefix="qajev-pw-")
    env = {**os.environ, "QAJEV_HOME": str(tmp_path / "state"), "BH_RUNTIME_DIR": runtime.name,
           "QAJEV_CHROME_FLAGS": shlex.join([*flags, f"--log-net-log={netlog}", "--net-log-capture-mode=Default"])}
    process = None
    try:
        command = [
            sys.executable, "-P", "-c", FLUSHING_CLI, "run", str(suite), "--runner", "local",
            "--headless", "--ephemeral",
            "--devices", "phone", "--jobs", "1", "--ux", "off", "--cost-cap", "0", "--env-file", str(empty),
            "--out", str(tmp_path / "reports"), "--no-shots", "--json", "--quiet",
        ]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env=env, pass_fds=(7,))
        stdout, stderr = process.communicate(timeout=180)
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        with monkeypatch.context() as own:
            own.setattr(chrome, "STATE", tmp_path / "state/chrome")
            for record in chrome.STATE.glob("*.json"):
                chrome.stop(record.stem)
        runtime.cleanup()
    (tmp_path / "cli.stderr").write_text(result.stderr)
    (tmp_path / "cli.stdout").write_text(result.stdout)
    assert result.stdout, f"CLI exit {result.returncode}; see cli.stderr"
    report = json.loads(result.stdout)
    assert result.returncode == 0, {"exit": result.returncode, "gate": report.get("gate")}
    assert report["gate"] == "PASS" and report["scenarios"][0]["outcome"] == "pass"
    assert report["cost"]["calls"] == {"typesafe": 0, "text": 0}
    data = json.loads(netlog.read_text())
    request_type = data["constants"]["logEventTypes"]["URL_REQUEST_START_JOB"]
    starts = [event for event in data["events"] if event.get("type") == request_type and event.get("phase") != 2]
    hosts = sorted({urlsplit(event["params"]["url"]).hostname for event in starts
                    if urlsplit(event["params"]["url"]).hostname})
    rejected = [host for host in hosts if not (host in {"127.0.0.1", "localhost"}
                or (host.endswith(".localhost") and len(host) > len(".localhost")))]
    receipt = {"request_hosts": hosts, "nonlocal_hosts": rejected, "models": report["cost"]["calls"],
               "request_starts": len(starts),
               "nonlocal_request_starts": sum(
                   urlsplit(event["params"]["url"]).hostname in rejected for event in starts),
               "input_events_and_submit_checked": True, "gate": "FAIL" if rejected else "PASS"}
    (tmp_path / "request-hosts.json").write_text(json.dumps(receipt, indent=2) + "\n")
    assert "127.0.0.1" in hosts
    assert rejected == [], receipt
