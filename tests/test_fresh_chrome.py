"""Each test in its own fresh Chrome (browser: fresh, the default). #802 (30 Oct): one shared Chrome cost 5-6.5 min a
step, as one test's open form or dialog held up the next; one process per test with a 150 s cap passed 21 of 21.
The chains' child processes are threads here: what the parent does with them is the contract."""

import queue
import threading
import time

import pytest

from qajev import chrome, config, report_html, runner, suite
from qajev.ledger import Ledger


def parse(scenarios, **extra):
    return suite.parse({"name": "fresh", "base_url": "http://127.0.0.1:8000", "devices": ["desktop"], **extra,
                        "scenarios": [{"about": "fixture", "expect": {"text": ["Hi"]}, **s} for s in scenarios]})


class Child:
    """A chain's process, as run_fresh sees one."""

    def __init__(self):
        self.ended = threading.Event()  # terminate() or kill()
        self.exitcode = None
        self.thread = None

    def is_alive(self):
        return self.thread.is_alive() and not self.ended.is_set()

    def join(self, timeout=None):
        self.thread.join(0.5 if self.ended.is_set() else timeout)

    def terminate(self):
        self.ended.set()
        self.exitcode = -15

    kill = terminate


class Late(queue.Queue):
    """A pipe whose last words are still on their way when the process ends: a quick look finds nothing."""

    def get_nowait(self):
        raise queue.Empty


@pytest.fixture
def fake(monkeypatch):
    """Chains run as threads. Each passes its tests, unless a test is named in: hang (hangs after it starts), crash
    (the process ends with no word), stall (Chrome is up, no test starts), wait (a quiet-machine wait of wait_s, then
    it passes), wait_hang (that wait, then it hangs), secret (the chain read a secret and a result shows it). A chain
    whose first test is in `late` says everything on a Late queue."""
    state = {"chains": [], "children": [], "hang": set(), "crash": set(), "stall": set(), "wait": set(),
             "wait_hang": set(), "secret": {}, "late": set(), "wait_s": 1.5, "chrome_s": 0.0, "signin_s": 0.0,
             "stopped": [], "started": [], "groups": []}

    def start_chain(_ctx, payload):
        chain = payload[1]
        messages = Late() if chain[0].name in state["late"] else queue.Queue()
        child = Child()
        state["chains"].append([s.name for s in chain])
        state["children"].append(child)
        key = f"chrome-{len(state['chains'])}"

        def script():
            time.sleep(state["chrome_s"])  # Chrome starting
            messages.put(("chrome", key))
            if state["signin_s"]:  # the account signing in, in two halves
                messages.put(("event", {"event": "signin", "account": "tester"}))
                time.sleep(state["signin_s"])
                messages.put(("event", {"event": "signin", "account": "tester", "ok": True}))
                time.sleep(state["signin_s"])
            results = []
            for s in chain:
                if s.name in state["crash"]:
                    child.exitcode = 1
                    return
                if s.name in state["stall"]:
                    child.ended.wait()
                    return
                messages.put(("event", {"event": "start", "scenario": s.name}))
                if s.name in state["wait"] | state["wait_hang"]:
                    messages.put(("event", {"event": "waiting", "reason": "load1 200 >= 150"}))
                    time.sleep(state["wait_s"])
                    messages.put(("event", {"event": "waited", "seconds": state["wait_s"], "quiet": True}))
                if s.name in state["hang"] | state["wait_hang"]:
                    child.ended.wait()
                    return
                result = runner._not_run(s, "pass", "done", "fixture")
                if s.name in state["secret"]:
                    messages.put(("secrets", [state["secret"][s.name]]))
                    result["reason"] = f"the page showed {state['secret'][s.name]}"
                results.append(result)
                messages.put(("event", {"event": "scenario", "result": result}))
            messages.put(("done", {"results": results, "ledger": Ledger(0.0).summary()}))
            child.exitcode = 0

        child.thread = threading.Thread(target=script, daemon=True)
        child.thread.start()
        return child, messages

    def run_group(scenarios, **kw):  # the shared way: every chain in this one Chrome
        state["groups"].append([s.name for s in scenarios])
        return {"results": [runner._not_run(s, "pass", "done", "fixture") for s in scenarios],
                "ledger": kw["ledger"].summary()}

    def start(profile, **kw):
        state["started"].append(kw)
        return {"cdp_url": "http://127.0.0.1:9", "state_key": "shared", "ephemeral": kw.get("ephemeral")}

    monkeypatch.setattr(runner, "_start_chain", start_chain)
    monkeypatch.setattr(runner, "run_group", run_group)
    monkeypatch.setattr(chrome, "start", start)
    monkeypatch.setattr(chrome, "stop", lambda key, group=False: state["stopped"].append((key, group)))
    monkeypatch.setattr(chrome, "reap", lambda: [])
    monkeypatch.setattr(chrome, "status", lambda profile=None: [])
    monkeypatch.setattr(chrome, "run_record", lambda owned: {"managed": True, "cdp_url": owned["cdp_url"]})
    monkeypatch.setattr(config, "_REMEMBERED", set())
    monkeypatch.delenv("QAJEV_FRESH_START_WAIT", raising=False)
    return state


