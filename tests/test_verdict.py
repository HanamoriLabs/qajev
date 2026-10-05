import json
import shutil
import subprocess

import pytest

from qajev import verdict as V

EXPECT = {"url": "/pricing", "text": ["Pro", "Team"], "absent": ["Error"], "js": "true"}


def observed(url="http://h/pricing", text=(True, True), absent=(False,), js=True):
    return {"url": url, "text": list(text), "absent": list(absent), "js": js, "says": "page text"}


def test_page_checks_pass_when_everything_holds():
    checks = V.page_checks(EXPECT, observed())
    assert len(checks) == 5 and V.all_ok(checks)


def test_page_checks_name_what_failed():
    checks = V.page_checks(EXPECT, observed(url="http://h/", text=(True, False), absent=(True,), js="error: x"))
    failed = [c["check"] for c in checks if not c["ok"]]
    assert failed == ["url contains '/pricing'", "page shows 'Team'", "page lacks 'Error'", "js 'true'"]


def test_js_must_be_exactly_true():
    assert not V.all_ok(V.page_checks({"js": "x"}, {"js": "error: boom"}))
    assert not V.all_ok(V.page_checks({"js": "x"}, {"js": False}))


@pytest.mark.skipif(not shutil.which("node"), reason="needs node to run the page-side JavaScript")
def test_a_js_condition_is_judged_by_what_its_promise_settles_to():
    # SideGame1: `!!(promise)` was true while the promise was pending, so a wait_for (and a sign-in's signed_in.js)
    # held on a broken page. The page-side source is run as the browser would run it.
    from qajev.session import awaited

    def settle(js, timeout_ms=300):
        script = f"({awaited(js, timeout_ms)}).then((r) => console.log(JSON.stringify(r)))"
        return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30).stdout)

    assert settle("Promise.resolve(false)") == {"value": False}
    assert settle("Promise.resolve(true)") == {"value": True}
    assert settle("(() => { throw new Error('cooldownLeft of an invalid jutsu') })()") == {
        "error": "cooldownLeft of an invalid jutsu"}
    assert settle("Promise.reject(new Error('nope'))") == {"error": "nope"}
    assert settle("new Promise(() => {})") == {"error": "no answer within 300 ms"}
    assert settle("undefined") == {"value": None}


def test_url_regex():
    assert V.all_ok(V.page_checks({"url_regex": r"/items/\d+$"}, {"url": "http://h/items/42"}))
    assert not V.all_ok(V.page_checks({"url_regex": r"/items/\d+$"}, {"url": "http://h/items/new"}))


OK = [{"check": "a", "ok": True}]
BAD = [{"check": "page shows 'Pro'", "ok": False, "detail": None}]


@pytest.mark.parametrize("stop, checks, has, outcome", [
    ("done", OK, True, "pass"),
    ("blocked", OK, True, "pass"),              # the page decides, not Jev's BLOCKED
    ("budget_actions", OK, True, "pass"),       # ...nor a harness stop
    ("done", BAD, True, "fail"),                # Jev thought it was done; the product disagrees
    ("reached", BAD, True, "fail"),             # page fine but a side effect is wrong
    ("checked", BAD, True, "fail"),
    ("blocked", BAD, True, "stuck"),
    ("budget_actions", BAD, True, "harness"),
    ("stale", BAD, True, "harness"),
    ("model_error", BAD, True, "harness"),
    ("guard_missing", BAD, True, "harness"),
    ("done", [], False, "unverified"),
    ("cost_cap", [], False, "harness"),
    ("unreachable", [], True, "fail"),
    ("skipped", [], True, "skipped"),
])
def test_classification(stop, checks, has, outcome):
    assert V.classify(stop, checks, has_checks=has)[0] == outcome


def test_fail_reason_quotes_the_failed_check():
    outcome, reason = V.classify("done", BAD, has_checks=True)
    assert reason == "Jev reported DONE but: page shows 'Pro'"


