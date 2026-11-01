"""Real CLI negative control: a narrow page with a wider capture cannot pass scripted checks."""

import glob
import http.server
import json
import os
import subprocess
import sys
import threading

import pytest
import test_capture_latency_live as capture_fixture

browser = capture_fixture.browser

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("QAJEV_VIEWPORT_LIVE") != "1",
    reason="explicit isolated CPU Chromium fixture required",
)


@pytest.fixture(scope="module")
def viewport_site():
    content = (b'<title>Viewport fixture</title><body style="margin:0;background:rgb(19,47,83)">'
            b'<p>Viewport ready</p>')

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            meta = b'' if self.path == '/legacy' else (
                b'<meta name="viewport" content="width=device-width,initial-scale=1">')
            body = b'<!doctype html>' + meta + content
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
normal_navigate=session.Session.navigate
def device(self,value):
    normal_device(self,value)
    normal_call(self,'Emulation.setDeviceMetricsOverride',width=width,height=height,
                deviceScaleFactor=1,mobile=False)
def call(self,method,**params):
    if method=='Page.captureScreenshot':
        params={**params,'captureBeyondViewport':True,
                'clip':{'x':0,'y':0,'width':1280,'height':900,'scale':1}}
    result=normal_call(self,method,**params)
    if method=='Page.captureScreenshot':
        pixels=Image.open(io.BytesIO(base64.b64decode(result['data']))).size
        (output/'capture-pixels.json').write_text(json.dumps(pixels))
    return result
def navigate(self,*args,**kwargs):
    result=normal_navigate(self,*args,**kwargs)
    measure='({width:innerWidth,height:innerHeight,scale:devicePixelRatio})'
    before=self.evaluate(measure)
    self.call('Page.captureScreenshot',format='png',fromSurface=True)
    after=self.evaluate(measure)
    (output/'planted-metrics.json').write_text(json.dumps({'before':before,'after':after}))
    return result
session.Session.set_device=device
session.Session.call=call
session.Session.navigate=navigate
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
    for view in (metrics["before"], metrics["after"]):
        assert (view["width"], view["height"], view["scale"]) == (width, height, 1)
    assert json.loads((tmp_path / "capture-pixels.json").read_text()) == [1280, 900]
    assert report["cost"]["calls"] == {"typesafe": 0, "text": 0}
    assert result.returncode != 0 and report["gate"] == "INCOMPLETE", "wrong viewport must not yield CLI PASS"
    row = report["scenarios"][0]
    assert row["outcome"] == "harness" and "viewport" in row["reason"].lower()
    assert str(width if width != 1280 else height) in row["reason"]


@pytest.mark.parametrize("device,width,height,scale,legacy", [
    ("desktop", 1280, 900, 1, False), ("phone", 390, 844, 3, False),
    ("phone", 390, 844, 3, True),
], ids=["desktop", "scaled-phone", "phone-without-viewport-meta"])
def test_real_cli_keeps_matching_desktop_and_scaled_phone_passing(
        browser, viewport_site, tmp_path, device, width, height, scale, legacy):
    cdp, _ = browser
    empty = tmp_path / "empty.env"
    empty.touch(mode=0o600)
    spec = tmp_path / "suite.json"
    spec.write_text(json.dumps({"name": "Viewport positive control", "base_url": viewport_site,
                               "devices": [device], "scenarios": [
        {"name": "matching native viewport", "url": "/legacy" if legacy else "/", "settle": 0,
         "about": "A matching viewport remains valid at its declared device scale.",
         "expect": {"text": "Viewport ready",
                    "js": (f"Math.abs(innerWidth*visualViewport.scale-{width})<1 && "
                           f"Math.abs(innerHeight*visualViewport.scale-{height})<1 && "
                           f"devicePixelRatio==={scale}"),
                    "says": "the page geometry matches its device scale and visual zoom"}}]}))
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


