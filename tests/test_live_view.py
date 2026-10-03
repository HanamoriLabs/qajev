import time

from qajev import live


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_frames_are_saved_only_while_someone_watches(tmp_path):
    shots = []

    class Fake(live.Frames):
        def capture(self):
            shots.append(time.monotonic())
            return b"\xff\xd8\xff\xe0frame"

    with Fake(tmp_path, "ws://unused", interval=0.02):
        time.sleep(0.2)
        assert shots == [] and live.frame(tmp_path) is None  # nobody watching: nothing captured
        live.touch(tmp_path)
        assert wait_for(lambda: live.frame(tmp_path))
    path, _mtime = live.frame(tmp_path)
    assert path.read_bytes() == b"\xff\xd8\xff\xe0frame" and shots


def test_a_failing_capture_does_not_end_the_frames(tmp_path):
    calls = []

    class Flaky(live.Frames):
        def capture(self):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("page closed")
            return b"\xff\xd8ok"

    live.touch(tmp_path)
    with Flaky(tmp_path, "ws://unused", interval=0.02):
        assert wait_for(lambda: live.frame(tmp_path))


def test_no_run_folder_or_no_page_means_no_frames(tmp_path):
    assert not isinstance(live.frames(None, "ws://x"), live.Frames)
    assert not isinstance(live.frames(tmp_path, None), live.Frames)
