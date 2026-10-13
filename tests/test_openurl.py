"""Open URL: a play step that checks, in QAJev's own Chrome, a page the app opened (Twinbox step 21, 14 Oct)."""

import json
import os
import subprocess
import sys
import textwrap

import pytest
import yaml

from qajev import native
from qajev.native import NativeError
from qajev.openurl import check_open_url, open_url_step

SESSION = "cs_test_a1B2c3"
URL = f"https://checkout.stripe.com/c/pay/{SESSION}#fragment"
SPEC = {"from": "new_box.upgrade.checkout_url", "url_regex": r"^https://checkout\.stripe\.com/c/pay/cs_test_",
        "says": "the app opened a Stripe test checkout",
        "expect": {"text": ["Twinbox Hobby: 3,700 credits a month", "$10.00"]},
        "guard": {"allow_requests": [r"^https://api\.stripe\.com/v1/payment_pages/{last}/init$"]}}


class App:
    def __init__(self, url=URL):
        self.url, self.errors, self.inputs = url, [], []

    def observe(self):
        return {"screen": "new box", "texts": [], "actions": [],
                "state": {"new_box": {"upgrade": {"stage": "waiting", "checkout_url": self.url}}}}

    def act(self, action):
        self.inputs.append(action)

    def call(self, **request):
        self.inputs.append(request)
        return {"ok": True}


class InnerRun:
    """A stand-in `qajev run`: records its arguments and suite, and writes the report a real run would."""

    def __init__(self, outcome="pass", checks=None, writes=True):
        self.calls, self.outcome, self.writes = [], outcome, writes
        self.checks = checks if checks is not None else [
            {"check": "page shows 'Twinbox Hobby: 3,700 credits a month'", "ok": True, "detail": None},
            {"check": "page shows '$10.00'", "ok": outcome != "fail", "detail": None if outcome != "fail" else "no"}]

    def __call__(self, args, timeout=600):
        self.calls.append(args)
        out = args[args.index("--out") + 1]
        self.suite = yaml.safe_load(open(args[args.index("run") + 1]).read())
        if self.writes:
            folder = os.path.join(out, "20261014-020000-x")
            os.makedirs(folder)
            with open(os.path.join(folder, "report.json"), "w") as f:
                json.dump({"gate": "PASS", "scenarios": [{"name": "x", "outcome": self.outcome, "reason": "why",
                                                          "checks": self.checks}]}, f)
        return 0, "done"


def test_the_page_the_app_opened_is_checked_in_qajevs_chrome(tmp_path):
    inner, events = InnerRun(), []
    r = open_url_step(App(), name="checkout", spec=SPEC, about="The checkout is a test session", run_dir=tmp_path,
                      emit=events.append, run=inner)
    assert r["outcome"] == "pass"
    assert [c["check"] for c in r["checks"]] == [
        r"state new_box.upgrade.checkout_url matches ^https://checkout\.stripe\.com/c/pay/cs_test_",
        "page: page shows 'Twinbox Hobby: 3,700 credits a month'", "page: page shows '$10.00'"]
    assert r["checks"][0]["says"] == "the app opened a Stripe test checkout"
    args = inner.calls[0]
    assert args[1:4] == ["-m", "qajev", "run"] and "--ephemeral" in args and "--headless" in args
    assert args[args.index("--runner") + 1] == "local"  # beside the app, whatever QAJEV_RUNNER says
    scenario = inner.suite["scenarios"][0]
    assert scenario["url"] == URL and scenario["expect"] == SPEC["expect"] and inner.suite["device"] == "desktop"
    # {last}: this session's own id, escaped, so the guard lets through its /init POST and no other session's
    assert inner.suite["guard"]["allow_requests"] == [rf"^https://api\.stripe\.com/v1/payment_pages/{SESSION}/init$"]
    assert r["opened_url"]["host"] == "checkout.stripe.com"
    assert any(e.get("event") == "step" and "checkout.stripe.com" in e["doing"] for e in events)


@pytest.mark.parametrize("url, shown", [
    (None, "None"), ("https://checkout.stripe.com/c/pay/cs_live_x", "cs_live_x"),
    ("https://evil.example/c/pay/cs_test_x", "evil.example"), (42, "42")])
