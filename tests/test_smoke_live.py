"""A smoke run shows its live screen, as a check or a suite does (José, 12 Oct: cloud smokes said "No live screen yet").

The live frame writer (live.frames) runs only while someone watches, and saves <run>/live/frame.jpg; the dashboard and
QAJev Cloud show it. A suite starts it around each test; smoke never started it, so a smoke had no live screen anywhere.
A fake browser stands in for Chrome here: no browser starts.
"""

import contextlib
from types import SimpleNamespace

from qajev import chrome, live, smoke
from qajev import session as session_mod


class FakeSession:
    def __init__(self, *_, **__):
        self.closed = False

    def set_device(self, *_):
        pass

    def arm(self, *_):
        pass

    def page_socket(self):
        return "ws://127.0.0.1:9/devtools/page/fake"

    def navigate(self, _url):
        return None

    def evaluate(self, _):
        return {"url": "https://shop.example/", "status": 200, "links": []}

    def probe(self, _):
        return {"probe": {"lcp": 1234, "cls": 0.05}}

    def close(self):
        self.closed = True


def fake_browser(monkeypatch, tmp_path):
    monkeypatch.setattr(chrome, "reap", lambda: [])
    monkeypatch.setattr(chrome, "start", lambda *a, **k: {"cdp_url": "http://127.0.0.1:9", "state_key": "k"})
    monkeypatch.setattr(chrome, "run_record", lambda owned: {"managed": True})
    monkeypatch.setattr(smoke, "robots_for", lambda url: None)
    monkeypatch.setattr(smoke, "wait_for_quiet", lambda opts: (True, 1.0))
    monkeypatch.setattr(smoke, "served_file", lambda url: None)
    monkeypatch.setattr(session_mod, "configure_env", lambda url: None)
    monkeypatch.setattr(session_mod, "Session", FakeSession)
    monkeypatch.setattr(session_mod, "stop_daemon", lambda: None)
    opts = SimpleNamespace(emit=None, out_dir=tmp_path, cdp_url=None, profile="default", headless=False, ephemeral=True,
                           motion=None, cpu_throttle=None, color_scheme=None, shots=False, ux=False, strict=False)
    return opts


def test_a_smoke_run_starts_the_live_frame_writer_on_its_page_for_the_whole_crawl(tmp_path, monkeypatch):
    seen = []

    @contextlib.contextmanager
    def frames(run_dir, socket, **_):
        seen.append(("start", run_dir, socket))
        yield
        seen.append(("stop",))

    monkeypatch.setattr(live, "frames", frames)
    opts = fake_browser(monkeypatch, tmp_path)

    built = smoke.run("https://shop.example/", opts, max_pages=1, device="desktop", settle=0)

    assert [s[0] for s in seen] == ["start", "stop"]
    assert seen[0][2] == "ws://127.0.0.1:9/devtools/page/fake"
    assert seen[0][1].parent == tmp_path  # the run's own folder: that is where the live screen is read from
    # figgyz.com, 12 Oct: smoke measured LCP and CLS but kept them out of the report. Each page's result has them now.
    page = built["scenarios"][0]
    assert (page["lcp"], page["cls"]) == (1234, 0.05)


def test_a_smoke_run_names_its_site_without_the_urls_user_info(tmp_path, monkeypatch):
    # redteam, 13 Oct: the name was "smoke {netloc}", so "alice:s3cret@host" reached the report folder's name on the Mac
    monkeypatch.setattr(live, "frames", lambda *a, **k: contextlib.nullcontext())
    opts = fake_browser(monkeypatch, tmp_path)

    built = smoke.run("https://alice:s3cret@shop.example:8443/", opts, max_pages=1, device="desktop", settle=0)

    assert built["suite"] == "smoke shop.example:8443"
    folders = [p.name for p in tmp_path.iterdir()]
    assert len(folders) == 1 and "-smoke-shop.example-8443-" in folders[0]
