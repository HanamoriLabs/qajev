"""expect.sweep: each setting's hooks, then no new console error, page error or visible banner."""

import http.server
import os
import threading
from urllib.parse import urlsplit

import pytest
from test_ux_dom_live import ENDPOINT
from test_ux_dom_live import no_model_initialization as no_model_initialization
from test_ux_dom_live import page as page

from qajev import plan, runner, session, suite, verdict

SETTINGS = [{"name": "graphics high", "do": [{"click": "#high"}]}, {"name": "language Japanese",
                                                                    "do": [{"click": "#ja"}]}]


def _suite(**extra):
    spec = {"name": "the settings menus", "settings": SETTINGS, **extra}
    return suite.parse({"devices": ["desktop"], "scenarios": [{"name": "settings", "about": "Each setting is clean.",
                        "url": "http://127.0.0.1:8877/", "expect": {"sweep": spec}}]})


def test_a_sweep_has_defaults_and_is_refused_before_the_browser_when_malformed():
    spec = _suite().scenarios[0].expect["sweep"]
    assert spec == {"name": "the settings menus", "settings": SETTINGS, "wait": 1,
                    "banners": ["[role=alert]", "[role=alertdialog]"]}
    assert _suite(wait=0, banners=[]).scenarios[0].expect["sweep"]["banners"] == []
    one = {"name": "a", "do": [{"click": "#a"}]}
    for bad, says in (({"settings": []}, "1 to 40 settings"), ({"settings": [one] * 41}, "1 to 40 settings"),
                      ({"settings": [one, one]}, "used twice"), ({"settings": [{"name": "a", "do": []}]},
                                                                   "at least one hook"),
                      ({"settings": [{"name": "a"}]}, "at least one hook"),
                      ({"settings": [{"name": "", "do": [{"click": "#a"}]}]}, "name must be"),
                      ({"settings": [{"name": "a", "do": [{"tap": 1}]}]}, "do\\[0\\]"),
                      ({"settings": [{**one, "wait": 2}]}, "unknown key"), ({"settings": ["a"]}, "must be"),
                      ({"wait": 31}, "wait"), ({"wait": -1}, "wait"), ({"wait": True}, "wait"),
                      ({"banners": "[role=alert]"}, "banners"), ({"banners": [""]}, "banners"),
                      ({"banners": ["a"] * 21}, "banners"), ({"name": ""}, "name"), ({"errors": "warn"}, "unknown")):
        with pytest.raises(suite.SuiteError, match=says):
            _suite(**bad)


def test_a_multiplayer_test_cannot_sweep():
    with pytest.raises(suite.SuiteError, match=r"expect: unknown key\(s\) \['sweep'\]"):
        suite.parse({"scenarios": [{"name": "a", "url": "http://127.0.0.1/", "about": "x", "clients": ["p1", "p2"],
                                    "steps": [{"client": "p1", "js": "1"}],
                                    "expect": {"sweep": {"name": "n", "settings": SETTINGS}}}]})


def test_each_setting_is_its_own_planned_positive_check():
    parsed = _suite(wait=0.5)
    expect = parsed.scenarios[0].expect
    assert suite.has_checks(expect) and plan.absent_only(parsed.scenarios) == []
    assert verdict.planned(expect) == [
        {"check": "sweep 'the settings menus': 'graphics high' shows no error",
         "says": "the settings menus: 0.5 s after graphics high, the page logged no new error (console.error, an "
                 "exception, a rejection, a worker's error) and shows no banner ([role=alert], [role=alertdialog])"},
        {"check": "sweep 'the settings menus': 'language Japanese' shows no error",
         "says": "the settings menus: 0.5 s after language Japanese, the page logged no new error (console.error, an "
                 "exception, a rejection, a worker's error) and shows no banner ([role=alert], [role=alertdialog])"}]
    assert "no banners are looked for" in verdict.planned(_suite(banners=[]).scenarios[0].expect)[0]["says"]


class Menus:
    """A stand-in tab. The page logs `before` as it loads; the i-th hook makes it log after[i]. Each read drains what
    the page logged since the read before, as the guard does. banners() gives the next list of `banners`."""

    def __init__(self, after=(), banners=(), before=()):
        self.pending, self.after, self.banners_seen = list(before), list(after), list(banners)
        self.calls, self.hooks = [], 0

    def set_device(self, *_): pass
    def arm(self, *_): pass
    def check_host(self, *_): pass
    def navigate(self, url): self.url = url

    def probe(self, *_):
        self.calls.append("read")
        drained, self.pending = self.pending, []
        return {"url": getattr(self, "url", None), "status": 200, "probe": {"errors": drained}}

    def run_hook(self, hook, url):
        self.calls.append(hook)
        if self.hooks < len(self.after):
            self.pending += self.after[self.hooks]
        self.hooks += 1

    def banners(self, selectors):
        self.calls.append(("banners", tuple(selectors)))
        return self.banners_seen.pop(0) if self.banners_seen else []