def test_a_url_that_is_missing_or_does_not_match_opens_nothing_and_fails(tmp_path, url, shown):
    inner = InnerRun()
    r = open_url_step(App(url=url), name="checkout", spec=SPEC, run_dir=tmp_path, run=inner)
    assert r["outcome"] == "fail" and inner.calls == []
    assert r["checks"][0]["ok"] is False and shown in r["checks"][0]["detail"]
    assert "nothing was opened" in r["reason"]


def test_a_page_that_fails_its_checks_fails_the_step(tmp_path):
    r = open_url_step(App(), name="checkout", spec=SPEC, run_dir=tmp_path, run=InnerRun(outcome="fail"))
    assert r["outcome"] == "fail" and r["checks"][-1] == {"check": "page: page shows '$10.00'", "ok": False,
                                                          "detail": "no"}


@pytest.mark.parametrize("outcome", ["harness", "stuck"])
def test_a_page_check_that_could_not_judge_is_harness(tmp_path, outcome):
    r = open_url_step(App(), name="checkout", spec=SPEC, run_dir=tmp_path, run=InnerRun(outcome=outcome, checks=[]))
    assert r["outcome"] == "harness" and f"the page check was {outcome}" in r["reason"]


def test_a_page_check_that_never_ran_is_harness(tmp_path):
    r = open_url_step(App(), name="checkout", spec=SPEC, run_dir=tmp_path, run=InnerRun(writes=False))
    assert r["outcome"] == "harness" and "the page check did not run" in r["reason"]


def test_an_open_url_step_in_a_session_runs_as_its_own_step(tmp_path, monkeypatch):
    from qajev import openurl
    from qajev.ledger import Ledger

    inner = InnerRun()
    monkeypatch.setattr(openurl, "_run", inner)
    steps = [{"name": "checkout", "about": "The app opened a Stripe test checkout.", "open_url": SPEC}]
    results = native.run_session(App(), steps, ledger=Ledger(1.0, 0.0), run_dir=tmp_path, shots=False)
    assert results[0]["outcome"] == "pass" and len(inner.calls) == 1


@pytest.mark.parametrize("spec, says", [
    ("opened_url", "needs from:, url_regex: and expect:"),
    ({**SPEC, "open": True}, "unknown key"),
    ({**SPEC, "from": ""}, "open_url.from"),
    ({**SPEC, "url_regex": "checkout"}, "url_regex: a regular expression from \\^https://"),
    ({**SPEC, "url_regex": "^http://checkout"}, "from \\^https://"),
    ({**SPEC, "url_regex": "^https://(["}, "url_regex: "),
    ({**SPEC, "expect": {}}, "open_url.expect"),
    ({**SPEC, "expect": {"textt": ["x"]}}, "textt"),
    ({**SPEC, "guard": {"allow_everything": True}}, "takes allow_requests only"),
    ({**SPEC, "guard": {"allow_secret_fields": True}}, "payment fields stay disabled"),
    ({**SPEC, "guard": {"allow_requests": ["^https://api\\.stripe\\.com/.*$"]}}, "with {last} in it"),
    ({**SPEC, "guard": {"allow_requests": ["https://api\\.stripe\\.com/{last}"]}}, "from \\^ to \\$"),
])
def test_a_wrong_open_url_step_is_refused_when_the_suite_loads(spec, says):
    with pytest.raises(NativeError, match=says):
        check_open_url([{"name": "checkout", "open_url": spec}])


@pytest.mark.parametrize("acting", ["goal", "play", "js", "pad", "keys", "idle", "relaunch", "handoff"])
def test_an_open_url_step_that_would_also_act_is_refused(acting):
    with pytest.raises(NativeError, match=f"only checks a page; put {acting}"):
        check_open_url([{"name": "checkout", "open_url": SPEC, acting: "x"}])


def test_an_open_url_step_with_its_own_expect_is_refused():
    with pytest.raises(NativeError, match="checks go in open_url.expect"):
        check_open_url([{"name": "checkout", "open_url": SPEC, "expect": {"state": {"a": 1}}}])