def run(spec, tmp_path, **opts):
    return runner.run(spec, runner.Options(out_dir=tmp_path, headless=True, load_high=None, **opts))


def by_name(built):
    return {r["name"]: r for r in built["scenarios"]}


CHAINED = [{"name": "home", "url": "/"}, {"name": "pricing", "url": "/pricing"},
           {"name": "sign in", "url": "/login"}, {"name": "cart", "url": "/cart", "depends_on": ["sign in"]}]
CAPPED = [{"name": "slow", "url": "/slow", "budget": {"seconds": 0.5}},
          {"name": "after slow", "url": "/next", "depends_on": ["slow"]},
          {"name": "quick", "url": "/"}]


def test_a_suite_runs_each_test_in_its_own_chrome_and_a_chain_in_one(fake, tmp_path):
    built = run(parse(CHAINED), tmp_path)
    assert sorted(fake["chains"]) == [["home"], ["pricing"], ["sign in", "cart"]]
    assert fake["groups"] == [] and fake["started"] == []  # no Chrome of the parent's own
    assert {n: r["outcome"] for n, r in by_name(built).items()} == dict.fromkeys(
        ["home", "pricing", "sign in", "cart"], "pass")
    assert built["browser"]["fresh"] is True


def test_browser_shared_keeps_one_chrome_for_the_whole_suite(fake, tmp_path):
    built = run(parse(CHAINED, browser="shared"), tmp_path)
    assert fake["chains"] == []
    assert len(fake["started"]) == 1 and fake["groups"] == [["home", "pricing", "sign in", "cart"]]
    assert "fresh" not in built["browser"] and "shared_because" not in built["browser"]  # it asked for one


def test_a_bad_browser_value_is_refused():
    with pytest.raises(suite.SuiteError, match="browser"):
        parse(CHAINED, browser="new")
    assert parse(CHAINED).browser == "fresh"


def test_a_test_past_its_cap_is_stopped_with_its_chrome_and_graded_harness(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "CAP_MARGIN", 0.5)
    fake["hang"] = {"slow"}
    got = by_name(built := run(parse(CAPPED), tmp_path))
    assert (got["slow"]["outcome"], got["slow"]["stop"], got["slow"]["reason"]) == ("harness", "cap", "cap 1 s hit")
    assert got["after slow"]["outcome"] == "skipped" and "'slow'" in got["after slow"]["reason"]
    assert got["quick"]["outcome"] == "pass"  # the other tests run on
    hung = fake["chains"].index(["slow", "after slow"])
    assert fake["children"][hung].ended.is_set()
    assert (f"chrome-{hung + 1}", True) in fake["stopped"]  # its whole process group, by its state record
    assert built["gate"] == "INCOMPLETE"


def test_a_wait_for_a_quiet_machine_does_not_count_toward_the_cap(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "CAP_MARGIN", 0.5)
    fake["wait"] = {"busy"}
    got = by_name(run(parse([{"name": "busy", "url": "/", "budget": {"seconds": 0.5}}]), tmp_path))
    assert got["busy"]["outcome"] == "pass", got["busy"]


