"""Owned Linux loopback proof for real pointer lock; no model calls or GPU rendering."""

import contextlib
import http.server
import json
import os
import shlex
import sys
import threading
import time

import pytest

from qajev import chrome
from qajev import session as session_mod
from qajev.ledger import Ledger
from qajev.session import Session

pytestmark = pytest.mark.skipif(
    os.environ.get("QAJEV_POINTER_LIVE") != "1" or sys.platform != "linux",
    reason="explicit isolated Linux pointer-lock proof required",
)


@pytest.fixture(scope="module")
def browser(tmp_path_factory):
    assert os.getpriority(os.PRIO_PROCESS, 0) == 19
    root = tmp_path_factory.mktemp("owned-pointer-browser")
    patch = pytest.MonkeyPatch()
    patch.setattr(chrome, "STATE", root / "state")
    patch.setattr(chrome, "PROFILES", root / "profiles")
    patch.setattr(chrome, "EPHEMERAL_ROOT", root / "ephemeral")
    patch.setenv("QAJEV_CHROME_FLAGS", shlex.join([
        "--disable-gpu", "--proxy-server=http://127.0.0.1:9",
        "--proxy-bypass-list=127.0.0.1;localhost;*.localhost",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
        f"--log-net-log={root / 'chrome-netlog.json'}",
    ]))
    owned = None
    try:
        owned = chrome.start("pointer", headless=True, ephemeral=True, port=0)
        session_mod.configure_env(owned["cdp_url"])
        from browser_harness import helpers
        from jev_ultrafast import agent, browser, model

        # load() wraps dependency globals. Restore them as well as closing resources before another test uses them.
        for obj, name in ((helpers, "_send"), (browser, "cdp"), (browser.Browser, "__init__"),
                          (model, "field_text"), (agent, "field_text"), (model, "post_json")):
            patch.setattr(obj, name, getattr(obj, name))
        patch.setattr(session_mod, "_jev", None)
        patch.setattr(session_mod, "_HEADLESS_INPUT_SESSIONS", set())
        yield owned
    finally:
        session_mod.stop_daemon()
        if owned is not None:
            chrome.stop(owned["state_key"])
        patch.undo()


@pytest.fixture(scope="module")
def site():
    body = b'''<!doctype html><title>Pointer fixture</title>
    <canvas id="surface" width="400" height="300" style="background:navy"></canvas>
    <script>
    window.receipt={clicks:0,changes:0,locked:false};
    const canvas=document.querySelector('canvas');
    document.addEventListener('pointerlockchange',()=>{
      receipt.changes++;receipt.locked=document.pointerLockElement===canvas;
    });
    document.addEventListener('pointerlockerror',()=>receipt.denied=true);
    canvas.addEventListener('mousedown',event=>{
      Object.assign(receipt,{clicks:receipt.clicks+1,trusted:event.isTrusted,
        active:navigator.userActivation.isActive,focused:document.hasFocus(),visible:!document.hidden});
      try {Promise.resolve(canvas.requestPointerLock()).catch(error=>{
        receipt.error={name:error.name,message:error.message};
      });} catch(error) {receipt.error={name:error.name,message:error.message};}
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
    try:
        yield f"http://127.0.0.1:{server.server_port}/pointer"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("input_path", ["hook", "jev"])
def test_background_canvas_acquires_real_pointer_lock_after_one_trusted_press(
        browser, site, tmp_path, isolated, input_path, monkeypatch):
    ledger = Ledger(0)
    session = None
    cover = None
    try:
        session = Session(ledger, headless=True, isolated=isolated)
        monkeypatch.setattr(session.jev.model, "post_json", lambda *_a, **_kw: pytest.fail("no model calls"))
        assert session.navigate(site) is None
        context = getattr(session.browser, "context", None)
        cover = session.jev.cdp("Target.createTarget", url="about:blank", background=False,
                                **({"browserContextId": context} if context else {}))["targetId"]
        cover_sid = session.jev.cdp("Target.attachToTarget", targetId=cover, flatten=True)["sessionId"]
        session.jev.cdp("Page.bringToFront", session_id=cover_sid)
        time.sleep(0.1)
        if input_path == "hook":
            session.trusted_click(100, 100)
        else:
            # The pinned Jev operation dispatches through this CDP entry point, bypassing Session.call.
            for kind in ("mousePressed", "mouseReleased"):
                session.jev.browser.cdp("Input.dispatchMouseEvent", session_id=session.browser.session,
                                        type=kind, x=100, y=100, button="left", clickCount=1)
        deadline = time.monotonic() + 5
        while True:
            receipt = session.evaluate("({...receipt,actual:document.pointerLockElement===surface})")
            if receipt.get("actual") or receipt.get("error") or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        (tmp_path / "pointer-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        assert receipt.get("actual") is True, receipt
        assert receipt["clicks"] == 1 and receipt["changes"] == 1
        assert receipt["trusted"] is True and receipt["active"] is True
        assert receipt["locked"] is True and not receipt.get("denied") and not receipt.get("error")
    finally:
        if session is not None:
            with contextlib.suppress(RuntimeError, TimeoutError):
                session.evaluate("document.exitPointerLock()")
            if cover:
                with contextlib.suppress(RuntimeError, TimeoutError):
                    session.jev.cdp("Target.closeTarget", targetId=cover)
            session.close()
