"""Each test in its own fresh Chrome (browser: fresh, the default). #802 (30 Oct): one shared Chrome cost 5-6.5 min a
step, as one test's open form or dialog held up the next; one process per test with a 150 s cap passed 21 of 21.
The chains' child processes are threads here: what the parent does with them is the contract."""

import queue
import threading
import time

import pytest

from qajev import chrome, report_html, runner, suite
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


@pytest.fixture
def fake(monkeypatch):
    """Chains run as threads that pass each test, hang in one named in `hang`, or end with no word in `crash`."""
    state = {"chains": [], "children": [], "hang": set(), "crash": set(), "wait": set(), "stopped": [],
             "started": [], "groups": []}

    def start_chain(_ctx, payload):
        chain = payload[1]
        messages, child = queue.Queue(), Child()
        state["chains"].append([s.name for s in chain])
        state["children"].append(child)
        key = f"chrome-{len(state['chains'])}"

        def script():
            messages.put(("chrome", key))
            results = []
            for s in chain:
                if s.name in state["crash"]:
                    child.exitcode = 1
                    return
                messages.put(("event", {"event": "start", "scenario": s.name}))
                if s.name in state["hang"]:
                    child.ended.wait()
                    return
                if s.name in state["wait"]:  # a quiet-machine wait longer than the test's cap
                    messages.put(("event", {"event": "waiting", "reason": "load1 200 >= 150"}))
                    time.sleep(1.5)
                results.append(runner._not_run(s, "pass", "done", "fixture"))
                messages.put(("event", {"event": "scenario", "result": results[-1]}))
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
    monkeypatch.setattr(chrome, "stop", state["stopped"].append)
    monkeypatch.setattr(chrome, "reap", lambda: [])
    monkeypatch.setattr(chrome, "status", lambda profile=None: [])
    monkeypatch.setattr(chrome, "run_record", lambda owned: {"managed": True, "cdp_url": owned["cdp_url"]})
    return state


def run(spec, tmp_path, **opts):
    return runner.run(spec, runner.Options(out_dir=tmp_path, headless=True, load_high=None, **opts))


def by_name(built):
    return {r["name"]: r for r in built["scenarios"]}


CHAINED = [{"name": "home", "url": "/"}, {"name": "pricing", "url": "/pricing"},
           {"name": "sign in", "url": "/login"}, {"name": "cart", "url": "/cart", "depends_on": ["sign in"]}]


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
    built = run(parse([{"name": "slow", "url": "/slow", "budget": {"seconds": 0.5}},
                       {"name": "after slow", "url": "/next", "depends_on": ["slow"]},
                       {"name": "quick", "url": "/"}]), tmp_path)
    got = by_name(built)
    assert (got["slow"]["outcome"], got["slow"]["stop"], got["slow"]["reason"]) == ("harness", "cap", "cap 1 s hit")
    assert got["after slow"]["outcome"] == "skipped" and "'slow'" in got["after slow"]["reason"]
    assert got["quick"]["outcome"] == "pass"  # the other tests run on
    hung = fake["chains"].index(["slow", "after slow"])
    assert fake["children"][hung].ended.is_set() and f"chrome-{hung + 1}" in fake["stopped"]
    assert built["gate"] == "INCOMPLETE"


def test_a_wait_for_a_quiet_machine_does_not_count_toward_the_cap(fake, monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "CAP_MARGIN", 0.5)
    fake["wait"] = {"busy"}
    got = by_name(run(parse([{"name": "busy", "url": "/", "budget": {"seconds": 0.5}}]), tmp_path))
    assert got["busy"]["outcome"] == "pass", got["busy"]


def test_a_chain_whose_process_ends_without_a_word_costs_its_own_tests_only(fake, tmp_path):
    fake["crash"] = {"pricing"}
    got = by_name(run(parse(CHAINED), tmp_path))
    assert got["pricing"]["outcome"] == "harness" and "exit 1" in got["pricing"]["reason"]
    assert got["home"]["outcome"] == got["cart"]["outcome"] == "pass"


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
