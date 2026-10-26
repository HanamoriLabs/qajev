"""Real CLI negative control: a narrow page with a wider capture cannot pass scripted checks."""

import http.server
import json
import os
import subprocess
import sys
import threading

import pytest

from test_capture_latency_live import browser  # noqa: F401

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("QAJEV_VIEWPORT_LIVE") != "1",
    reason="explicit isolated CPU Chromium fixture required",
)


@pytest.fixture(scope="module")
def viewport_site():
    body = (b'<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
            b'<title>Viewport fixture</title><body style="margin:0;background:rgb(19,47,83)">'
            b'<p>Viewport ready</p>')

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


# This planted corruption changes real CDP metrics and the capture request, not the runner's result or checks.
PLANTED_CLI = r'''
import base64,io,json,sys
from pathlib import Path
from PIL import Image
from qajev import cli,session
output=Path(sys.argv[1]);width,height=map(int,sys.argv[2:4])
normal_device=session.Session.set_device
normal_call=session.Session.call
def device(self,value):
    normal_device(self,value)
    normal_call(self,'Emulation.setDeviceMetricsOverride',width=width,height=height,
                deviceScaleFactor=1,mobile=False)
    metrics=self.evaluate('({width:innerWidth,height:innerHeight,scale:devicePixelRatio})')
    (output/'planted-metrics.json').write_text(json.dumps(metrics))
def call(self,method,**params):
    if method=='Page.captureScreenshot':
        params={**params,'captureBeyondViewport':True,
                'clip':{'x':0,'y':0,'width':1280,'height':900,'scale':1}}
    result=normal_call(self,method,**params)
    if method=='Page.captureScreenshot':
        pixels=Image.open(io.BytesIO(base64.b64decode(result['data']))).size
        (output/'capture-pixels.json').write_text(json.dumps(pixels))
    return result
session.Session.set_device=device
session.Session.call=call
raise SystemExit(cli.main(sys.argv[4:]))
'''


@pytest.mark.parametrize("width,height", [(205, 900), (1280, 205)], ids=["205px-window", "wrong-height"])
def test_real_narrow_window_cli_is_harness_even_when_its_scripted_checks_hold(
        browser, viewport_site, tmp_path, width, height):
    cdp, _ = browser
    empty = tmp_path / "empty.env"
    empty.touch(mode=0o600)
    spec = tmp_path / "suite.json"
    spec.write_text(json.dumps({"name": "Viewport negative control", "base_url": viewport_site,
                               "devices": ["desktop"], "scenarios": [
        {"name": "script holds in the wrong viewport", "url": "/", "settle": 0,
         "about": "A real narrow viewport is refused before its otherwise passing script can grade the run.",
         "expect": {"text": "Viewport ready", "js": "true", "says": "the scripted check deliberately holds"}}]}))
    env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "PYTHONPATH") if key in os.environ}
    env.update(QAJEV_HOME=str(tmp_path / "home"), QAJEV_OUTLIVE_PARENT="1", PYTHONDONTWRITEBYTECODE="1")
    argv = [sys.executable, "-P", "-c", PLANTED_CLI, str(tmp_path), str(width), str(height),
            "run", str(spec), "--runner", "local", "--headless", "--cdp-url", cdp.endpoint,
            "--devices", "desktop", "--cost-cap", "0", "--ux", "off", "--no-shots",
            "--env-file", str(empty), "--out", str(tmp_path / "reports"), "--json", "--quiet"]
    with (tmp_path / "cli.stdout").open("w") as stdout, (tmp_path / "cli.stderr").open("w") as stderr:
        result = subprocess.run(argv, env=env, stdout=stdout, stderr=stderr, timeout=90)
    report = json.loads((tmp_path / "cli.stdout").read_text())
    metrics = json.loads((tmp_path / "planted-metrics.json").read_text())
    assert (metrics["width"], metrics["height"]) == (width, height)
    assert report["cost"]["calls"] == {"typesafe": 0, "text": 0}
    assert result.returncode != 0 and report["gate"] == "INCOMPLETE", "wrong viewport must not yield CLI PASS"
    row = report["scenarios"][0]
    assert row["outcome"] == "harness" and "viewport" in row["reason"].lower()
    assert str(width if width != 1280 else height) in row["reason"]
    assert json.loads((tmp_path / "capture-pixels.json").read_text()) == [1280, 900]


@pytest.mark.parametrize("device,width,height,scale", [
    ("desktop", 1280, 900, 1), ("phone", 390, 844, 3),
])
def test_real_cli_keeps_matching_desktop_and_scaled_phone_passing(
        browser, viewport_site, tmp_path, device, width, height, scale):
    cdp, _ = browser
    empty = tmp_path / "empty.env"
    empty.touch(mode=0o600)
    spec = tmp_path / "suite.json"
    spec.write_text(json.dumps({"name": "Viewport positive control", "base_url": viewport_site,
                               "devices": [device], "scenarios": [
        {"name": "matching native viewport", "url": "/", "settle": 0,
         "about": "A matching viewport remains valid at its declared device scale.",
         "expect": {"text": "Viewport ready",
                    "js": f"innerWidth==={width} && innerHeight==={height} && devicePixelRatio==={scale}"}}]}))
    env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "PYTHONPATH") if key in os.environ}
    env.update(QAJEV_HOME=str(tmp_path / "home"), QAJEV_OUTLIVE_PARENT="1", PYTHONDONTWRITEBYTECODE="1")
    argv = [sys.executable, "-P", "-m", "qajev", "run", str(spec), "--runner", "local", "--headless",
            "--cdp-url", cdp.endpoint, "--devices", device, "--cost-cap", "0", "--ux", "off", "--no-shots",
            "--env-file", str(empty), "--out", str(tmp_path / "reports"), "--json", "--quiet"]
    with (tmp_path / "cli.stdout").open("w") as stdout, (tmp_path / "cli.stderr").open("w") as stderr:
        result = subprocess.run(argv, env=env, stdout=stdout, stderr=stderr, timeout=90)
    report = json.loads((tmp_path / "cli.stdout").read_text())
    assert result.returncode == 0 and report["gate"] == "PASS", "matching native viewport must remain usable"
    assert report["cost"]["calls"] == {"typesafe": 0, "text": 0}
    row = report["scenarios"][0]
    assert row["outcome"] == "pass" and all(check["ok"] for check in row["checks"])
