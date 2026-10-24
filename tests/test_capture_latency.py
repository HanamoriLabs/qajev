"""Capture preparation keeps the owned Linux surface live without changing the image request."""

import base64
from types import SimpleNamespace

import pytest

from qajev import session as session_mod
from qajev.session import Session


def page(monkeypatch, *, platform="linux", activation_error=None, capture_error=None):
    calls = []
    encoded = base64.b64encode(b"fixture-frame").decode()

    def call(method, **params):
        calls.append((method, params))
        if method == "Page.bringToFront" and activation_error:
            raise activation_error
        if method == "Page.captureScreenshot":
            if capture_error:
                raise capture_error
            return {"data": encoded}
        return {"result": {"value": None}}

    def global_call(*_args, **_params):
        raise AssertionError("capture preparation must use this page's session, not the daemon's default tab")

    monkeypatch.setattr(session_mod, "sys", SimpleNamespace(platform=platform), raising=False)
    session = Session.__new__(Session)
    session.browser = SimpleNamespace(call=call)
    session.jev = SimpleNamespace(cdp=global_call)
    return session, calls


@pytest.mark.parametrize("headless", [False, True])
@pytest.mark.parametrize("params", [
    {"format": "jpeg", "quality": 70},
    {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
    {"format": "png", "fromSurface": True, "captureBeyondViewport": False,
     "clip": {"x": 20, "y": 40, "width": 110, "height": 90, "scale": 1}},
])
def test_linux_capture_activates_its_own_target_before_sending_the_unchanged_request(monkeypatch, headless, params):
    session, calls = page(monkeypatch)
    session.headless = headless
    answer = session.call("Page.captureScreenshot", **params)
    assert calls == [("Page.bringToFront", {}), ("Page.captureScreenshot", params)]
    assert answer == {"data": base64.b64encode(b"fixture-frame").decode()}


@pytest.mark.parametrize("method, params", [
    ("Runtime.evaluate", {"expression": "1 + 1", "returnByValue": True}),
    ("Input.dispatchKeyEvent", {"type": "keyDown", "key": "a"}),
    ("Page.bringToFront", {}),
])
def test_other_page_commands_do_not_activate_a_window(monkeypatch, method, params):
    session, calls = page(monkeypatch)
    session.call(method, **params)
    assert calls == [(method, params)]


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_non_linux_capture_keeps_the_existing_window_focus_policy(monkeypatch, platform):
    session, calls = page(monkeypatch, platform=platform)
    session.call("Page.captureScreenshot", format="png")
    assert calls == [("Page.captureScreenshot", {"format": "png"})]


def test_failed_activation_refuses_capture_instead_of_waiting_on_a_hidden_surface(monkeypatch):
    session, calls = page(monkeypatch, activation_error=RuntimeError("target closed"))
    with pytest.raises(RuntimeError, match="target closed"):
        session.call("Page.captureScreenshot", format="png")
    assert calls == [("Page.bringToFront", {})]


def test_capture_error_is_preserved_after_preparation(monkeypatch):
    session, calls = page(monkeypatch, capture_error=RuntimeError("capture refused"))
    with pytest.raises(RuntimeError, match="capture refused"):
        session.call("Page.captureScreenshot", format="png")
    assert calls == [("Page.bringToFront", {}), ("Page.captureScreenshot", {"format": "png"})]


def test_saved_and_data_url_screenshots_use_the_same_prepared_capture_path(monkeypatch, tmp_path):
    session, calls = page(monkeypatch)
    path = tmp_path / "frame.jpg"
    assert session.screenshot(path, marked=False) == path
    assert path.read_bytes() == b"fixture-frame"
    assert session.screen_image() == "data:image/jpeg;base64," + base64.b64encode(b"fixture-frame").decode()
    assert session.screen_image_png() == "data:image/png;base64," + base64.b64encode(b"fixture-frame").decode()
    capture_calls = [i for i, (method, _) in enumerate(calls) if method == "Page.captureScreenshot"]
    assert len(capture_calls) == 3
    assert all(calls[i - 1] == ("Page.bringToFront", {}) for i in capture_calls)
    assert calls[capture_calls[-1]][1] == {"format": "png", "fromSurface": True, "captureBeyondViewport": False}


def test_react_capture_keeps_each_frame_and_measures_preparation_and_readback(monkeypatch, tmp_path):
    session, calls = page(monkeypatch)
    now = [1.5]
    ordinary_call = session.browser.call

    def timed(method, **params):
        now[0] += 0.025 if method == "Page.bringToFront" else 0.125
        return ordinary_call(method, **params)

    session.browser.call = timed
    monkeypatch.setattr(session_mod.time, "monotonic", lambda: now[0])
    session.shot_dir, session.shot_prefix = tmp_path, "capture"
    first = session._react_shot("same cue", 1.5, 0)
    second = session._react_shot("same cue", now[0], 0)
    assert first == {"shot": "same-cue", "at_s": 1.5, "took_s": 0.15,
                     "path": str(tmp_path / "capture-001-same-cue.jpg")}
    assert second == {"shot": "same-cue", "at_s": 1.65, "took_s": 0.15,
                      "path": str(tmp_path / "capture-002-same-cue.jpg")}
    assert len(list(tmp_path.glob("*.jpg"))) == 2
    assert [method for method, _ in calls] == ["Page.bringToFront", "Page.captureScreenshot"] * 2
