"""Q64/Q65: real Chrome, only loopback fixtures, no model or customer profile."""

import http.server
import json
import os
import threading
import time

import pytest

from qajev import chrome, verdict
from qajev import session as session_mod
from qajev.ledger import Ledger

pytestmark = pytest.mark.skipif(os.environ.get("QAJEV_LIVE") != "1", reason="needs isolated local fixture Chrome")

PAGE = b"""<!doctype html><html lang="en"><title>JavaScript runtime fixture</title>
<main><h1>Runtime fixture</h1><p id="ticks">0</p></main><script>
window.total = 17;
window.fixtureTicks = 0;
setInterval(() => { document.getElementById('ticks').textContent = String(++window.fixtureTicks); }, 20);
</script></html>"""


@pytest.fixture(scope="module")
def page():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(PAGE)

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = f"http://127.0.0.1:{server.server_port}/"
    record = chrome.start(f"js-runtime-{os.getpid()}", headless=True, ephemeral=True)
    session_mod.configure_env(record["cdp_url"])
    session = session_mod.Session(Ledger(0.0), headless=True, hosts={address.split("//")[1].rstrip("/")})
    try:
        session.arm("readonly")
        assert session.navigate(address) is None
        yield session, address
    finally:
        session.close()
        session_mod.stop_daemon()
        chrome.stop(record["state_key"])
        server.shutdown()


def test_real_probe_keeps_the_failed_check_and_declared_values(page):
    session, _ = page
    expect = {"js": "window.total === 20", "explain": "({actual: window.total, expected: 20})"}
    observed = session.probe(expect)
    [check] = verdict.page_checks(expect, observed)
    assert check["ok"] is False and check["value"] == "false"
    assert '"actual": 17' in check["detail"] and '"expected": 20' in check["detail"]


def test_unrounded_page_clock_and_timers_advance_between_js_hooks(page):
    session, address = page
    session.run_hook({"js": "window.runtimeSamples = []"}, address)
    started = time.monotonic()
    for _ in range(6):
        session.run_hook({"js": "window.runtimeSamples.push([performance.now(), window.fixtureTicks])"}, address)
        time.sleep(0.1)
    samples = session.evaluate("window.runtimeSamples")
    elapsed = time.monotonic() - started
    assert len(samples) == 6
    assert all(b[0] > a[0] for a, b in zip(samples, samples[1:])), json.dumps(samples)
    assert samples[-1][0] - samples[0][0] >= 100, (samples, elapsed)
    assert samples[-1][1] > samples[0][1], json.dumps(samples)
    print("clock_between_hooks=" + json.dumps({"samples": samples, "elapsed_s": elapsed}))


def test_unrounded_clock_and_timers_advance_inside_one_async_js_hook(page):
    session, address = page
    session.run_hook({"js": """(async () => {
      window.runtimeAsyncSamples = [];
      for (let i = 0; i < 6; i++) {
        await new Promise(resolve => setTimeout(resolve, 50));
        window.runtimeAsyncSamples.push([performance.now(), window.fixtureTicks]);
      }
    })()"""}, address)
    samples = session.evaluate("window.runtimeAsyncSamples")
    assert len(samples) == 6
    assert all(b[0] > a[0] for a, b in zip(samples, samples[1:])), json.dumps(samples)
    assert samples[-1][0] - samples[0][0] >= 100, json.dumps(samples)
    assert samples[-1][1] > samples[0][1], json.dumps(samples)
    print("clock_inside_hook=" + json.dumps({"samples": samples}))