@pytest.mark.parametrize("outcomes, strict, gate", [
    (["pass", "pass"], False, "PASS"),
    (["pass", "fail"], False, "FAIL"),
    (["pass", "stuck"], False, "INCOMPLETE"),
    (["pass", "stuck"], True, "FAIL"),
    (["pass", "harness"], False, "INCOMPLETE"),
    (["unverified"], False, "INCOMPLETE"),
    ([], False, "INCOMPLETE"),
])
def test_gate(outcomes, strict, gate):
    assert V.gate(outcomes, strict=strict) == gate


def test_fetch_and_command_checks():
    assert V.fetch_check({"url": "/api", "status": 200}, {"status": 200})["ok"]
    assert not V.fetch_check({"url": "/api", "status": 200}, {"status": 500})["ok"]
    assert not V.fetch_check({"url": "/api", "contains": "ok"}, {"status": 200, "body": "nope"})["ok"]
    assert V.command_check({"run": "x", "stdout": "1"}, {"exit": 0, "stdout": "1\n"})["ok"]
    assert not V.command_check({"run": "x"}, {"exit": 2, "stderr": "boom"})["ok"]


def test_findings_skip_third_party_noise_and_our_own_write_blocks():
    probe = {"errors": [
        {"kind": "exception", "detail": "TypeError: x is undefined"},
        {"kind": "rejection", "detail": "TypeError: QAJev read-only: blocked POST"},
        {"kind": "http", "status": 500, "detail": "http://h/api/data"},
        {"kind": "http", "status": 404, "detail": "https://cdn.other.com/pixel.gif"},
        {"kind": "resource", "detail": "http://h/missing.png"},
        {"kind": "console", "detail": "warning-ish"},
    ], "blank_ms": 12000}
    found = V.findings_from_probe(probe, scenario="s", url="http://h/", first_party_hosts={"h"})
    kinds = [(f["severity"], f["kind"]) for f in found]
    assert kinds == [("S2", "page error"), ("S2", "HTTP 500"), ("S3", "failed to load"), ("S3", "console error")]


def test_csp_violations_are_findings_even_for_third_party_hosts():
    probe = {"errors": [
        {"kind": "csp", "disposition": "enforce", "directive": "script-src-elem",
         "detail": "https://analytics.tiktok.com/i18n/pixel/events.js"},
        {"kind": "csp", "disposition": "report", "directive": "connect-src", "detail": "https://t.co/1/i/adsct"},
    ]}
    found = V.findings_from_probe(probe, scenario="s", url="https://h/", first_party_hosts={"h"})
    assert [(f["severity"], f["kind"], f["detail"]) for f in found] == [
        ("S2", "blocked by CSP", "script-src-elem: https://analytics.tiktok.com/i18n/pixel/events.js"),
        ("S3", "CSP violation (report-only)", "connect-src: https://t.co/1/i/adsct"),
    ]


def test_a_blank_spell_is_one_finding_with_its_longest_duration():
    assert V.blank_finding(9_999, scenario="s", url="u") == []
    (f,) = V.blank_finding(15_037, scenario="s", url="u")
    assert (f["severity"], f["kind"], f["detail"]) == ("S2", "blank or spinner over 10 s", "longest 15.0 s")


def test_visible_checks_are_named_and_explain_a_miss():
    ok, miss = V.page_checks({"visible": ["€17.50", "FAQ"]}, {"visible": [True, False]})
    assert ok == {"check": "on screen: '€17.50'", "ok": True, "detail": None}
    assert not miss["ok"] and "not visible in the viewport" in miss["detail"]


def test_screens_report_the_runner_up_label_from_the_request():
    decision = {
        "operation": "CLICK", "target": "2", "confidence": 0.9,
        "target_probabilities": {"1": 0.3, "2": 0.6, "3": 0.1},
        "request": {"questions": {"click_target": {"criteria": {
            "1": {"element": "[1] Overview"}, "2": {"element": "[2] Pricing"}, "3": {"element": "[3] Docs"}}}}},
    }
    done = {"operation": "DONE", "target": None, "operation_probabilities": {"DONE": 0.95, "WAIT": 0.05}}
    s1, s2 = V.screens([{**decision, "latency_ms": 280}, done])
    assert (s1["next_step"], s1["p"], s1["runner_up"], s1["runner_up_p"]) == ("[2] Pricing", 0.6, "[1] Overview", 0.3)
    assert (s2["next_step"], s2["p"], s2["runner_up"]) == ("DONE", 0.95, "WAIT")
    assert (s1["options"], s1["ms"], s2["options"], s2["ms"]) == (3, 280, 2, None)  # qajev top's decisions view


