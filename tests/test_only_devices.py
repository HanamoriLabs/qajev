"""--only NAME runs that scenario on every device. JSR price run A (30 Oct): --only ran the desktop copy only, and the
phone copy ("NAME (phone)") was left out with no warning."""

import pytest

from qajev import runner, suite


def two_devices(*scenarios):
    return suite.parse({"devices": ["desktop", "phone"], "scenarios": [
        {"name": name, "url": "http://127.0.0.1:8000/", "expect": {"text": ["Hi"]}, **extra}
        for name, extra in scenarios]})


def names(chosen):
    return [s.name for s in chosen]


def test_a_base_name_runs_every_device_copy_and_a_copys_name_runs_that_copy():
    s = two_devices(("landing en", {}), ("pricing", {}))
    assert names(runner.select(s, ["landing en"])) == ["landing en", "landing en (phone)"]
    assert names(runner.select(s, ["landing en (phone)"])) == ["landing en (phone)"]


def test_each_copy_brings_its_own_devices_dependencies():
    s = two_devices(("sign in", {}), ("cart", {"depends_on": ["sign in"]}))
    assert names(runner.select(s, ["cart"])) == ["sign in", "cart", "sign in (phone)", "cart (phone)"]


def test_a_scenario_its_author_named_like_a_copy_is_not_one():
    s = suite.parse({"devices": ["desktop"], "scenarios": [
        {"name": "landing", "url": "http://127.0.0.1:8000/", "expect": {"text": ["Hi"]}},
        {"name": "landing (old)", "url": "http://127.0.0.1:8000/old", "expect": {"text": ["Hi"]}}]})
    assert names(runner.select(s, ["landing"])) == ["landing"]


def test_an_unknown_name_is_still_refused():
    with pytest.raises(runner.ConfigError, match="no scenario named"):
        runner.select(two_devices(("landing", {})), ["nope"])


def rerun_selects(monkeypatch, s, outcomes):
    """What `qajev rerun JOB --failed` runs for a finished 2-device job with these outcomes: its argv, parsed by the
    CLI, then selected from the suite."""
    from qajev import cli, jobs

    monkeypatch.setattr(jobs, "status", lambda job, detail=False: {"state": "done", "argv": ["run", "suite.yaml"]})
    monkeypatch.setattr(jobs, "result", lambda job: {"scenarios": [{"name": n, "outcome": o}
                                                                   for n, o in outcomes.items()]})
    argv, _what = jobs.rerun_argv("job", failed=True)
    args = cli.build_parser().parse_args(argv)
    return names(runner.select(s, args.only, exact=args.exact))


def test_rerun_failed_never_runs_again_a_device_copy_that_passed(monkeypatch):
    s = two_devices(("sign in", {}), ("landing", {"depends_on": ["sign in"]}), ("cart", {}))
    passed = {n: "pass" for n in names(s.scenarios)}
    assert rerun_selects(monkeypatch, s, {**passed, "landing": "fail"}) == ["sign in", "landing"]
    assert rerun_selects(monkeypatch, s, {**passed, "cart (phone)": "fail"}) == ["cart (phone)"]


def test_each_guard_alone_keeps_a_look_alike_out():
    def one_device(other):
        return suite.parse({"devices": ["desktop"], "scenarios": [
            {"name": "landing", "url": "http://127.0.0.1:8000/", "expect": {"text": ["Hi"]}},
            {"name": "landing (old)", "expect": {"text": ["Hi"]}, **other}]})

    same_place = one_device({"url": "http://127.0.0.1:8000/"})  # only the device tells it apart
    assert names(runner.select(same_place, ["landing"])) == ["landing"]
    elsewhere = one_device({"url": "http://127.0.0.1:8000/old", "device": "phone"})  # only the url tells it apart
    assert names(runner.select(elsewhere, ["landing"])) == ["landing"]
