"""Opt-in Linux fixture for real viewport captures from inactive and minimized targets.

Uses one owned headless Chrome with software rendering, loopback pages and no models.
The fixture does not run any application suite or change a machine's browser limit.
"""

import base64
import contextlib
import http.server
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from qajev import chrome
from qajev.session import Session

pytestmark = pytest.mark.skipif(
    os.environ.get("QAJEV_CAPTURE_LIVE") != "1" or sys.platform != "linux",
    reason="explicit isolated Linux capture proof required",
)


class CDP:
    def __init__(self, endpoint):
        from websockets.sync.client import connect

        self.endpoint = endpoint
        with urllib.request.urlopen(endpoint + "/json/version", timeout=5) as reply:
            self.version = json.load(reply)
        self.socket = connect(self.version["webSocketDebuggerUrl"], max_size=32 * 1024**2)
        self.sequence = 0

    def call(self, method, session_id=None, **params):
        self.sequence += 1
        sequence = self.sequence
        request = {"id": sequence, "method": method, "params": params}
        if session_id:
            request["sessionId"] = session_id
        self.socket.send(json.dumps(request))
        deadline = time.monotonic() + 8
        while True:
            reply = json.loads(self.socket.recv(timeout=max(0.001, deadline - time.monotonic())))
            if reply.get("id") == sequence:
                if reply.get("error"):
                    raise RuntimeError(str(reply["error"]))
                return reply.get("result") or {}
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{method}: fixture response budget exhausted")


@pytest.fixture(autouse=True)
def no_model_initialization(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("capture fixtures must not initialize Jev or a model")

    monkeypatch.setattr("qajev.session.load", forbidden)


@pytest.fixture(scope="module")
def browser(tmp_path_factory):
    assert os.getpriority(os.PRIO_PROCESS, 0) == 19
    root = tmp_path_factory.mktemp("owned-capture-browser")
    profile = root / "profile"
    profile.mkdir(mode=0o700)
    argv = [chrome.binary(), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-background-networking", "--disable-component-update", "--disable-sync",
            "--disable-extensions", "--disable-component-extensions-with-background-pages",
            "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0", f"--user-data-dir={profile}",
            "--proxy-server=http://127.0.0.1:9", "--proxy-bypass-list=127.0.0.1;localhost;*.localhost",
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
            f"--log-net-log={root / 'chrome-netlog.json'}", "about:blank"]
    child = None
    cdp = None
    try:
        with (root / "chromium.log").open("w") as log:
            started = time.monotonic()
            child = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = started + 60  # normal Chrome startup budget; per-frame performance is measured separately
            active = profile / "DevToolsActivePort"
            while not active.is_file():
                assert child.poll() is None, "owned Chrome exited; see chromium.log"
                assert time.monotonic() < deadline, "owned Chrome did not publish its port"
                time.sleep(0.05)
            endpoint = "http://127.0.0.1:" + active.read_text().splitlines()[0]
            cdp = CDP(endpoint)
            (root / "browser.json").write_text(json.dumps({"endpoint": endpoint, "version": cdp.version,
                                                          "startup_seconds": time.monotonic() - started,
                                                          "argv": argv}, indent=2) + "\n")
            yield cdp, root
    finally:
        if cdp is not None:
            cdp.socket.close()
        if child is not None and child.poll() is None:
            if os.getpgid(child.pid) == child.pid:
                os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                if os.getpgid(child.pid) == child.pid:
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)