def test_ignore_case_is_named_in_the_check():
    (check,) = V.page_checks({"text": ["Solo"], "ignore_case": True}, {"text": [True]})
    assert check == {"check": "page shows 'Solo' (any case)", "ok": True, "detail": None}


def test_an_expected_status_is_a_check_and_not_a_finding():
    # fixture "the old page is gone": the test showed the 404 page it set out to show, and still got an S2 "HTTP 404"
    assert V.page_checks({"status": 404}, {"status": 404}) == [
        {"check": "the page answered HTTP 404", "ok": True, "detail": None}]
    assert V.page_checks({"status": 404}, {"status": 200}) == [
        {"check": "the page answered HTTP 404", "ok": False, "detail": "HTTP 200"}]
    assert V.document_findings(404, {"status": 404}, scenario="gone", url="http://h/old") == []
    [f] = V.document_findings(404, {}, scenario="gone", url="http://h/old")
    assert (f["severity"], f["kind"]) == ("S2", "HTTP 404")
    assert V.document_findings(503, {"status": 404}, scenario="gone", url="http://h/old")[0]["severity"] == "S1"
    assert V.document_findings(200, {}, scenario="ok", url="http://h/") == []


def test_a_missing_text_shows_the_closest_text_on_the_page():
    # I'M HIM! HUD corner: "page shows 'overlapFrames=0'" failed with the first 400 characters of the page as its
    # detail; the line that mattered ("REPRO ... overlapFrames=5 ...") was further down.
    seen = {"text": [False, True], "near": ["REPRO 1920x1080 overlapFrames=5 maxArea=812", None], "says": "E TALK"}
    miss, hit = V.page_checks({"text": ["overlapFrames=0", "REPRO"]}, seen)
    assert miss == {"check": "page shows 'overlapFrames=0'", "ok": False,
                    "detail": "closest on the page: REPRO 1920x1080 overlapFrames=5 maxArea=812"}
    assert hit["ok"] and hit["detail"] is None
    (nothing,) = V.page_checks({"text": ["Zebra"]}, {"text": [False], "near": [None], "says": "E TALK"})
    assert nothing["detail"] == "not on the page; it begins: E TALK"


def test_a_stuck_or_harness_result_names_what_the_guard_held_back():
    # verse1, 4 Oct: "stuck" read as a layout bug; the guard had hidden the "Shop" button over its tooltip's "buy".
    held = [{"label": "Shop Show clothes and gear you can buy", "why": "danger", "match": "buy"},
            {"label": "Card number", "why": "secret field", "match": None},
            {"label": "Elsewhere", "why": "off-site link", "match": "elsewhere.example"},
            {"label": "Delete account", "why": "danger", "match": "Delete account"}]
    stuck = V.with_guard_note("stuck", "Jev found no way forward", held)
    assert stuck == ("Jev found no way forward; guard hid: 'Shop Show clothes and gear you can buy' (danger: buy), "
                     "'Card number' (secret field), 'Elsewhere' (off-site link: elsewhere.example) and 1 more")
    harness = V.with_guard_note("harness", "action budget spent", held[:1])
    assert harness == "action budget spent; guard hid: 'Shop Show clothes and gear you can buy' (danger: buy)"
    assert V.with_guard_note("pass", "ok", held) == "ok" and V.with_guard_note("fail", "no", held) == "no"
    assert V.with_guard_note("stuck", "Jev found no way forward", []) == "Jev found no way forward"