def test_a_wait_does_not_loosen_the_cap_after_it(fake, monkeypatch, tmp_path):
    # The clock stops for the wait only: the test still has its own 1 s after it, not the run's load budget.
    monkeypatch.setattr(runner, "CAP_MARGIN", 0.5)
    monkeypatch.setenv("QAJEV_FRESH_START_WAIT", "0.2")
    fake["wait_hang"], fake["wait_s"] = {"busy"}, 0.3
    started = time.monotonic()
    got = by_name(run(parse([{"name": "busy", "url": "/", "budget": {"seconds": 0.5}}]), tmp_path, load_wait=2.0))
    assert (got["busy"]["outcome"], got["busy"]["reason"]) == ("harness", "cap 1 s hit")
    assert time.monotonic() - started < 2.1  # 0.3 s of wait + the 1 s cap; a clock never restarted ends past 2.2 s


def test_a_chain_whose_process_ends_without_a_word_costs_its_own_tests_only(fake, tmp_path):
    fake["crash"] = {"pricing"}
    got = by_name(run(parse(CHAINED), tmp_path))
    assert got["pricing"]["outcome"] == "harness" and "exit 1" in got["pricing"]["reason"]
    assert got["home"]["outcome"] == got["cart"]["outcome"] == "pass"
    crashed = fake["chains"].index(["pricing"])
    assert (f"chrome-{crashed + 1}", True) in fake["stopped"]  # what it started is stopped, as at a cap


def test_a_chain_that_never_reaches_its_next_test_is_stopped_at_the_start_wait(fake, monkeypatch, tmp_path):
    monkeypatch.setenv("QAJEV_FRESH_START_WAIT", "0.5")
    fake["stall"] = {"pricing"}
    got = by_name(run(parse(CHAINED), tmp_path))
    assert got["pricing"]["outcome"] == "harness" and "QAJEV_FRESH_START_WAIT" in got["pricing"]["reason"]
    assert got["home"]["outcome"] == "pass"


def test_each_step_before_a_test_gets_its_own_start_wait(fake, monkeypatch, tmp_path):
    # Chrome's own start has QAJEV_CHROME_START_WAIT; then the sign-in's start and end each give the next step
    # QAJEV_FRESH_START_WAIT again. All of it together is longer than one start wait, and nothing is stopped.
    monkeypatch.setenv("QAJEV_FRESH_START_WAIT", "1")
    monkeypatch.setenv("QAJEV_CHROME_START_WAIT", "3")
    fake["chrome_s"], fake["signin_s"] = 1.5, 0.8  # the test starts 3.1 s in: past 1 s, and past each wait alone
    got = by_name(run(parse([{"name": "home", "url": "/"}]), tmp_path))
    assert got["home"]["outcome"] == "pass", got["home"]


def test_the_last_words_of_a_chain_that_has_ended_still_count(fake, tmp_path):
    # Its process can end before its "done" leaves the pipe: the parent looks once more, and waits for it.
    fake["late"] = {"pricing"}
    got = by_name(run(parse(CHAINED), tmp_path))
    assert got["pricing"]["outcome"] == "pass", got["pricing"]


def test_the_start_wait_is_a_setting_of_60_s_by_default(monkeypatch):
    monkeypatch.delenv("QAJEV_FRESH_START_WAIT", raising=False)
    assert runner.start_wait() == 60.0
    monkeypatch.setenv("QAJEV_FRESH_START_WAIT", "120")
    assert runner.start_wait() == 120.0
    for bad in ("soon", "0", "-5"):
        monkeypatch.setenv("QAJEV_FRESH_START_WAIT", bad)
        with pytest.raises(runner.ConfigError, match="QAJEV_FRESH_START_WAIT"):
            runner.start_wait()


def test_the_report_is_redacted_of_what_a_chain_read_for_its_sign_in(fake, tmp_path):
    # The child read the secret; the parent never did. It must still not reach report.json, .md or .html.
    fake["secret"] = {"home": "vault-only-value-7731"}
    built = run(parse(CHAINED), tmp_path)
    assert "[redacted]" in by_name(built)["home"]["reason"]
    written = [f.read_text(errors="replace") for f in tmp_path.rglob("*") if f.is_file()]
    assert written and not any("vault-only-value-7731" in text for text in written)


@pytest.mark.parametrize(("change", "why"), [
    ({"cdp_url": "http://127.0.0.1:9"}, "--cdp-url"),
    ({"jobs": 2}, "--jobs 2 with the kept profile"),
])
def test_a_run_that_cannot_have_fresh_chromes_shares_one_and_says_why(fake, change, why):
    spec = parse(CHAINED)
    mode, because = runner.browser_plan(spec, runner.Options(**change))
    assert mode == "shared" and why in because
    assert runner.browser_plan(spec, runner.Options(jobs=2, ephemeral=True)) == ("fresh", None)


