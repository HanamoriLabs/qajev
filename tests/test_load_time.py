"""expect.load: the time from a page's navigation start to its first playable frame, on a cold cache (29 Oct: a demo
passed 5/5 on a GPU box, and on a laptop it took over 2 min to load)."""

import http.server
import os
import threading
import time
from urllib.parse import urlsplit

import pytest
from test_ux_dom_live import ENDPOINT
from test_ux_dom_live import no_model_initialization as no_model_initialization
from test_ux_dom_live import page as page

from qajev import report, report_files, report_html, runner, session, suite, verdict

READY = "window.game?.playable === true"


def _scenario(load, **extra):
    return suite.parse({"scenarios": [{"name": "loads", "url": "http://127.0.0.1/", "about": "x", "settle": 0,
                                       "expect": {"load": load}, **extra}]}).scenarios[0]


def test_a_load_check_needs_ready_and_whole_milliseconds():
    s = _scenario({"ready": READY, "max_ms": 10000, "failed": "window.game?.failed === true"})
    assert s.expect["load"] == {"ready": READY, "max_ms": 10000, "failed": "window.game?.failed === true"}
    assert suite.has_checks(s.expect)
    assert _scenario({"ready": READY, "max_ms": 100}).expect["load"]["max_ms"] == 100
    for bad, says in (({"max_ms": 10000}, "ready is required"), ({"ready": "", "max_ms": 100}, "ready must be"),
                      ({"ready": READY, "max_ms": 10}, "looks like seconds \\(write 10000\\)"),
                      ({"ready": READY, "max_ms": 2.5}, "whole milliseconds"),
                      ({"ready": READY, "max_ms": 300001}, "100 to 300000"),
                      ({"ready": READY, "max_ms": 99}, "100 to 300000"),
                      ({"ready": READY, "max_ms": True}, "whole milliseconds"),
                      ({"ready": READY, "max_ms": 100, "first": "x"}, "first"), ("fast", "must be")):
        with pytest.raises(suite.SuiteError, match=says):
            _scenario(bad)


def test_a_multiplayer_test_cannot_time_a_load():
    with pytest.raises(suite.SuiteError, match=r"expect: unknown key\(s\) \['load'\]"):
        suite.parse({"scenarios": [{"name": "a", "url": "http://127.0.0.1/", "about": "x", "clients": ["p1", "p2"],
                                    "steps": [{"client": "p1", "js": "1"}],
                                    "expect": {"load": {"ready": READY, "max_ms": 1000}}}]})


def test_the_plan_names_the_load_check_and_its_signals():
    planned = verdict.planned(_scenario({"ready": READY, "max_ms": 10000, "failed": "window.bad"}).expect)
    assert planned == [{"check": "playable within 10 s on a cold cache",
                        "says": "playable within 10 s of the navigation, on a cold cache: the first "
                                "animation frame where 'window.game?.playable === true' returns true; 'window.bad' "
                                "returning true fails the load at once"}]


def test_a_planted_slow_load_names_the_load_check():
    s = _scenario({"ready": READY, "max_ms": 10000}, expect_fail="playable within 10 s on a cold cache",
                  about="A late start (planted: the level starts 15 s late).")
    assert s.expect_fail == {"check": "playable within 10 s on a cold cache",
                             "words": s.expect_fail["words"]}


def test_a_load_check_runs_on_a_cold_cache_even_when_the_suite_names_none():
    assert runner._cache(_scenario({"ready": READY, "max_ms": 1000}), runner.Options(ux="off")) == "cold"
    plain = suite.parse({"scenarios": [{"name": "a", "url": "http://127.0.0.1/", "about": "x",
                                        "expect": {"text": "a"}}]}).scenarios[0]
    assert runner._cache(plain, runner.Options(ux="off")) is None


class Loading:
    """A stand-in tab: wait_load answers what the page's watcher would have seen."""

    def __init__(self, got):
        self.got, self.order = got, []

    def set_device(self, *_):
        pass

    def arm(self, *_):
        pass

    def check_host(self, *_):
        pass

    def set_cache(self, cache):
        self.order.append(("cache", cache))

    def watch_load(self, spec):
        self.order.append(("watch", spec["ready"]))

    def unwatch_load(self):
        self.order.append(("unwatch",))

    def navigate(self, url):
        self.order.append(("navigate", url))

    def browser_clock(self):
        self.order.append(("clock",))
        return 1_000_000.0

    def wait_load(self, spec, started):
        self.order.append(("wait", spec["max_ms"], started))
        return dict(self.got)

    def probe(self, *_):
        return {"url": "http://127.0.0.1/", "probe": {}}