def test_jev_never_setting_off_for_the_page_the_checks_need_is_harness_not_a_product_failure():
    # FlockTab's nightly, 5 Oct: "enterprise", "leaderboard", "changelog" and phone "pricing" scrolled the home page
    # 14-15 times and said DONE there (on the phone the menu was never opened); live, the pages are fine.
    scrolls = [{"kind": "scroll"}] * 15
    reason = V.never_set_off("https://flocktab.com/", "https://flocktab.com/#top", {"url": "/enterprise"}, scrolls)
    assert reason.startswith("Jev never left the start page: it scrolled 15 time(s) and clicked nothing, so it never "
                             "reached /enterprise. This says nothing about the product")
    assert V.never_set_off("https://flocktab.com", "https://flocktab.com/",
                           {"url_regex": "^https://console\\.flocktab\\.com/login"}, []).endswith(
        'a goal that names the way there ("open the menu, then ...") helps')
    # a product failure stays one
    clicked = scrolls + [{"kind": "click"}]  # Jev did look for a way (a missing link, a dead menu): not harness
    assert V.never_set_off("https://flocktab.com/", "https://flocktab.com/", {"url": "/enterprise"}, clicked) is None
    assert V.never_set_off("https://flocktab.com/", "https://flocktab.com/pricing", {"url": "/enterprise"},
                           scrolls) is None  # it left the start page
    assert V.never_set_off("https://flocktab.com/pricing", "https://flocktab.com/pricing", {"url": "/pricing"},
                           scrolls) is None  # it began on the page the checks want
    assert V.never_set_off("https://flocktab.com/", "https://flocktab.com/", {"text": ["Team"]}, scrolls) is None
    # a site whose navigation is truly gone also only gets scrolled, and grades harness: never a PASS
    assert V.gate(["pass", "harness"]) == "INCOMPLETE" and V.gate(["harness"], strict=True) == "INCOMPLETE"
    assert V.gate(["harness", "fail"]) == "FAIL" and V.EXIT_CODES["INCOMPLETE"] != 0


@pytest.mark.parametrize("stop, not_run, outcome", [
    ("browser_error", (), "harness"),       # the run broke: what did run cannot make it a pass
    ("hook_failed", (), "harness"),
    ("guard_missing", (), "harness"),
    ("budget_actions", (), "pass"),         # Jev wandered, but the page was judged in full
    ("budget_actions", ("expect.fetch",), "harness"),  # ...unless a check never ran
    ("checked", ("step 5 (js)",), "harness"),
])
def test_a_run_that_stopped_before_its_checks_ran_is_never_a_pass(stop, not_run, outcome):
    got, reason = V.classify(stop, OK, has_checks=True, stop_detail="step 4 (snapshot walking): RuntimeError: x",
                             not_run=not_run)
    assert got == outcome
    if outcome == "harness" and stop != "checked":
        assert "step 4 (snapshot walking): RuntimeError: x" in reason
    if not_run:
        assert reason.endswith(f"not run: {', '.join(not_run)}")
    assert V.gate([got]) == ("PASS" if outcome == "pass" else "INCOMPLETE")


def test_an_after_hook_that_fails_leaves_the_scenario_incomplete_and_names_what_never_ran(monkeypatch):
    from qajev import runner, suite
    from qajev.session import HookFailed

    class Page:
        def set_device(self, device): pass
        def arm(self, mode, speech=None): pass
        def check_host(self, url): pass
        def navigate(self, url): self.url = url
        def probe(self, expect): return {"url": self.url, "status": 200, "text": [True], "probe": {}}
        def why_failed(self, url): return None
        def fetch(self, probe): raise AssertionError("fetch must not run after a failed hook")

        def run_hook(self, hook, url):
            raise HookFailed("js 'cart.empty()' returned false")

    monkeypatch.setattr(runner, "wait_for_quiet", lambda opts: (True, 1.0))
    s = suite.parse({"scenarios": [{"name": "cart", "url": "http://127.0.0.1:8765/cart",
                                    "after": [{"js": "cart.empty()"}],
                                    "expect": {"text": "Cart", "fetch": [{"url": "/api/cart", "status": 200}]}}]})
    result = runner.run_scenario(Page(), s.scenarios[0], opts=runner.Options(), hosts={"127.0.0.1"}, run_dir=None)
    assert result["outcome"] == "harness", result["reason"]
    assert "hook failed: js 'cart.empty()' returned false" in result["reason"]
    assert result["reason"].endswith("not run: expect.fetch")
