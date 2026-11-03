"""A dark readback is evidence of a dark frame only when the page drew in that frame (runner.drawn_dark).

redteam-1's known-answer tests, 3 Nov: a black-frame fault must fail on its look, so with canvas_capture: page Clef
judges a dark screen whose own frame is dark. A readback is no evidence when the drawing buffer holds no frame the
page drew in that one (O1: with alpha: false a cleared buffer reads opaque black), when it is transparent, or when
it is lit. These run the readback in a real Chrome; test_looks_blank grades the results.
"""

import json
import os
from types import SimpleNamespace

import pytest
from test_live import Recorder, browser, session, site  # noqa: F401  (the fixture server and its Chrome)

from qajev import runner
from qajev import session as session_mod

LIVE = pytest.mark.skipif(os.environ.get("QAJEV_LIVE") != "1", reason="set QAJEV_LIVE=1 (starts a local Chrome)")
GRAB = "document.getElementById('view').toDataURL()"


def frame(session, site, page, tmp_path):  # noqa: F811
    session.count_draws()
    assert session.navigate(f"{site}/{page}") is None
    session.evaluate("new Promise((done) => setTimeout(done, 300))")  # a few frames drawn
    return runner.drawn_dark(session, SimpleNamespace(canvas_capture={"page": GRAB}, name=page), tmp_path)


@LIVE
def test_a_page_that_clears_to_black_in_its_frame_is_a_dark_frame(session, site, tmp_path):  # noqa: F811
    got = frame(session, site, "frame-black.html", tmp_path)
    assert got["dark"] is True and got["draws"] >= 1 and not got["transparent"], got
    assert (tmp_path / got["image"]).is_file()


@LIVE
def test_a_lit_frame_is_not_dark(session, site, tmp_path):  # noqa: F811
    got = frame(session, site, "frame-lit.html", tmp_path)
    assert got["dark"] is False and got["blank"] is None and got["draws"] >= 1, got


@pytest.mark.parametrize("page", ["frame-timer.html", "frame-once.html"])
@LIVE
def test_a_frame_the_page_did_not_draw_in_is_no_evidence(session, site, tmp_path, page):  # noqa: F811
    # timer: it draws between frames; once: it drew at load only. Either way no draw call in the readback's frame.
    got = frame(session, site, page, tmp_path)
    assert got["draws"] == 0 and got["dark"] is False, got


@LIVE
def test_a_page_callback_after_qajevs_in_the_same_frame_is_not_counted_yet(session, site):  # noqa: F811
    # redteam-1 Ask 1: QAJev's frame callback is registered first here, then the page's, which clears to a lit colour.
    # The readback runs before the page draws, so it sees 0 draws and is no evidence, whatever its pixels say. The
    # page's callback, later in the same frame, sees its own draw counted.
    session.count_draws()
    assert session.navigate(f"{site}/frame-once.html") is None
    session.evaluate("new Promise((done) => setTimeout(done, 300))")
    readback = session_mod.IN_FRAME.format(expression=GRAB)
    got = session.evaluate(
        f"(() => {{ const read = {readback}; let after = null; requestAnimationFrame(() => {{ paint(0.8, 0.7, 0.5, 1); "
        "after = window[Symbol.for('qajev.frameDraws')](); }); "
        "return read.then((r) => new Promise((done) => setTimeout(() => done({draws: r.draws, after}), 100))); })()")
    assert got == {"draws": 0, "after": 1}, json.dumps(got)


@LIVE
def test_a_transparent_frame_is_no_evidence(session, site, tmp_path):  # noqa: F811
    got = frame(session, site, "frame-clear.html", tmp_path)
    assert got["transparent"] is True and got["draws"] >= 1 and got["dark"] is False, got
