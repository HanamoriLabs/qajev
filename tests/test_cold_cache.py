"""cache: cold: every load of a test skips Chrome's cache and the site's service worker, as a visitor's first visit
does (Verse, 1 Nov: its page crashed only on a cold cache, and a run's reloads came from the warm one)."""

import http.server
import os
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from test_ux_dom_live import ENDPOINT
from test_ux_dom_live import no_model_initialization as no_model_initialization
from test_ux_dom_live import page as page

from qajev import cli, mcp_server, runner, session, suite


def _tab():
    sent = []
    tab = session.Session.__new__(session.Session)
    tab.browser = SimpleNamespace(call=lambda method, **params: sent.append((method, params)) or {})
    tab.headless = True
    return tab, sent


def test_a_cold_cache_turns_off_the_cache_and_the_service_worker_and_back():
    tab, sent = _tab()
    tab.set_cache("cold")
    assert sent == [("Network.enable", {}), ("Network.setCacheDisabled", {"cacheDisabled": True}),
                    ("Network.setBypassServiceWorker", {"bypass": True})]
    tab.set_cache("cold")
    assert len(sent) == 3  # already cold: nothing more to send
    tab.set_cache(None)
    assert sent[3:] == [("Network.enable", {}), ("Network.setCacheDisabled", {"cacheDisabled": False}),
                        ("Network.setBypassServiceWorker", {"bypass": False})]
    tab.set_cache(None)
    assert len(sent) == 6


def test_cache_is_read_from_the_test_or_the_suite_and_only_cold_is_a_word():
    parsed = suite.parse({"cache": "cold", "scenarios": [
        {"name": "a", "url": "http://127.0.0.1/", "about": "x", "expect": {"text": "a"}}]})
    assert parsed.scenarios[0].cache == "cold"
    with pytest.raises(suite.SuiteError, match="cache must be cold"):
        suite.parse({"scenarios": [{"name": "a", "url": "http://127.0.0.1/", "about": "x", "cache": "warm",
                                    "expect": {"text": "a"}}]})
    with pytest.raises(suite.SuiteError, match="not a multiplayer"):
        suite.parse({"scenarios": [{"name": "a", "url": "http://127.0.0.1/", "about": "x", "cache": "cold",
                                    "clients": ["p1", "p2"], "steps": [{"client": "p1", "js": "1"}],
                                    "expect": {"text": "Room"}}]})


@pytest.mark.parametrize("own,run,expected", [("cold", None, "cold"), (None, "cold", "cold"), (None, None, None)])
def test_the_runner_sets_the_tests_cache_before_its_page_loads_and_the_result_says_so(monkeypatch, own, run,
                                                                                      expected):
    from test_check_evidence import Page

    monkeypatch.setattr(runner, "wait_for_quiet", lambda _: (True, 1))
    order = []

    class Caching(Page):
        def set_cache(self, cache):
            order.append(("cache", cache))

        def navigate(self, url):
            order.append(("navigate", url))

    scenario = suite.parse({"scenarios": [{"name": "one", "url": "http://127.0.0.1/", "settle": 0,
                                          **({"cache": own} if own else {}), "expect": {"js": "false"}}]}).scenarios[0]
    result = runner.run_scenario(Caching({}), scenario, opts=runner.Options(shots=False, ux="off", cache=run),
                                 hosts={"127.0.0.1"}, run_dir=None)
    assert order == [("cache", expected), ("navigate", "http://127.0.0.1/")]
    assert result.get("cache") == expected


def test_the_command_line_and_the_mcp_tools_take_both_options():
    args = cli.build_parser().parse_args(["check", "https://shop.example/", "--cold-cache",
                                          "--console-keep", "^Verse", "--console-keep", "boot \\d+"])
    opts = cli._options(args)
    assert opts.cache == "cold" and opts.console_keep == ("^Verse", "boot \\d+")
    assert cli._options(cli.build_parser().parse_args(["check", "https://shop.example/"])).cache is None
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["check", "https://shop.example/", "--console-keep", "(?P<n>x)"])
    assert mcp_server._keep_args(["^Verse"], True) == ["--console-keep=^Verse", "--cold-cache"]
    assert mcp_server._keep_args("-dash first", False) == ["--console-keep=-dash first"]
    assert mcp_server._keep_args(None, False) == []
    with pytest.raises(mcp_server.ToolError, match="Python-only"):
        mcp_server._keep_args(["(?i)x"], False)


@pytest.fixture
def counted():
    hits = {"app.js": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path == "/app.js":
                hits["app.js"] += 1
                body, kind = b"window.loaded = (window.loaded || 0) + 1;", "text/javascript"
            else:
                body, kind = b'<!doctype html><link rel="icon" href="data:,"><script src="/app.js"></script>', \
                    "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "max-age=3600")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.skipif(not ENDPOINT or os.uname().sysname != "Linux", reason="requires approved CPU fixture browser")
def test_browser_loads_from_the_network_every_time_on_a_cold_cache(page, counted):
    session, _ = page
    site, hits = counted
    session.hosts = {urlsplit(site).netloc}
    for _ in range(2):  # a warm cache: the script comes once from the network, then from the cache
        session.navigate(site + "/")
    warm = hits["app.js"]
    session.set_cache("cold")
    for _ in range(2):
        session.navigate(site + "/")
    session.reload()
    assert warm == 1 and hits["app.js"] == warm + 3


def test_smoke_takes_a_cold_cache_but_refuses_console_keep():
    import inspect

    assert "cold_cache" in inspect.signature(mcp_server.qa_smoke).parameters
    assert "console_keep" not in inspect.signature(mcp_server.qa_smoke).parameters
    with pytest.raises(SystemExit, match="--console-keep is for check, run and project run"):
        cli.cmd_smoke(cli.build_parser().parse_args(["smoke", "http://127.0.0.1:1/", "--console-keep", "x"]))