def test_an_open_profile_chrome_is_shared_not_closed(fake, monkeypatch):
    monkeypatch.setattr(chrome, "status", lambda profile=None: [{"alive": True, "ephemeral": False}])
    mode, because = runner.browser_plan(parse(CHAINED), runner.Options())
    assert mode == "shared" and "already open" in because
    assert runner.browser_plan(parse(CHAINED), runner.Options(ephemeral=True)) == ("fresh", None)


def test_the_report_says_which_chrome_ran_the_tests():
    assert report_html.browser_where({"managed": True, "fresh": True, "ephemeral": True, "headless": True}) == (
        "a fresh Chrome for each test (QAJev throwaway profile (deleted after the run), headless)")
    shared = report_html.browser_where({"managed": False, "cdp_url": "http://127.0.0.1:9",
                                        "shared_because": "the run uses a Chrome QAJev did not start (--cdp-url)"})
    assert shared == ("http://127.0.0.1:9 (attached); one Chrome for the suite: the run uses a Chrome QAJev did not "
                      "start (--cdp-url)")


# The chain itself (_fresh_chain), in this thread: what it sends.

SECRETS = dict(zip(("password", "email", "code", "cookie"),  # fixture values only, none real
                   ("fixture-pass-9b1e", "vault-ref-person@example.test", "482915", "sess-cookie-value-5c2d")))


def test_a_chain_sends_nothing_its_sign_in_read_only_the_values_to_redact(monkeypatch, tmp_path):
    # Each secret a sign-in reads (a password, an email by vault reference, a TOTP code, a cookie value) is shown by
    # the page: in a check, a finding, Jev's history and the reason. Only the chain's process could redact it.
    from qajev import session

    monkeypatch.setattr(config, "_REMEMBERED", set())
    monkeypatch.setattr(chrome, "start", lambda profile, **kw: {"cdp_url": "http://127.0.0.1:9", "state_key": "k"})
    monkeypatch.setattr(chrome, "stop", lambda key, group=False: None)
    monkeypatch.setattr(session, "daemon_pid", lambda: None)

    def sign_in_first(suite, **kw):
        for value in SECRETS.values():  # as vault.resolve and signin remember them
            config.remember_secret(value)
        return {"account": "tester", "ok": True}

    def run_group(chain, *, opts, **kw):
        shown = " ".join(SECRETS.values())
        result = {"name": chain[0].name, "outcome": "fail", "reason": f"the page said {shown}",
                  "checks": [{"check": "text", "ok": False, "detail": shown}],
                  "findings": [{"severity": "S2", "title": f"console: {shown}"}],
                  "history": [{"kind": "type", "action": "Email", "text": SECRETS["email"]}]}
        opts.emit({"event": "start", "scenario": chain[0].name})
        opts.emit({"event": "step", "scenario": chain[0].name, "doing": f"typing {SECRETS['code']}"})
        opts.emit({"event": "scenario", "result": result})
        return {"results": [result], "ledger": kw["ledger"].summary()}

    monkeypatch.setattr(runner, "sign_in_first", sign_in_first)
    monkeypatch.setattr(runner, "run_group", run_group)
    spec = parse([{"name": "account page", "url": "/account"}],
                 account={"email": "seed:qa_test_user.json#email", "password": "seed:qa_test_user.json#password",
                          "login": {"url": "/login.html"}})
    messages = queue.Queue()
    opts_dict = {k: v for k, v in runner.Options(headless=True).__dict__.items() if k != "emit"}
    runner._fresh_chain((spec, spec.scenarios, opts_dict, {"headless": True, "motion": "reduce"}, tmp_path, 0.0, 0.0,
                         None, None, False), messages)
    said = []
    while not messages.empty():
        said.append(messages.get_nowait())
    told = [body for kind, body in said if kind == "secrets"]
    assert sorted(v for body in told for v in body) == sorted(SECRETS.values())  # the parent learns what to redact
    rest = repr([m for m in said if m[0] != "secrets"])
    assert "[redacted]" in rest and not [k for k, v in SECRETS.items() if v in rest]