def _run(monkeypatch, got, load=None):
    monkeypatch.setattr(runner, "wait_for_quiet", lambda _: (True, 1))
    tab = Loading(got)
    result = runner.run_scenario(tab, _scenario(load or {"ready": READY, "max_ms": 10000, "failed": "window.bad"}),
                                 opts=runner.Options(shots=False, ux="off"), hosts={"127.0.0.1"}, run_dir=None)
    return result, tab


def test_a_page_playable_within_its_limit_passes_and_is_watched_from_before_its_navigation(monkeypatch):
    result, tab = _run(monkeypatch, {"ms": 4210})
    assert tab.order[:5] == [("cache", "cold"), ("watch", READY), ("clock",), ("navigate", "http://127.0.0.1/"),
                             ("wait", 10000, 1_000_000.0)]
    assert ("unwatch",) in tab.order[5:]
    assert result["outcome"] == "pass", result["reason"]
    assert result["load"] == {"max_ms": 10000, "cache": "cold", "ms": 4210}
    assert result["checks"][0]["check"] == "playable within 10 s on a cold cache"
    assert result["checks"][0]["detail"] == "playable at 4.21 s"


@pytest.mark.parametrize("got,outcome,reason", [
    ({"ms": 10000}, "pass", "all 1 check(s) passed"),  # the limit itself is within it (redteam-1 N1, #274)
    ({"ms": 10001}, "fail", "playable at 10.00 s, over the 10 s limit"),
    ({"ms": None, "waited_ms": 20000, "watched": True, "frames": 1180, "error": "TypeError: x is undefined"}, "fail",
     "not playable after 20.0 s (limit 10 s); ready last threw: TypeError: x is undefined"),
    ({"ms": None, "waited_ms": 3100, "watched": True, "frames": 180, "failed_ms": 3100}, "fail",
     "the page said its load failed at 3.10 s ('window.bad' returned true)"),
    ({"ms": None, "waited_ms": 20000, "watched": False, "frames": 0}, "harness",
     "the ready watcher never ran in the page: check ready and failed for a JavaScript syntax error"),
    ({"ms": None, "waited_ms": 20000, "watched": True, "frames": 0}, "harness",  # redteam-1 L2, #274
     "the page drew no animation frame in 20.0 s, so its load was not judged"),
])
def test_a_slow_failed_or_unwatched_load_is_graded_by_what_the_page_said(monkeypatch, got, outcome, reason):
    result, _ = _run(monkeypatch, got)
    assert result["outcome"] == outcome
    assert reason in result["reason"]


def test_a_page_that_never_loads_names_the_load_check_as_not_run(monkeypatch):
    class Down(Loading):
        def navigate(self, url):
            return "net::ERR_CONNECTION_REFUSED"

    monkeypatch.setattr(runner, "wait_for_quiet", lambda _: (True, 1))
    tab = Down({})
    result = runner.run_scenario(tab, _scenario({"ready": READY, "max_ms": 1000}), opts=runner.Options(
        shots=False, ux="off"), hosts={"127.0.0.1"}, run_dir=None)
    assert not any(o[0] == "wait" for o in tab.order) and tab.order[-1] == ("unwatch",)
    assert "load" not in result


def test_the_watcher_keeps_the_authors_text_as_written():
    sent = []
    tab = session.Session.__new__(session.Session)
    tab.browser = type("B", (), {"call": lambda _self, method, **params: sent.append((method, params))
                                 or {"identifier": "7"}})()
    tab.crash, tab.net = None, None
    tab.watch_load({"ready": "window['__FAILED__'] === true // a comment", "max_ms": 100})
    (method, params), = sent
    assert method == "Page.addScriptToEvaluateOnNewDocument"
    assert "window['__FAILED__'] === true // a comment\n  );" in params["source"]
    assert "const failed = null;" in params["source"]
    tab.unwatch_load()
    assert sent[-1] == ("Page.removeScriptToEvaluateOnNewDocument", {"identifier": "7"})


