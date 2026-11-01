"""color_scheme: a test tells the page the visitor's system prefers light or dark (foleyapp, 12 Oct 2026).

Foley promises that "dark mode follows the system setting". QAJev could not set that preference, so no test could
prove it. Now a scenario, a suite or a project objective says `color_scheme: light | dark`, and `qajev check
--color-scheme` does the same; the page's `prefers-color-scheme` media query then answers as on such a system.

The live half runs in QAJev's own throwaway Chrome, in a visible window (never headless). Nothing leaves the machine.
"""

import http.server
import os
import threading
from functools import partial

import pytest

from qajev import project as P
from qajev.suite import SuiteError, parse

live = pytest.mark.skipif(os.environ.get("QAJEV_LIVE") != "1", reason="set QAJEV_LIVE=1 (starts a local Chrome)")


def suite(**over):
    data = {"name": "s", "base_url": "http://localhost:5000",
            "scenarios": [{"name": "dark", "url": "/", "expect": {"text": ["x"]}, "color_scheme": "dark"},
                          {"name": "plain", "url": "/", "expect": {"text": ["x"]}}]}
    data.update(over)
    return data


def test_a_scenario_names_its_color_scheme_and_others_take_the_suites():
    s = parse(suite())
    assert [x.color_scheme for x in s.scenarios] == ["dark", None]
    s = parse(suite(color_scheme="light"))
    assert [x.color_scheme for x in s.scenarios] == ["dark", "light"]


@pytest.mark.parametrize("bad", ["Dark", "auto", "", 1])
def test_only_light_or_dark_is_accepted(bad):
    with pytest.raises(SuiteError, match="color_scheme"):
        parse(suite(scenarios=[{"name": "a", "url": "/", "expect": {"text": ["x"]}, "color_scheme": bad}]))


def test_a_project_objective_may_name_its_color_scheme(tmp_path, monkeypatch):
    monkeypatch.setenv("QAJEV_PROJECTS", str(tmp_path / "projects"))
    monkeypatch.setattr(P, "_source_root", lambda: None)
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "site.toml").write_text(
        'name = "site"\n[env.prod]\nbase_url = "https://site.example"\n'
        '[[objective]]\nname = "dark"\ncolor_scheme = "dark"\nexpect = { text = ["x"] }\n')
    suite_data = P.suite_data(P.load("site"), env="prod")
    assert suite_data["scenarios"][0]["color_scheme"] == "dark"


# ---- live: the page's media query answers as the test said ----

PAGE = ("<!doctype html><title>theme</title><script>"
        "window.scheme = () => matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';"
        "</script><p>Theme page</p>")


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("color-scheme")
    (root / "page.html").write_text(PAGE)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(http.server.SimpleHTTPRequestHandler,
                                                                       directory=str(root)))
    server.RequestHandlerClass.log_message = lambda *_: None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    from qajev import chrome

    if why := chrome.display_missing():  # a visible Chrome: with no display the tests skip and say why
        pytest.skip(why)

    record = chrome.start(f"color-scheme-{os.getpid()}", headless=False, ephemeral=True)  # visible, never headless
    yield record
    chrome.stop(record["state_key"])


@live
def test_the_page_sees_the_scheme_each_test_names_and_keeps_reduced_motion(site, browser):
    from qajev import session as session_mod
    from qajev.ledger import Ledger

    session_mod.configure_env(browser["cdp_url"])
    s = session_mod.Session(Ledger(0.0), headless=False, hosts={f"127.0.0.1:{site}"})
    try:
        s.set_device({"width": 1280, "height": 900, "mobile": False, "scale": 1})
        s.arm("readonly")
        seen = []
        for scheme in ("dark", "light", "dark"):
            s.set_color_scheme(scheme)
            assert s.navigate(f"http://127.0.0.1:{site}/page.html") is None
            seen.append(s.evaluate("window.scheme()"))
        assert seen == ["dark", "light", "dark"]
        # Setting the scheme must not drop the reduced-motion preference QAJev sets by default.
        assert s.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches") is True
    finally:
        s.close()
        session_mod.stop_daemon()


def test_the_command_line_scheme_reaches_every_test_that_names_none():
    from qajev import cli

    args = cli.build_parser().parse_args(["check", "https://shop.example/", "--color-scheme", "dark"])
    assert cli._options(args).color_scheme == "dark"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["check", "https://shop.example/", "--color-scheme", "auto"])