LATE_CAPTURE_CLI = r'''
import base64,io,json,sys
from pathlib import Path
from PIL import Image
from qajev import cli,session,vision
out=Path(sys.argv[1])
def forbidden_judge(*args,**kwargs):
    raise AssertionError('the capture must be rejected before any vision judge call')
vision.require_clef=lambda what:None
vision.look=forbidden_judge
normal_hook=session.Session.run_hook
normal_call=session.Session.call
def measure(page):
    return page.evaluate('({innerWidth,innerHeight,clientWidth:document.documentElement.clientWidth,'
                         'clientHeight:document.documentElement.clientHeight,devicePixelRatio})')
def hook(self,*args,**kwargs):
    result=normal_hook(self,*args,**kwargs)
    normal_call(self,'Emulation.setDeviceMetricsOverride',width=205,height=900,deviceScaleFactor=1,mobile=False)
    return result
def call(self,method,**params):
    if method=='Page.captureScreenshot' and params.get('format')=='jpeg':
        before=measure(self)
        params={**params,'captureBeyondViewport':True,
                'clip':{'x':0,'y':0,'width':1280,'height':900,'scale':1}}
        result=normal_call(self,method,**params)
        after=measure(self)
        pixels=Image.open(io.BytesIO(base64.b64decode(result['data']))).size
        (out/'actual-jpeg.json').write_text(json.dumps({'before':before,'after':after,'pixels':pixels}))
        return result
    return normal_call(self,method,**params)
session.Session.run_hook=hook
session.Session.call=call
raise SystemExit(cli.main(sys.argv[2:]))
'''


@pytest.mark.parametrize("where", ["report", "react", "vision"])
def test_real_cli_refuses_later_bad_frames_after_a_valid_startup(browser, viewport_site, tmp_path, where):
    cdp, _ = browser
    empty = tmp_path / "empty.env"
    empty.touch(mode=0o600)
    spec = tmp_path / "suite.json"
    hooks: list[dict[str, object]] = [{"js": "window.fixtureFrames=0"}]
    if where == "react":
        hooks.append({"react": {
            "for_s": 1, "every_ms": 100, "until": "window.fixtureFrames===1",
            "js": "(()=>{window.fixtureFrames=1;return {shot:'late'};})()"}})
    expect: dict[str, object] = {
        "text": "Viewport ready", "js": "true", "says": "the scripted checks deliberately hold"}
    if where == "vision":
        expect["looks"] = ["the viewport is valid"]
    spec.write_text(json.dumps({"name": "Capture-time geometry fixture", "base_url": viewport_site,
                               "devices": ["desktop"], "scenarios": [
        {"name": "valid startup then narrow capture", "url": "/", "settle": 0,
         "about": "The actual retained or judged frame must match the page at capture time.",
         "before": hooks, "expect": expect}]}))
    env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "PYTHONPATH") if key in os.environ}
    env.update(QAJEV_HOME=str(tmp_path / "home"), QAJEV_OUTLIVE_PARENT="1", PYTHONDONTWRITEBYTECODE="1")
    argv = [sys.executable, "-P", "-c", LATE_CAPTURE_CLI, str(tmp_path), "run", str(spec), "--runner", "local",
            "--headless", "--cdp-url", cdp.endpoint, "--devices", "desktop", "--cost-cap", "0", "--ux", "off",
            "--env-file", str(empty), "--out", str(tmp_path / "reports"), "--json", "--quiet"]
    with (tmp_path / "cli.stdout").open("w") as stdout, (tmp_path / "cli.stderr").open("w") as stderr:
        child = subprocess.run(argv, env=env, stdout=stdout, stderr=stderr, timeout=90)
    report = json.loads((tmp_path / "cli.stdout").read_text())
    shot = json.loads((tmp_path / "actual-jpeg.json").read_text())
    assert report["cost"]["calls"] == {"typesafe": 0, "text": 0}
    assert shot["pixels"] == [1280, 900]
    assert all(view["innerWidth"] == view["clientWidth"] == 205 for view in (shot["before"], shot["after"]))
    row = report["scenarios"][0]
    assert row["viewport"]["before"]["innerWidth"] == 1280, "startup must actually have been correct"
    assert child.returncode == 2 and report["gate"] == "INCOMPLETE"
    assert row["outcome"] == "harness" and row["stop"] == "viewport_mismatch"
    assert row["capture_viewport"]["after"]["clientWidth"] == 205
    assert row["shot"] is None and not any(frame.get("path") for frame in row.get("react", []))


FONT = next(iter(sorted(glob.glob("/usr/share/fonts/**/*.ttf", recursive=True))), None)


