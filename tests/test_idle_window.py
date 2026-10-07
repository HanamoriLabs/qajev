"""A game step's `before:` (where it starts) and an idle step's `lasted:` (how long that state held with no input),
and an idle counted in the step's time. im-him plan 40, step 6: "the ending cutscene plays out (about 30 s)" passed
with 0 s on the clock, and a cutscene that never opened, or closed after 0.1 s, would have passed the same way."""

import pytest

from qajev import native, plan, report_html


class _NoLedger:
    def spent(self):
        return 0.0


class Cutscene:
    """The ENDING cutscene runs until `ends_at` seconds on the fake clock, then the game is back to PLAYING.
    `lag`: each look takes that long (a loaded machine) from `slow_from` on."""

    errors = []

    def __init__(self, clock, ends_at, *, starts_on="ENDING", lag=0.0, slow_from=0.0):
        self.clock, self.ends_at, self.starts_on, self.lag, self.slow_from = clock, ends_at, starts_on, lag, slow_from
        self.acted = []

    def call(self, **_):
        return {"ok": True}

    def observe(self):
        if self.clock["t"] >= self.slow_from:
            self.clock["t"] += self.lag
        screen = self.starts_on if self.clock["t"] < self.ends_at else "PLAYING"
        return {"screen": screen, "texts": [], "actions": [], "state": {}}

    def act(self, action):
        self.acted.append(action)

    def shot(self, _path):
        return None


@pytest.fixture
def clock(monkeypatch):
    t = {"t": 0.0}
    monkeypatch.setattr(native.time, "monotonic", lambda: t["t"])
    monkeypatch.setattr(native.time, "sleep", lambda s: t.__setitem__("t", t["t"] + s))
    return t


STEP = {"name": "the ending plays out", "before": {"screen": "ENDING"}, "idle": 45, "lasted": {"min": 25, "max": 40},
        "expect": {"screen": "PLAYING"}}


def run(game, step=STEP):
    native.check_window([step])
    [r] = native.run_session(game, [step], ledger=_NoLedger(), run_dir=None, shots=False)
    return r


def test_an_idle_counts_in_the_steps_time_and_the_report_says_so(clock):
    r = run(Cutscene(clock, 30.2), {"name": "wait", "idle": 45, "expect": {"screen": "PLAYING"}})
    assert r["outcome"] == "pass" and r["idled_s"] == pytest.approx(45, abs=1) and r["seconds"] >= 45
    assert "idled 45 s, no input from QAJev" in report_html._scenario(0, r)


def test_a_cutscene_that_plays_out_in_its_window_passes(clock):
    game = Cutscene(clock, 30.2)
    r = run(game)
    assert r["outcome"] == "pass" and game.acted == []
    start, lasted = r["checks"][:2]
    assert start["check"] == "at the start: screen is ENDING" and start["ok"]
    assert lasted["check"] == "lasted: screen is ENDING held 25 to 40 s" and lasted["ok"]
    assert lasted["detail"].startswith("ended between 30.0 and 30.5 s")
    assert lasted["says"] == "with no input, the screen is ENDING for 25 to 40 s"


def test_a_step_that_does_not_start_where_it_says_fails_and_does_not_run(clock):
    r = run(Cutscene(clock, 30.2, starts_on="PLAYING"))  # the cutscene never opened
    assert r["outcome"] == "fail" and clock["t"] < 1  # it did not idle
    assert r["reason"] == "at the start: screen is ENDING (on PLAYING); the step did not run"


@pytest.mark.parametrize("ends_at, why", [(0.1, "ended between 0.0 and 0.5 s"), (99, "still held after 4")])
def test_a_cutscene_too_short_or_still_on_at_the_max_fails(clock, ends_at, why):
    r = run(Cutscene(clock, ends_at))
    lasted = r["checks"][1]
    assert r["outcome"] == "fail" and not lasted["ok"] and not lasted.get("broke") and why in lasted["detail"]


def test_when_the_machine_is_too_slow_to_tell_it_is_harness_not_a_fail(clock):
    # looks 6 s apart from 20 s on: the cutscene ended at 24.0, somewhere between a look before 25 s and one after
    r = run(Cutscene(clock, 24.0, lag=5.5, slow_from=20.0))
    lasted = r["checks"][1]
    assert lasted.get("broke") and "too slow to tell" in lasted["detail"]
    assert r["outcome"] == "harness" and r["look_gap_s"] > 5


def test_checks_after_a_measured_idle_get_no_grace_period(clock):
    # The game returns to PLAYING 3 s after the idle ends: a check-only step would wait up to 10 s and pass.
    step = {**STEP, "lasted": {"min": 25}, "expect": {"screen": "PLAYING"}}
    r = run(Cutscene(clock, 48.0), step)
    assert r["outcome"] == "fail" and "screen is PLAYING (on ENDING)" in r["reason"]


@pytest.mark.parametrize("step, says", [
    ({"idle": 45, "lasted": {"max": 40}}, "needs before: and idle:"),
    ({"before": {"screen": "ENDING"}, "lasted": {"max": 40}}, "needs before: and idle:"),
    ({"before": {"screen": "ENDING"}, "idle": 45, "lasted": {"min": 40, "max": 25}}, "min 40 is more than max 25"),
    ({"before": {"screen": "ENDING"}, "idle": 40, "lasted": {"max": 40}}, "must be longer than lasted: max 40"),
    ({"before": {"screen": "ENDING"}, "idle": 45, "lasted": {"max": "40"}}, "takes min and/or max"),
    ({"before": {"screen": "ENDING"}, "idle": 45, "lasted": {"for": 30}}, "takes min and/or max"),
    ({"before": {"fps": 60}}, "takes screen, text and state"),
    ({"before": {"screen": "MENU"}, "relaunch": True}, "cannot go on a relaunch"),
])
def test_a_bad_before_or_lasted_is_refused_when_the_suite_loads(step, says):
    with pytest.raises(native.NativeError, match=says):
        native.check_window([{"name": "s", **step}])


def test_the_plan_lists_the_start_and_the_window_in_plain_words():
    [it] = plan.from_steps([{**STEP, "about": "The ending plays out by itself."}])
    assert [line["words"] for line in it["checks"]] == [
        "at the start: the screen is ENDING", "with no input, the screen is ENDING for 25 to 40 s",
        "the screen is PLAYING", "no script errors"]
    assert it["described"]