def _run(monkeypatch, tmp_path, menus, **extra):
    monkeypatch.setattr(runner, "wait_for_quiet", lambda _: (True, 0))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    return runner.run_scenario(menus, _suite(**extra).scenarios[0], opts=runner.Options(shots=False),
                               hosts={"127.0.0.1:8877"}, run_dir=tmp_path)


LOADING = [{"kind": "console", "detail": "logged as the page loads"}]
BANNERS = ("banners", ("[role=alert]", "[role=alertdialog]"))


def test_a_sweep_drains_what_came_before_it_then_reads_each_setting_after_its_wait(monkeypatch):
    menus, slept = Menus(before=LOADING), []
    monkeypatch.setattr(runner.time, "sleep", lambda s: slept.append(s) or menus.calls.append(("sleep", s)))
    scenario = _suite().scenarios[0]
    checks, evidence = runner._sweep_check(menus, scenario, "http://127.0.0.1:8877/", lambda: menus.probe())
    assert menus.calls == ["read", SETTINGS[0]["do"][0], ("sleep", 1), "read", BANNERS,
                           SETTINGS[1]["do"][0], ("sleep", 1), "read", BANNERS]
    assert [(c["ok"], c["detail"]) for c in checks] == [(True, "no new error and no banner")] * 2
    assert evidence == {"name": "the settings menus", "wait": 1, "banners": ["[role=alert]", "[role=alertdialog]"],
                        "settings": [{"name": "graphics high", "errors": [], "error_count": 0, "banners": []},
                                     {"name": "language Japanese", "errors": [], "error_count": 0, "banners": []}]}


def test_a_clean_sweep_passes(monkeypatch, tmp_path):
    result = _run(monkeypatch, tmp_path, Menus(before=LOADING))
    assert result["outcome"] == "pass", result["reason"]
    assert result["sweep"]["name"] == "the settings menus" and len(result["checks"]) == 2


@pytest.mark.parametrize("errors,banners,says", [
    ([{"kind": "console", "detail": "shader compile failed"}], [], "1 new error(s), 0 banner(s): console: shader"),
    ([{"kind": "exception", "detail": "Error: preset missing"}], [], "1 new error(s), 0 banner(s): exception: Error"),
    ([{"kind": "rejection", "detail": "locale 404"}], [], "1 new error(s), 0 banner(s): rejection: locale 404"),
    ([{"kind": "worker", "level": "error", "detail": "decoder died"}], [], "1 new error(s), 0 banner(s): worker: "),
    ([], [{"selector": "[role=alert]", "text": "Could not save"}], "0 new error(s), 1 banner(s): banner [role=alert]: "
                                                                     "'Could not save'"),
])
def test_a_setting_that_logs_an_error_or_shows_a_banner_fails_its_own_check(monkeypatch, tmp_path, errors, banners,
                                                                              says):
    result = _run(monkeypatch, tmp_path, Menus(after=[[], errors], banners=[[], banners]))
    assert result["outcome"] == "fail", result["reason"]
    first, second = result["checks"]
    assert first["ok"] is True and second["ok"] is False
    assert second["check"] == "sweep 'the settings menus': 'language Japanese' shows no error"
    assert second["detail"].startswith(says), second["detail"]


@pytest.mark.parametrize("errors", [
    [{"kind": "worker", "level": "warn", "detail": "slow decode"}],
    [{"kind": "http", "status": 404, "detail": "http://127.0.0.1:8877/x.png"}],
    [{"kind": "resource", "detail": "http://127.0.0.1:8877/x.png"}],
    [{"kind": "console", "detail": "Blocked by QAJev read-only mode: POST /save"}],
])
def test_warnings_failed_requests_and_qajevs_own_block_are_not_a_settings_error(monkeypatch, tmp_path, errors):
    result = _run(monkeypatch, tmp_path, Menus(after=[errors]))
    assert result["outcome"] == "pass", result["reason"]


def test_many_errors_are_named_three_at_a_time_and_kept_ten_at_a_time(monkeypatch, tmp_path):
    many = [{"kind": "console", "detail": f"error {i}"} for i in range(12)]
    result = _run(monkeypatch, tmp_path, Menus(after=[many]))
    detail = result["checks"][0]["detail"]
    assert detail == "12 new error(s), 0 banner(s): console: error 0; console: error 1; console: error 2; and 9 more"
    assert result["sweep"]["settings"][0]["error_count"] == 12 and len(result["sweep"]["settings"][0]["errors"]) == 10


def test_a_banner_selector_that_is_not_css_is_harness(monkeypatch, tmp_path):
    broken = [{"selector": "[role=", "error": "'[role=' is not a valid selector"}]
    result = _run(monkeypatch, tmp_path, Menus(after=[[{"kind": "console", "detail": "x"}]], banners=[broken, broken]),
                  banners=["[role="])
    assert result["outcome"] == "harness", result["reason"]
    assert "the check failed to run: banner selector '[role=': '[role=' is not a valid selector" in result["reason"]