@pytest.fixture(scope="module")
def site():
    body = (b'<!doctype html><title>Capture fixture</title>'
            b'<body style="margin:0;background:rgb(18,71,123);min-height:200vh">'
            b'<h1>Capture fixture</h1><p id="frame">Frame zero</p>')

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/fixture"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("state", ["background", "minimized"])
@pytest.mark.parametrize("device", [(1280, 900, 1), (390, 844, 3)])
@pytest.mark.parametrize("kind", ["png", "react-jpeg"])
def test_inactive_target_captures_fresh_native_pixels_without_a_thirty_second_wait(
        browser, site, tmp_path, state, device, kind):
    cdp, _root = browser
    targets = []
    rows = []
    try:
        target = cdp.call("Target.createTarget", url="about:blank", background=True)["targetId"]
        targets.append(target)
        sid = cdp.call("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        other = cdp.call("Target.createTarget", url="about:blank")["targetId"]
        targets.append(other)
        other_sid = cdp.call("Target.attachToTarget", targetId=other, flatten=True)["sessionId"]
        cdp.call("Runtime.evaluate", session_id=other_sid,
                 expression="document.body.style.backgroundColor='rgb(230,18,101)'")
        session = Session.__new__(Session)
        session.browser = SimpleNamespace(target=target,
                                         call=lambda method, **params: cdp.call(method, session_id=sid, **params))
        session.jev, session.agent, session.headless = SimpleNamespace(cdp=cdp.call), object(), False
        session.shot_dir, session.shot_prefix = tmp_path, "native"
        width, height, scale = device
        session.call("Page.enable")
        session.call("Emulation.setFocusEmulationEnabled", enabled=True)  # Jev also keeps background JS focused
        session.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
                     deviceScaleFactor=scale, mobile=width < 500)
        session.call("Page.navigate", url=site)
        deadline = time.monotonic() + 10
        while session.evaluate("document.readyState") != "complete":
            assert time.monotonic() < deadline
            time.sleep(0.02)
        colors = [(18, 71, 123), (41, 152, 73), (162, 53, 29)]
        for index, color in enumerate(colors, 1):
            session.call("Page.bringToFront")
            if state == "minimized":
                session.minimize()
                window = cdp.call("Browser.getWindowForTarget", targetId=target)
                assert window["bounds"]["windowState"] == "minimized", window
            else:
                cdp.call("Page.bringToFront", session_id=other_sid)
            # Focus emulation can report a visible document while its compositor surface is still inactive.
            visibility = session.evaluate("({hidden:document.hidden,focused:document.hasFocus()})")
            session.evaluate("document.body.style.backgroundColor=" + json.dumps(f"rgb{color}") + ";"
                             f"document.getElementById('frame').textContent='Frame {index}'")
            began = time.monotonic()
            if kind == "png":
                value = session.screen_image_png()
                data = base64.b64decode(value.split(",", 1)[1], validate=True)
                path = tmp_path / f"frame-{index}.png"
                path.write_bytes(data)
                frame = None
            else:
                frame = session._react_shot("same cue", 0, began)
                path = Path(frame["path"]) if frame["path"] else None
                assert path is not None and path.is_file(), "capture must retain a frame, not silently skip it"
                data = path.read_bytes()
            elapsed = time.monotonic() - began
            image = Image.open(io.BytesIO(data)).convert("RGB")
            assert image.size == (width * scale, height * scale), "capture must keep the native viewport size"
            pixel = image.getpixel((20 * scale, (height - 20) * scale))
            assert all(abs(a - b) <= 4 for a, b in zip(pixel, color)), (pixel, color)
            assert elapsed < 5, f"capture took {elapsed:.3f}s; see capture-timing.json"
            if frame:
                assert 0 <= frame["took_s"] <= elapsed + 0.01
                assert Path(frame["path"]).name == f"native-{index:03d}-same-cue.jpg"
            rows.append({"frame": index, "elapsed_seconds": elapsed, "pixel": pixel,
                         "size": image.size, "react": frame, "visibility_before_capture": visibility})
        assert len(rows) == 3
        if kind == "react-jpeg":
            assert len(list(tmp_path.glob("native-*.jpg"))) == 3
    finally:
        (tmp_path / "capture-timing.json").write_text(json.dumps({"state": state, "device": device,
                                                                "kind": kind, "frames": rows}, indent=2) + "\n")
        for target in reversed(targets):
            with contextlib.suppress(RuntimeError, TimeoutError):
                cdp.call("Target.closeTarget", targetId=target)


def test_cli_retains_all_requested_frames_and_checks_with_zero_model_calls(browser, site, tmp_path):
    cdp, _root = browser
    suite = tmp_path / "capture.json"
    suite.write_text(json.dumps({
        "name": "Native capture fixture", "base_url": site, "devices": ["desktop"],
        "scenarios": [{"name": "three fresh frames", "url": site,
                       "about": "Keep each requested viewport frame and judge every authored check.",
                       "before": [{"js": "window.__captureFrame=0"}, {"react": {
                           "for_s": 10, "every_ms": 100, "until": "window.__captureFrame===3",
                           "js": "(()=>{window.__captureFrame++;document.getElementById('frame').textContent="
                                 "'Frame '+window.__captureFrame;return {shot:'same cue'};})()"}}],
                       "expect": {"text": "Capture fixture", "js": "window.__captureFrame===3"}}],
    }))
    empty = tmp_path / "empty.env"
    empty.touch(mode=0o600)
    env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "PYTHONPATH") if key in os.environ}
    env.update(QAJEV_HOME=str(tmp_path / "home"), QAJEV_RUNNER="local", QAJEV_OUTLIVE_PARENT="1",
               QAJEV_LOCK_WAIT="3600")
    argv = [sys.executable, "-P", "-m", "qajev", "run", str(suite), "--runner", "local",
            "--cdp-url", cdp.endpoint, "--env-file", str(empty), "--cost-cap", "0", "--ux", "off",
            "--out", str(tmp_path / "reports"), "--json", "--events"]
    child = None
    try:
        with (tmp_path / "cli.stdout").open("w") as stdout, (tmp_path / "cli.stderr").open("w") as stderr:
            child = subprocess.Popen(argv, env=env, stdout=stdout, stderr=stderr)
            assert child.wait(timeout=90) == 0, "see cli.stdout and cli.stderr"
        data = json.loads((tmp_path / "cli.stdout").read_text())
        assert data["gate"] == "PASS" and data["cost"]["calls"] == {"typesafe": 0, "text": 0}
        scenario = data["scenarios"][0]
        assert len(scenario["checks"]) == 2 and all(check["ok"] is True for check in scenario["checks"])
        frames = [entry for entry in scenario["react"] if entry.get("path")]
        assert len(frames) == 3
        assert len({entry["path"] for entry in frames}) == 3
        for frame in frames:
            assert frame["took_s"] < 5
            image = Image.open(Path(data["run_dir"]) / frame["path"])
            assert image.size == (1280, 900)
        (tmp_path / "cli-capture.json").write_text(json.dumps({
            "gate": data["gate"], "model_calls": data["cost"]["calls"], "timing": data["timing"],
            "frames": frames, "all_checks_kept": True}, indent=2) + "\n")
    finally:
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGINT)
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
