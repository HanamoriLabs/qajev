"""Handoff: a step a person does by hand while the run waits; QAJev only watches (Twinbox's journey, 13 Oct)."""

import json

import pytest

from qajev import cli, native
from qajev.cli import main
from qajev.handoff import check_handoff, handoff_step
from qajev.native import NativeError

SPEC = {"who": "José", "does": "Authorize GitHub on the box with the code it shows",
        "until": {"github_signed_in": True}, "minutes": 10}


class App:
    """A stand-in app whose state turns true after `after` looks, or never. It records any input it gets."""

    def __init__(self, after=None, dies_at=None):
        self.looks, self.after, self.dies_at, self.inputs = 0, after, dies_at, []
        self.errors = []  # as a real game's: a step that is not a handoff reads it, and must fail by name, not crash

    def observe(self):
        self.looks += 1
        if self.dies_at is not None and self.looks >= self.dies_at:
            raise NativeError("the game closed its bridge (crashed or quit)")
        done = self.after is not None and self.looks > self.after
        copy = {"id": "x", "label": "Copy", "kind": "click", "x": 1, "y": 1}
        return {"screen": "box", "texts": [], "actions": [copy], "state": {"github_signed_in": done}}

    def act(self, action):
        self.inputs.append(action)

    def call(self, **request):
        self.inputs.append(request)
        return {"ok": True}


def test_the_step_passes_once_the_person_has_done_it_and_qajev_sends_no_input():
    app, events = App(after=3), []
    r = handoff_step(app, name="GitHub sign-in", spec=SPEC, emit=events.append, look=0.01)
    assert r["outcome"] == "pass"
    assert r["checks"] == [{"check": "José did it: github_signed_in = true", "ok": True, "detail": None}]
    assert r["handoff"]["who"] == "José" and r["handoff"]["waited_s"] >= 0
    assert app.inputs == []  # no click, no key, no pilot: the person acts, QAJev only reads
    told = [e for e in events if e["event"] == "handoff"]
    assert told == [{"event": "handoff", "scenario": "GitHub sign-in", "who": "José",
                     "does": "Authorize GitHub on the box with the code it shows", "minutes": 10,
                     "at": told[0]["at"]}]
    assert any(e["event"] == "step" and e["doing"].startswith("waiting for José: Authorize GitHub") for e in events)


def test_a_person_who_did_not_come_in_time_is_stuck_not_a_product_failure():
    app = App(after=None)
    r = handoff_step(app, name="GitHub sign-in", spec={**SPEC, "minutes": 0.001}, look=0.01)
    assert r["outcome"] == "stuck" and r["stop"] == "handoff_timeout"
    assert r["checks"][0]["ok"] is False and "github_signed_in was false" in r["checks"][0]["detail"]
    assert "waited 0.001 min for José (“Authorize GitHub" in r["reason"]
    assert "says nothing about the product" in r["reason"]
    assert app.inputs == []


def test_an_app_that_stops_answering_while_it_waits_is_a_harness_stop():
    r = handoff_step(App(after=None, dies_at=3), name="GitHub sign-in", spec=SPEC, look=0.01)
    assert r["outcome"] == "harness" and r["stop"] == "browser_error"
    assert "stopped answering" in r["reason"] and "waiting for José" in r["reason"]


def test_a_handoff_step_in_a_session_runs_as_its_own_step(tmp_path):
    from qajev.ledger import Ledger

    steps = [{"name": "GitHub sign-in", "about": "The owner authorizes GitHub on the box.", "handoff": SPEC}]
    app = App(after=1)
    results = native.run_session(app, steps, ledger=Ledger(1.0, 0.0), run_dir=tmp_path, shots=False)
    assert results[0]["outcome"] == "pass" and results[0]["handoff"]["does"].startswith("Authorize GitHub")
    assert not [i for i in app.inputs if i.get("op") != "pilot"]  # at most the usual "pilot off", never an act


@pytest.mark.parametrize("handoff, says", [
    ("José signs in", "needs who:, does: and until:"),
    ({**SPEC, "code": "1234-ABCD"}, "unknown key"),
    ({**SPEC, "who": " "}, "handoff.who"),
    ({**SPEC, "does": None}, "handoff.does"),
    ({**SPEC, "until": {}}, "handoff.until"),
    ({**SPEC, "minutes": 0}, "handoff.minutes: from 1 to 30"),
    ({**SPEC, "minutes": 31}, "handoff.minutes: from 1 to 30"),
])
def test_a_wrong_handoff_is_refused_naming_what_is_wrong(handoff, says):
    with pytest.raises(NativeError, match=says):
        check_handoff([{"name": "sign-in", "handoff": handoff}])


@pytest.mark.parametrize("acting", ["goal", "play", "js", "pad", "keys", "idle", "relaunch", "open"])
def test_a_handoff_step_that_would_also_act_is_refused(acting):
    with pytest.raises(NativeError, match=f"only waits, and QAJev acts nowhere in it: put {acting}"):
        check_handoff([{"name": "sign-in", "handoff": SPEC, acting: "x"}])


def test_a_handoff_step_with_its_own_expect_is_refused_because_until_is_its_one_check():
    with pytest.raises(NativeError, match="only check is handoff.until; put expect: in the next step"):
        check_handoff([{"name": "sign-in", "handoff": SPEC, "expect": {"text": ["Signed in"]}}])


def test_qajev_play_refuses_a_wrong_handoff_when_the_suite_loads(tmp_path, capsys):
    suite = tmp_path / "s.yaml"
    suite.write_text("steps:\n  - name: sign-in\n    handoff: {who: José, does: sign in, until: {}}\n")
    env = tmp_path / ".env"
    env.write_text("")
    assert main(["play", str(tmp_path), "--suite", str(suite), "--json", "--env-file", str(env)]) == cli.EXIT_CONFIG
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "handoff.until" in out["error"]