def test_the_inner_run_takes_the_browser_slot_from_its_parent_and_no_one_else_does():
    from qajev import jobs

    take = textwrap.dedent("""
        from qajev import jobs
        try:
            with jobs.machine_lock("inner", wait=1):
                print("in")
        except jobs.Busy:
            print("busy")
    """)
    with jobs.machine_lock("the play run"):
        child = subprocess.run([sys.executable, "-c", take], capture_output=True, text=True, timeout=60)
        grandchild = subprocess.run(["/bin/sh", "-c", f'"{sys.executable}" -c \'{take}\'; true'], capture_output=True,
                                    text=True, timeout=60)
    assert child.stdout.strip() == "in", child.stderr  # the play run's own child: no wait, no deadlock
    assert grandchild.stdout.strip() == "busy", grandchild.stderr  # anything else still queues


def test_qajev_play_refuses_a_wrong_open_url_step_when_the_suite_loads(tmp_path, capsys):
    from qajev import cli

    suite = tmp_path / "s.yaml"
    suite.write_text("steps:\n  - name: checkout\n    open_url: {from: opened_url, url_regex: 'http://x', "
                     "expect: {text: [a]}}\n")
    env = tmp_path / ".env"
    env.write_text("")
    assert cli.main(["play", str(tmp_path), "--suite", str(suite), "--json", "--env-file", str(env)]) == cli.EXIT_CONFIG
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "url_regex" in out["error"]


def test_the_page_opens_only_in_qajevs_own_throwaway_chrome(tmp_path, monkeypatch):
    """Proof 1: never José's browser, the system's default browser or `open`: one `qajev run`, throwaway profile."""
    import webbrowser

    def refuse(*a, **k):
        raise AssertionError(f"the page went somewhere else: {a}")

    monkeypatch.setattr(webbrowser, "open", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "system", refuse)
    inner = InnerRun()
    r = open_url_step(App(), name="checkout", spec=SPEC, run_dir=tmp_path, run=inner)
    assert r["outcome"] == "pass" and len(inner.calls) == 1
    args = inner.calls[0]
    assert args[0] == sys.executable and args[1:4] == ["-m", "qajev", "run"]
    assert {"--ephemeral", "--headless"} <= set(args) and args[args.index("--runner") + 1] == "local"
    assert "--profile" not in args and "--cdp-url" not in args


def test_on_a_checkout_page_payment_fields_stay_disabled_and_only_its_own_init_posts(tmp_path):
    """Proof 2: the page's run is read-only with QAJev's guard as is; the one request let through is this session's."""
    inner = InnerRun()
    open_url_step(App(), name="checkout", spec=SPEC, run_dir=tmp_path, run=inner)
    assert inner.suite["guard"] == {"allow_requests": [rf"^https://api\.stripe\.com/v1/payment_pages/{SESSION}/init$"]}
    assert "mode" not in inner.suite and "mode" not in inner.suite["scenarios"][0]  # read-only, as production is
    from qajev.suite import parse

    suite = parse(inner.suite)
    assert not suite.guard.get("allow_secret_fields") and suite.scenarios[0].mode == "readonly"


@pytest.mark.parametrize("url, says", [
    ("http://checkout.stripe.com/c/pay/cs_test_x", "not an https URL"),
    ("https://user:pass@checkout.stripe.com/c/pay/cs_test_x", "user name or password is refused"),
    ("https://checkout.stripe.com.evil.example/c/pay/cs_test_x", "was 'https://checkout.stripe.com.evil"),
])
def test_a_url_that_is_not_https_holds_a_password_or_is_another_host_opens_nothing(tmp_path, url, says):
    """Proof 3, with a url_regex loose enough to let each one match: the step's own rules still refuse them."""
    inner = InnerRun()
    loose = {**SPEC, "url_regex": r"^https?://(.*@)?checkout\.stripe\.com/c/pay/cs_test_"}
    r = open_url_step(App(url=url), name="checkout", spec=loose if "evil" not in url else SPEC, run_dir=tmp_path,
                      run=inner)
    assert r["outcome"] == "fail" and inner.calls == [] and "nothing was opened" in r["reason"]
    assert says in r["checks"][0]["detail"]
