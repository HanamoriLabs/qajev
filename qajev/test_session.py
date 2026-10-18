"""Private, late test-session bootstrap. Never serialize a provider or a login link.

Only a broker installed in this process can enable this path. Its socket is the existing
TLS Namespace enrollment connection; AES-GCM also binds delivery to a run and machine.
The browser bootstrap uses a direct in-memory CDP connection before the daemon, live
frames, model, console and network recorders attach. It never reads browser cookies.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import json
import re
import secrets
import time
from typing import Callable
from urllib.parse import urlsplit


class SessionError(RuntimeError):
    """Fixed safe messages only; upstream text and URL values must never escape."""


@dataclasses.dataclass(frozen=True)
class Policy:
    app: str
    origin: str
    uid: str
    login_path: str
    fragment: str
    clean_path: str

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) != {f.name for f in dataclasses.fields(cls)}:
            raise SessionError("test_session_policy_invalid")
        if any(not isinstance(v, str) for v in value.values()):
            raise SessionError("test_session_policy_invalid")
        u = urlsplit(value["origin"])
        if (
            u.scheme != "https"
            or not u.hostname
            or u.username
            or u.password
            or u.path
            or u.query
            or u.fragment
            or u.netloc != u.hostname
        ):
            raise SessionError("test_session_origin_invalid")
        for key in ("login_path", "clean_path"):
            if not re.fullmatch(r"/[A-Za-z0-9/_-]*", value[key]) or value[key].startswith("//"):
                raise SessionError("test_session_path_invalid")
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value[k]) for k in ("app", "uid", "fragment")):
            raise SessionError("test_session_policy_invalid")
        return cls(**value)

    def verify(self, reply, now):
        if (
            not isinstance(reply, dict)
            or reply.get("test_account") is not True
            or reply.get("uid") != self.uid
            or not isinstance(reply.get("publishable_key"), str)
            or not reply["publishable_key"].startswith("pk_test_")
        ):
            raise SessionError("test_session_identity_rejected")
        end = reply.get("expires_at")
        if isinstance(end, bool) or not isinstance(end, (int, float)) or not now < end <= now + 900:
            raise SessionError("test_session_expired")
        return float(end)

    def validate_link(self, reply, now):
        end = self.verify(reply, now)
        if end > now + 60:
            raise SessionError("test_session_code_expiry_invalid")
        link = reply.get("login_url")
        prefix = self.origin + self.login_path + "#" + self.fragment + "="
        if (
            not isinstance(link, str)
            or not link.startswith(prefix)
            or not re.fullmatch(r"[a-zA-Z0-9_-]{32,128}", link[len(prefix) :])
        ):
            raise SessionError("test_session_link_rejected")
        return link


def binding(run, machine, app, ready_nonce):
    if any(
        not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", v) for v in (run, machine, app, ready_nonce)
    ):
        raise SessionError("test_session_binding_invalid")
    return json.dumps([run, machine, app, ready_nonce], separators=(",", ":")).encode()


def seal(key, aad, value):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    payload = AESGCM(key).encrypt(nonce, json.dumps(value, separators=(",", ":")).encode(), aad)
    return {"sealed": base64.b64encode(nonce + payload).decode()}


def unseal(key, aad, value):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        raw = base64.b64decode(value["sealed"], validate=True)
        return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], aad))
    except (InvalidTag, ValueError, TypeError, KeyError):
        raise SessionError("test_session_delivery_rejected") from None


def send(conn, value):
    raw = json.dumps(value, separators=(",", ":")).encode()
    if len(raw) > 8192:
        raise SessionError("test_session_frame_too_large")
    try:
        conn.sendall(len(raw).to_bytes(4, "big") + raw)
    except OSError:
        raise SessionError("test_session_transport_lost") from None


def receive(conn):
    def exact(n):
        out = b""
        while len(out) < n:
            part = conn.recv(n - len(out))
            if not part:
                raise SessionError("test_session_transport_lost")
            out += part
        return out

    try:
        size = int.from_bytes(exact(4), "big")
        if not 1 <= size <= 8192:
            raise SessionError("test_session_frame_too_large")
        value = json.loads(exact(size))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise SessionError("test_session_transport_rejected") from None


@dataclasses.dataclass
class Lease:
    policy: Policy
    expires_at: float
    now: Callable[[], float] = time.time

    def check(self):
        if self.now() >= self.expires_at:
            raise SessionError("test_session_expired")


def bootstrap(policy, browser, deliver, *, now=time.time):
    try:
        browser.ready()  # incognito target exists before the broker may mint
        reply = deliver(policy)
        link = policy.validate_link(reply, now())
        code_end = reply["expires_at"]
        browser.navigate(link)
        del link, reply
        identity = browser.identity(policy)  # same browser; clean path before recording
        if now() >= code_end:
            raise SessionError("test_session_code_expired")
        end = policy.verify(identity, now())
        return Lease(policy, end, now)
    except Exception:
        browser.close()
        # Transport/browser libraries can include the URL or request body in exceptions.
        raise SessionError("test_session_bootstrap_rejected") from None


class PrivateBrowser:
    """Own an incognito target without the harness daemon or a disk-backed cookie store."""

    def __init__(self, cdp_url):
        from websockets.sync.client import connect

        from . import chrome

        info = chrome.version(cdp_url)
        if not info or not info.get("webSocketDebuggerUrl"):
            raise SessionError("test_session_browser_not_ready")
        self.ws = connect(info["webSocketDebuggerUrl"], max_size=1_000_000, open_timeout=5)
        self.context = self.target = self.session = None
        self.seq = 0

    def call(self, method, *, page=False, **params):
        self.seq += 1
        msg = {"id": self.seq, "method": method, "params": params}
        if page:
            msg["sessionId"] = self.session
        try:
            self.ws.send(json.dumps(msg))
            end = time.monotonic() + 10
            while time.monotonic() < end:
                got = json.loads(self.ws.recv(timeout=max(0.01, end - time.monotonic())))
                if got.get("id") == self.seq:
                    if "error" in got:
                        break
                    return got.get("result") or {}
        except Exception:
            pass
        raise SessionError("test_session_browser_failed") from None

    def ready(self):
        self.context = self.call("Target.createBrowserContext", disposeOnDetach=True)["browserContextId"]
        self.call("Browser.setDownloadBehavior", behavior="deny", browserContextId=self.context)
        for name in ("microphone", "camera"):
            self.call(
                "Browser.setPermission", permission={"name": name}, setting="denied", browserContextId=self.context
            )
        self.target = self.call(
            "Target.createTarget", url="about:blank", browserContextId=self.context, background=True
        )["targetId"]
        self.session = self.call("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        self.call("Page.enable", page=True)
        self.call("Emulation.setFocusEmulationEnabled", page=True, enabled=True)

    def navigate(self, link):
        result = self.call("Page.navigate", page=True, url=link)
        if result.get("errorText"):
            raise SessionError("test_session_navigation_failed")

    def identity(self, policy):
        # Only the final URL and fixed identity fields return. Never observe the bootstrap document.
        expression = (
            "(async()=>{if(location.href!=="
            + json.dumps(policy.origin + policy.clean_path)
            + ")return null;const r=await fetch('/api/v1/test/session',{credentials:'same-origin',"
            + "redirect:'error',cache:'no-store'});if(!r.ok)return null;return r.json()})()"
        )
        end = time.monotonic() + 15
        while time.monotonic() < end:
            result = self.call(
                "Runtime.evaluate",
                page=True,
                expression=expression,
                returnByValue=True,
                awaitPromise=True,
                timeout=5000,
            )
            value = (result.get("result") or {}).get("value")
            if isinstance(value, dict):
                return value
            time.sleep(0.05)
        raise SessionError("test_session_identity_missing")

    def close(self):
        if self.context:
            with contextlib.suppress(Exception):
                self.call("Target.disposeBrowserContext", browserContextId=self.context)
            self.context = None
        with contextlib.suppress(Exception):
            self.ws.close()

    def revoke(self):
        # Fixed app-owned control request, after recorders stop. Cookie remains inside Chrome.
        with contextlib.suppress(Exception):
            self.call(
                "Runtime.evaluate",
                page=True,
                expression="fetch('/api/v1/test/session/revoke',{method:'POST',credentials:'same-origin',redirect:'error',cache:'no-store',headers:{'content-type':'application/json'},body:'{}'}).then(r=>r.ok)",
                returnByValue=True,
                awaitPromise=True,
                timeout=5000,
            )


_provider = None  # installed in memory only by qcloud.runner; never taken from the environment


@contextlib.contextmanager
def provider(callback):
    global _provider
    if _provider is not None:
        raise SessionError("test_session_provider_already_set")
    _provider = callback
    try:
        yield
    finally:
        _provider = None


def prepare(app, cdp_url, *, urls=()):
    if _provider is None:
        raise SessionError("test_session_broker_not_available")
    policy, deliver = _provider(app)
    if any(urlsplit(u).scheme + "://" + urlsplit(u).netloc != policy.origin for u in urls):
        raise SessionError("test_session_target_origin_rejected")
    browser = PrivateBrowser(cdp_url)
    lease = bootstrap(policy, browser, deliver, now=time.time)
    return browser, lease