@pytest.fixture
def late_font_site():
    """A page that starts a web font after load. The font comes only after GET /release, so it lands mid-capture."""
    released = threading.Event()
    words = " ".join(["Late font ready"] + ["word"] * 60)
    page = ('<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Late font</title>'
            '<body style="margin:0;background:rgb(19,47,83);color:white"><p style="font:40px/1.2 Late,monospace;margin:0">'
            f'{words}</p><script>addEventListener("load",()=>{{const face=new FontFace("Late","url(/late.ttf)",'
            '{sizeAdjust:"900%"});document.fonts.add(face);face.load();});</script>').encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/release":
                released.set()
                body, kind = b"", "text/plain"
            elif self.path == "/late.ttf":
                released.wait(20)
                body, kind = open(FONT, "rb").read(), "font/ttf"
            else:
                body, kind = page, "text/html"
            self.send_response(200)
            self.send_header("Content-Type", kind)
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
        released.set()
        server.shutdown()
        server.server_close()
        worker.join(5)


# Lets the held font go right after the scenario's first capture, before QAJev measures the page again. Only the
# font's timing is set by the test: the font, its layout and the scrollbar are Chrome's own.
LATE_FONT_CLI = r"""
import json,sys,time,urllib.request
from pathlib import Path
from qajev import cli,session
out,base=Path(sys.argv[1]),sys.argv[2]
normal_call=session.Session.call
state={'released':False}
MEASURE=('({fonts:document.fonts.status,tall:document.documentElement.scrollHeight>innerHeight,'
         'clientWidth:document.documentElement.clientWidth,innerWidth})')
def call(self,method,**params):
    first=method=='Page.captureScreenshot' and not state['released']
    if first:
        deadline=time.monotonic()+5  # the page starts the font from its load handler
        while self.evaluate('document.fonts.status')!='loading' and time.monotonic()<deadline:
            time.sleep(0.05)
    result=normal_call(self,method,**params)
    if first:
        state['released']=True
        before=self.evaluate(MEASURE)
        urllib.request.urlopen(base+'release',timeout=5).read()
        deadline=time.monotonic()+10
        while True:
            after=self.evaluate(MEASURE)
            if after['fonts']=='loaded' and after['clientWidth']<after['innerWidth'] or time.monotonic()>deadline:
                break
            time.sleep(0.05)
        (out/'font-release.json').write_text(json.dumps({'before':before,'after':after}))
    return result
session.Session.call=call
raise SystemExit(cli.main(sys.argv[3:]))
"""


@pytest.mark.skipif(FONT is None, reason="no system TrueType font to serve as the late web font")
def test_real_late_font_during_the_startup_capture_is_taken_again_and_passes(browser, late_font_site, tmp_path):
    cdp, _ = browser
    empty = tmp_path / "empty.env"
    empty.touch(mode=0o600)
    spec = tmp_path / "suite.json"
    spec.write_text(json.dumps({"name": "Late font fixture", "base_url": late_font_site,
                               "devices": ["desktop"], "scenarios": [
        {"name": "late web font", "url": "/", "settle": 0,
         "about": "A web font that lands during the startup capture moves the page, and the capture is taken again.",
         "expect": {"text": "Late font ready", "js": "document.fonts.status==='loaded'",
                    "says": "the late web font has loaded"}}]}))
    env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "PYTHONPATH") if key in os.environ}
    env.update(QAJEV_HOME=str(tmp_path / "home"), QAJEV_OUTLIVE_PARENT="1", PYTHONDONTWRITEBYTECODE="1")
    argv = [sys.executable, "-P", "-c", LATE_FONT_CLI, str(tmp_path), late_font_site, "run", str(spec),
            "--runner", "local", "--headless", "--cdp-url", cdp.endpoint, "--devices", "desktop", "--cost-cap", "0",
            "--ux", "off", "--no-shots", "--env-file", str(empty), "--out", str(tmp_path / "reports"), "--json",
            "--quiet"]
    with (tmp_path / "cli.stdout").open("w") as stdout, (tmp_path / "cli.stderr").open("w") as stderr:
        result = subprocess.run(argv, env=env, stdout=stdout, stderr=stderr, timeout=90)
    font = json.loads((tmp_path / "font-release.json").read_text())
    assert font["before"]["fonts"] == "loading" and not font["before"]["tall"]
    assert font["after"]["fonts"] == "loaded" and font["after"]["tall"], "the font must really move the page"
    assert font["after"]["clientWidth"] < font["before"]["clientWidth"], "the font must bring a scrollbar"
    report = json.loads((tmp_path / "cli.stdout").read_text())
    row = report["scenarios"][0]
    assert result.returncode == 0 and report["gate"] == "PASS", row.get("reason")
    assert row["outcome"] == "pass" and row["viewport"]["retakes"] == 1
    assert row["viewport"]["before"] == row["viewport"]["after"]
    assert row["viewport"]["after"]["clientWidth"] == font["after"]["clientWidth"]
