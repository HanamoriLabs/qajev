"""LIVE: the run's own frame grabber while someone watches it live (José, 5 Oct: "click LIVE to watch")."""

import json
import time

import pytest

from qajev import live


def test_live_runs_at_five_frames_a_second_two_when_the_machine_is_busy_one_during_real_time_steps():
    assert live.interval(load1=10, mem_free=50) == pytest.approx(1 / 5)
    assert live.interval(load1=61, mem_free=50) == pytest.approx(1 / 2)
    assert live.interval(load1=10, mem_free=25) == pytest.approx(1 / 2)
    live.REALTIME.set()  # a react hook or a held key: a frame must not steal the game's time
    try:
        assert live.interval(load1=10, mem_free=50) == pytest.approx(1.0)
    finally:
        live.REALTIME.clear()


def test_a_key_or_react_step_runs_in_real_time_and_clears_it_even_when_it_fails(monkeypatch):
    from qajev.session import HookFailed, Session

    page = Session.__new__(Session)
    monkeypatch.setattr(page, "observe", lambda: None, raising=False)  # a hook ends by looking at the page again
    seen = []
    monkeypatch.setattr(page, "press", lambda _v: seen.append(("key", live.REALTIME.is_set())), raising=False)

    def broken(_v):
        seen.append(("react", live.REALTIME.is_set()))
        raise HookFailed("react: policy threw")

    monkeypatch.setattr(page, "react", broken, raising=False)
    page.run_hook({"key": {"press": "s", "hold_ms": 100}}, "http://127.0.0.1/")
    with pytest.raises(HookFailed):
        page.run_hook({"react": {"js": "x()"}}, "http://127.0.0.1/")
    assert seen == [("key", True), ("react", True)] and not live.REALTIME.is_set()


def test_the_frame_grabber_may_only_look_and_bring_its_own_page_to_front():
    grabber = live.Frames("/nowhere", "ws://127.0.0.1:1/devtools/page/x")
    for method in ("Runtime.evaluate", "Input.dispatchKeyEvent", "Network.getCookies", "Target.createTarget"):
        with pytest.raises(PermissionError, match=method):
            grabber.send(method, {})  # refused before any connection is made


def test_watching_live_is_counted_and_written_for_the_report(tmp_path, monkeypatch):
    run = tmp_path / "run"
    live.stream_touch(run)
    grabber = live.Frames(run, "ws://unused", interval=0.05)
    monkeypatch.setattr(grabber, "capture", lambda **_: b"\xff\xd8\xff\xe0jpg")
    monkeypatch.setattr(grabber, "page_url", lambda: "http://127.0.0.1:5173/shop/cart?token=abc#pay")
    monkeypatch.setattr(live, "interval", lambda **_: 0.05)
    with grabber:
        time.sleep(0.6)
    assert (run / "live" / "frame.jpg").read_bytes() == b"\xff\xd8\xff\xe0jpg"
    seconds = json.loads((run / "live" / "watched.json").read_text())["seconds"]
    assert 0.3 <= seconds <= 1.0
    assert json.loads((run / "live" / "page.json").read_text())["url"] == "http://127.0.0.1:5173/shop/cart"
