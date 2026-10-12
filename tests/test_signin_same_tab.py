"""login.tab: same (foleyapp, 13 Oct): the account signs in in the scenarios' own tab, so an app that keeps one writer
per origin (Foley's OPFS database) never sees a second tab. The default stays QAJev's own sign-in tab."""

from types import SimpleNamespace

import pytest

from qajev import runner, signin
from qajev.suite import SuiteError, _account

made = []


class FakeSession:
    def __init__(self, *a, **kw):
        made.append(self)

    def close(self):
        pass

    def page_socket(self):
        return None


@pytest.fixture
def group(monkeypatch, tmp_path):
    made.clear()
    seen = {"signin": [], "scenario": []}
    monkeypatch.setattr("qajev.session.Session", FakeSession)
    monkeypatch.setattr("qajev.session.configure_env", lambda cdp_url: "x")
    monkeypatch.setattr("qajev.session.stop_daemon", lambda: None)
    monkeypatch.setattr(runner.live, "frames", lambda *a, **kw: __import__("contextlib").nullcontext())

    def scenario(session, sc, **kw):
        seen["scenario"].append(session)
        return {"name": sc.name, "outcome": "pass", "checks": [], "findings": [], "screens": []}

    monkeypatch.setattr(runner, "run_scenario", scenario)
    meta = {"headless": True, "hosts": ["localhost:5284"], "guard": {}, "cwd": str(tmp_path)}
    scenarios = [SimpleNamespace(name=n, url="/", goal=None, mode="mutate", depends_on=[], clients=None)
                 for n in ("one", "two")]
    account = {"name": "A", "email": "a@foley.test", "password": "seed:x#A.password",
               "login": {"url": "/", "tab": "same"}}
    opts = SimpleNamespace(allow_commands=False, emit=None, sign_in_here=account, account=None)

    def run(ok=True):
        def fake_sign_in(session, acc):
            seen["signin"].append(session)
            if not ok:
                raise signin.SignInFailed("Wrong email or password")
            return {"account": acc["name"], "ok": True}

        monkeypatch.setattr(signin, "sign_in", fake_sign_in)
        monkeypatch.setattr(signin, "page_seen", lambda session: "")
        return runner.run_group(scenarios, opts=opts, cdp_url=None, suite_meta=meta, run_dir=tmp_path,
                                ledger=runner.Ledger(1.0)), opts

    return run, seen


def test_same_tab_signs_in_once_in_the_session_the_scenarios_use(group):
    run, seen = group
    out, opts = run()
    assert len(made) == 1 and seen["signin"] == made and seen["scenario"] == made * 2  # one tab for all of it
    assert opts.signed_here == {"account": "A", "ok": True, "tab": "same"} and opts.account == "A"
    assert [r["outcome"] for r in out["results"]] == ["pass", "pass"]


def test_a_failed_same_tab_sign_in_ends_every_scenario_as_harness_with_the_reason(group):
    run, seen = group
    out, opts = run(ok=False)
    assert seen["scenario"] == [] and opts.account is None
    assert [(r["outcome"], r["reason"]) for r in out["results"]] == [
        ("harness", "sign-in failed: Wrong email or password")] * 2


@pytest.mark.parametrize("tab", ["new", "", 1])
def test_login_tab_takes_own_or_same_only(tab):
    with pytest.raises(SuiteError, match="account.login.tab"):
        _account({"email": "qa@example.test", "password": "seed:f.json#p",
                  "login": {"url": "http://localhost:4000/", "tab": tab}}, None)