def test_a_setting_whose_hook_fails_stops_the_test_as_a_failed_hook(monkeypatch, tmp_path):
    menus = Menus()

    def broken(hook, url):
        raise session.HookFailed("no element matches '#high'")

    menus.run_hook = broken
    result = _run(monkeypatch, tmp_path, menus)
    assert result["stop"] == "hook_failed" and result["outcome"] == "harness"
    assert "sweep 'the settings menus', setting 'graphics high': no element matches '#high'" in result["reason"]


def test_a_sweep_with_a_pad_setting_sets_up_the_pad_before_the_page_loads(monkeypatch, tmp_path):
    menus = Menus()
    menus.use_pad = lambda: menus.calls.append("pad")
    result = _run(monkeypatch, tmp_path, menus, settings=[{"name": "controller", "do": [{"pad": "A"}]}])
    assert result["outcome"] == "pass", result["reason"]
    assert menus.calls[0] == "pad"  # before the first read of the page


def test_without_banners_no_banner_is_read(monkeypatch, tmp_path):
    menus = Menus(banners=[[{"selector": "x", "text": "never asked"}]])
    result = _run(monkeypatch, tmp_path, menus, banners=[])
    assert result["outcome"] == "pass" and not any(isinstance(c, tuple) for c in menus.calls)


# A settings menu. Each button is one setting, and each but #clean has a planted fault: #logs logs an error, #throws
# throws in a timer, #rejects leaves a rejection unhandled, #banner shows an alert. The page also logs an error as it
# loads (before the sweep), keeps an empty live region (role=alert, no text) and a hidden alert with text.
MENU = """<!doctype html><link rel="icon" href="data:,">
<div role="alert" id="live"></div><div role="alert" hidden>an old error, hidden</div>
<div role="alert" id="banner" hidden></div>
<button id="clean">Clean</button><button id="logs">Logs</button><button id="throws">Throws</button>
<button id="rejects">Rejects</button><button id="banner-on">Banner</button>
<script>
const on = (id, f) => document.getElementById(id).addEventListener('click', f);
on('clean', () => { document.body.dataset.mode = 'clean'; });
on('logs', () => console.error('shader compile failed: high'));
on('throws', () => setTimeout(() => { throw new Error('quality preset missing'); }));
on('rejects', () => { Promise.reject(new Error('locale file 404')); });
on('banner-on', () => {
  const b = document.getElementById('banner'); b.hidden = false; b.textContent = 'Could not save';
});
console.error('logged as the page loads');
</script>"""


@pytest.fixture
def menu():
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            body = MENU.encode()
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


@pytest.mark.skipif(not ENDPOINT or os.uname().sysname != "Linux", reason="requires approved CPU fixture browser")
def test_browser_each_planted_fault_fails_its_own_setting_and_the_clean_one_passes(page, menu, monkeypatch):
    tab, _ = page
    tab.hosts = {urlsplit(menu).netloc}
    monkeypatch.setattr(session, "require_transport", lambda: None)
    tab.arm("readonly")
    tab.navigate(menu + "/")
    settings = [{"name": name, "do": [{"click": f"#{name}"}]} for name in ("clean", "logs", "throws", "rejects",
                                                                         "banner-on")]
    scenario = suite.parse({"devices": ["desktop"], "scenarios": [{"name": "menu", "about": "x", "url": menu,
                            "expect": {"sweep": {"name": "menus", "settings": settings, "wait": 0.5}}}]}).scenarios[0]
    checks, evidence = runner._sweep_check(tab, scenario, menu, lambda: tab.probe(scenario.expect))
    got = {setting["name"]: (check["ok"], check["detail"]) for setting, check in zip(settings, checks, strict=True)}
    assert got["clean"] == (True, "no new error and no banner"), got  # the load's error and the empty alerts: none
    assert got["logs"][0] is False and "console: shader compile failed: high" in got["logs"][1], got
    assert got["throws"][0] is False and "exception: " in got["throws"][1] and "quality preset missing" in \
        got["throws"][1], got
    assert got["rejects"][0] is False and "rejection: " in got["rejects"][1] and "locale file 404" in \
        got["rejects"][1], got
    assert got["banner-on"] == (False, "0 new error(s), 1 banner(s): banner [role=alert]: 'Could not save'"), got
    assert [s["error_count"] for s in evidence["settings"]] == [0, 1, 1, 1, 0]


@pytest.mark.skipif(not ENDPOINT or os.uname().sysname != "Linux", reason="requires approved CPU fixture browser")
def test_browser_a_banner_selector_that_is_not_css_comes_back_as_its_error(page, menu, monkeypatch):
    tab, _ = page
    tab.hosts = {urlsplit(menu).netloc}
    monkeypatch.setattr(session, "require_transport", lambda: None)
    tab.navigate(menu + "/")
    [broken] = tab.banners(["[role="])
    assert broken["selector"] == "[role=" and "not a valid selector" in broken["error"], broken