def test_reports_show_the_load_and_keep_its_frame(monkeypatch, tmp_path):
    load = {"max_ms": 10000, "cache": "cold", "ms": 12345, "shot": "shots/loads-load.jpg"}
    assert report_html.load_summary(load) == "playable at 12.35 s (limit 10 s, cold cache)"
    assert report_html.load_summary({"max_ms": 1000, "ms": None, "waited_ms": 2000, "watched": False}) == \
        "not measured: the ready watcher never ran in the page (limit 1 s, cold cache)"
    assert report_html.load_summary({"max_ms": 1000, "ms": None, "waited_ms": 2000, "watched": True, "frames": 0}) == \
        "not measured: the page drew no animation frame in 2.0 s (limit 1 s, cold cache)"
    assert report_html.load_summary({"max_ms": 1000, "ms": None, "waited_ms": 2000, "watched": True,
                                     "frames": 9}) == "not playable after 2.0 s (limit 1 s, cold cache)"
    assert report_files.image_paths({"scenarios": [{"shot": "shots/loads.jpg", "load": load}]}) == \
        ["shots/loads-load.jpg", "shots/loads.jpg"]
    with pytest.raises(ValueError, match="invalid report .*metadata"):
        report_files.image_paths({"scenarios": [{"load": "fast"}]})
    result, _ = _run(monkeypatch, {"ms": 12345})
    result["load"] = load
    data = report.build(suite.parse({"scenarios": [{"name": "loads", "url": "http://127.0.0.1/", "about": "x",
                                                     "expect": {"load": {"ready": READY, "max_ms": 10000}}}]}),
                        [result], [], browser={}, started_at=time.time(), strict=False, interrupted=False,
                        run_dir=tmp_path)
    md, html = report.markdown(data), report_html.render(data)
    assert "Load: playable at 12.35 s (limit 10 s, cold cache)" in md and "(shots/loads-load.jpg)" in md
    assert "Load: playable at 12.35 s (limit 10 s, cold cache)" in html and 'src="shots/loads-load.jpg"' in html


# A page whose game is playable `?after=<ms>` after its navigation, or says its load failed then (`&fail=1`), or both
# in the same frame (`&both=1`). `?hop=<ms>`: a loader page that moves to the game page (location.replace) that late.
GAME = """<!doctype html><link rel="icon" href="data:,"><canvas width="64" height="64"></canvas><script>
const q = new URLSearchParams(location.search), after = Number(q.get('after') || 0), hop = q.get('hop');
if (hop) setTimeout(() => location.replace('/?after=' + after), Number(hop));
else setTimeout(() => {
  window.game = q.get('both') ? {playable: true, failed: true} : q.get('fail') ? {failed: true} : {playable: true};
}, after);
</script>"""


@pytest.fixture
def game():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            body = GAME.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _measure(tab, url, spec):
    tab.watch_load(spec)
    try:
        started = tab.browser_clock()
        tab.navigate(url)
        return tab.wait_load(spec, started)
    finally:
        tab.unwatch_load()


@pytest.mark.skipif(not ENDPOINT or os.uname().sysname != "Linux", reason="requires approved CPU fixture browser")
def test_browser_times_a_page_from_its_navigation_to_its_first_playable_frame(page, game):
    tab, _ = page
    tab.hosts = {urlsplit(game).netloc}
    fast = _measure(tab, game + "/?after=300", {"ready": READY, "max_ms": 3000})
    assert 300 <= fast["ms"] < 3000 and fast["document_ms"] < 300, fast
    # A loader page that moves on after 1 s is timed from the navigation, not from the last document (redteam-1 L1).
    hop = _measure(tab, game + "/?hop=1000&after=300", {"ready": READY, "max_ms": 5000})
    assert hop["ms"] >= 1300 and hop["document_ms"] >= 1000 and hop["ms"] - hop["document_ms"] >= 300, hop
    # ready and failed in the same frame: failed wins (redteam-1 N2)
    both = _measure(tab, game + "/?after=200&both=1", {"ready": READY, "max_ms": 3000,
                                                       "failed": "window.game?.failed === true"})
    assert both["ms"] is None and 200 <= both["failed_ms"] < 3000, both
    # The planted fault: the same page, 5 s late, against a 2 s limit. QAJev waits twice the limit, then says so.
    slow = _measure(tab, game + "/?after=5000", {"ready": READY, "max_ms": 2000})
    assert slow["ms"] is None and slow["watched"] is True and 4000 <= slow["waited_ms"] < 5000, slow
    assert slow["frames"] > 30, slow  # the page drew its frames all along: a real "not playable"
    failed = _measure(tab, game + "/?after=200&fail=1", {"ready": READY, "max_ms": 3000,
                                                         "failed": "window.game?.failed === true"})
    assert failed["ms"] is None and 200 <= failed["failed_ms"] < 3000, failed
    broken = _measure(tab, game + "/?after=0", {"ready": "window.game?.playable ===", "max_ms": 500})
    assert broken["ms"] is None and broken["watched"] is False and 1000 <= broken["waited_ms"] < 3000, broken
    # After unwatch_load, a later page has no watcher.
    tab.navigate(game + "/?after=0")
    assert tab.evaluate(session.LOAD_STATE_JS)["missing"] is True
