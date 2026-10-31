"""Headless mouse input targets the actual active document without changing permissions."""

import sys
from types import SimpleNamespace

import pytest

from qajev import session as session_mod


def dispatcher(*, fail=None):
    sent = []

    def raw(method, session_id=None, **params):
        sent.append((method, session_id, params))
        if method == "Page.bringToFront" and fail:
            raise fail
        return {"receipt": method}

    return session_mod._headless_mouse_cdp(raw, {"owned-headless"}), sent


def test_headless_mouse_press_activates_the_exact_target_before_unchanged_input():
    call, sent = dispatcher()
    params = {"type": "mousePressed", "x": 41, "y": 75, "button": "left", "clickCount": 1, "modifiers": 2}
    result = call("Input.dispatchMouseEvent", session_id="owned-headless", **params)
    assert sent == [("Page.bringToFront", "owned-headless", {}),
                    ("Input.dispatchMouseEvent", "owned-headless", params)]
    assert result == {"receipt": "Input.dispatchMouseEvent"}


@pytest.mark.parametrize("sid", [None, "headed-tab", "another-session"])
def test_unregistered_or_headed_sessions_keep_the_existing_focus_policy(sid):
    call, sent = dispatcher()
    call("Input.dispatchMouseEvent", session_id=sid, type="mousePressed", x=1, y=2)
    assert sent == [("Input.dispatchMouseEvent", sid, {"type": "mousePressed", "x": 1, "y": 2})]


@pytest.mark.parametrize("method,params", [
    ("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left"}),
    ("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 2, "y": 3}),
    ("Input.dispatchMouseEvent", {"type": "mouseWheel", "deltaY": 120, "deltaX": 0}),
    ("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Escape"}),
    ("Runtime.evaluate", {"expression": "document.hasFocus()"}),
    ("Page.bringToFront", {}),
])
def test_non_press_commands_are_not_activated_or_rewritten(method, params):
    call, sent = dispatcher()
    call(method, session_id="owned-headless", **params)
    assert sent == [(method, "owned-headless", params)]


@pytest.mark.parametrize("failure", [RuntimeError("target closed"), TimeoutError("activation timed out")])
def test_failed_activation_sends_no_mouse_input(failure):
    call, sent = dispatcher(fail=failure)
    with pytest.raises(type(failure), match=str(failure)):
        call("Input.dispatchMouseEvent", session_id="owned-headless", type="mousePressed", x=1, y=2)
    assert sent == [("Page.bringToFront", "owned-headless", {})]


def test_repeated_presses_reactivate_after_another_tab_took_the_foreground():
    call, sent = dispatcher()
    for _ in range(2):
        call("Input.dispatchMouseEvent", session_id="owned-headless", type="mousePressed", x=1, y=2)
    assert [method for method, _, _ in sent] == ["Page.bringToFront", "Input.dispatchMouseEvent"] * 2


def test_load_uses_one_prepared_dispatcher_for_hooks_and_jev_direct_input(monkeypatch):
    sent = []

    def raw(method, session_id=None, **params):
        params.pop("_response_timeout")
        sent.append((method, session_id, params))
        return {}

    class Browser:
        def __init__(self, _url):
            pass

    browser = SimpleNamespace(cdp=raw, Browser=Browser)
    model = SimpleNamespace(field_text=lambda _: None, post_json=lambda *_a, **_kw: None)
    monkeypatch.setitem(sys.modules, "browser_harness", SimpleNamespace(
        admin=SimpleNamespace(), helpers=SimpleNamespace(_send=lambda *_a, **_kw: {"result": {}})))
    monkeypatch.setitem(sys.modules, "jev_ultrafast", SimpleNamespace(
        browser=browser, agent=SimpleNamespace(), model=model))
    monkeypatch.setattr(session_mod, "_jev", None)
    monkeypatch.setattr(session_mod, "_HEADLESS_INPUT_SESSIONS", {"owned-headless"})
    monkeypatch.setenv("BU_NAME", "pointer-fixture")
    monkeypatch.setattr(session_mod.providers, "apply", lambda: {})
    monkeypatch.setattr(session_mod.providers, "route", lambda raw, _: raw)
    loaded = session_mod.load(SimpleNamespace(wrap=lambda raw: raw))
    assert loaded.cdp is browser.cdp
    loaded.cdp("Input.dispatchMouseEvent", session_id="owned-headless", type="mousePressed", x=1, y=2)
    browser.cdp("Input.dispatchMouseEvent", session_id="owned-headless", type="mousePressed", x=3, y=4)
    assert sent == [("Page.bringToFront", "owned-headless", {}),
                    ("Input.dispatchMouseEvent", "owned-headless", {"type": "mousePressed", "x": 1, "y": 2}),
                    ("Page.bringToFront", "owned-headless", {}),
                    ("Input.dispatchMouseEvent", "owned-headless", {"type": "mousePressed", "x": 3, "y": 4})]


NEW_HEADLESS_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/153.0.0.0 "
                   "Safari/537.36")


@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("requested,product,user_agent,registered", [
    (True, "Chrome/153", "fixture", True), (False, "HeadlessChrome/153", "fixture", True),
    (False, "Chrome/153", "fixture", False),
    # Chrome 153's new headless mode says "Chrome/153" as its product; only its user agent says headless (31 Oct)
    (False, "Chrome/153.0.8010.52", NEW_HEADLESS_UA, True),
])
def test_real_session_lifecycle_registers_only_headless_targets_and_removes_closed_ones(
        monkeypatch, isolated, requested, product, user_agent, registered):
    sessions = set()
    monkeypatch.setattr(session_mod, "_HEADLESS_INPUT_SESSIONS", sessions)

    def cdp(method, **_params):
        return {"browserContextId": "owned-context", "targetId": "owned-target", "sessionId": "owned-headless",
                "product": product, "userAgent": user_agent}

    browser = SimpleNamespace(session="owned-headless", target="owned-target", call=lambda *_a, **_k: {})
    jev = SimpleNamespace(cdp=cdp, browser=SimpleNamespace(MARKER="fixture"),
                          admin=SimpleNamespace(ensure_daemon=lambda: None),
                          agent=SimpleNamespace(Agent=lambda *_a: SimpleNamespace(browser=browser, close=lambda: None)))
    monkeypatch.setattr(session_mod, "load", lambda *_a: jev)
    monkeypatch.setattr(session_mod.netlog, "start", lambda *_a: None)
    for name in ("_own_user_agent", "_keep_safe", "minimize", "page_socket"):
        monkeypatch.setattr(session_mod.Session, name, lambda *_a: None)
    session = session_mod.Session(None, headless=requested, isolated=isolated)
    assert sessions == ({"owned-headless"} if registered else set())
    session.close()
    assert sessions == set()
