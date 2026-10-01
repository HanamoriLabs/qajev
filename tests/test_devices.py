"""Every website test runs on desktop and on a phone by default (devices: [desktop, phone])."""


import pytest

from qajev import suite as suite_mod
from qajev.cli import build_parser, check_suite


def base(**extra):
    return {"name": "shop", "base_url": "http://127.0.0.1:8765", "scenarios": [
        {"name": "pricing", "url": "/", "goal": "Find the price. Stop when visible.", "expect": {"text": ["$29"]}},
        {"name": "then home", "goal": "Go home. Stop when home.", "expect": {"url": "/"}},
    ], **extra}


def test_a_suite_runs_every_scenario_on_desktop_and_on_a_phone_by_default(monkeypatch):
    monkeypatch.delenv("QAJEV_DEVICES", raising=False)
    s = suite_mod.parse(base())
    assert [x.name for x in s.scenarios] == ["pricing", "then home", "pricing (phone)", "then home (phone)"]
    phone = s.scenarios[2]
    assert phone.device["mobile"] is True and phone.device["width"] < 500 and "Mobile" in phone.device["ua"]
    assert s.scenarios[3].depends_on == ["pricing (phone)"]  # each device keeps its own chain
    assert s.scenarios[0].device["mobile"] is False


def test_a_pinned_device_runs_once_and_devices_can_be_narrowed(monkeypatch):
    monkeypatch.delenv("QAJEV_DEVICES", raising=False)
    data = base()
    data["scenarios"][0]["device"] = "tablet"
    data["scenarios"][1]["url"] = "/"
    s = suite_mod.parse(data)
    assert [x.name for x in s.scenarios] == ["pricing", "then home", "then home (phone)"]
    assert [x.name for x in suite_mod.parse(base(device="phone")).scenarios] == ["pricing", "then home"]
    assert [x.name for x in suite_mod.parse(base(devices=["desktop"])).scenarios] == ["pricing", "then home"]
    assert [x.name for x in suite_mod.parse(base(), devices=["phone", "tablet"]).scenarios] == [
        "pricing", "then home", "pricing (tablet)", "then home (tablet)"]
    monkeypatch.setenv("QAJEV_DEVICES", "desktop")
    assert len(suite_mod.parse(base()).scenarios) == 2
    with pytest.raises(suite_mod.SuiteError, match="devices"):
        suite_mod.parse(base(devices=["fridge"]))


def test_qajev_check_tests_the_phone_view_too_unless_a_device_is_pinned(monkeypatch):
    monkeypatch.delenv("QAJEV_DEVICES", raising=False)
    args = build_parser().parse_args(["check", "http://127.0.0.1:8765/", "--expect-text", "x"])
    assert [s.name for s in check_suite(args).scenarios] == ["check", "check (phone)"]
    args = build_parser().parse_args(["check", "http://127.0.0.1:8765/", "--expect-text", "x", "--device", "desktop"])
    assert [s.name for s in check_suite(args).scenarios] == ["check"]
    args = build_parser().parse_args(["check", "http://127.0.0.1:8765/", "--expect-text", "x", "--devices", "desktop"])
    assert [s.name for s in check_suite(args).scenarios] == ["check"]


def test_a_phone_tab_also_says_it_is_a_phone():
    from qajev.session import Session

    calls = []
    s = Session.__new__(Session)
    s.device, s.call = None, lambda method, **params: calls.append((method, params))
    s.set_device(suite_mod.DEVICES["phone"])
    ua = next(p for m, p in calls if m == "Emulation.setUserAgentOverride")
    assert "iPhone" in ua["userAgent"] and ua.get("platform") == "iPhone"
    calls.clear()
    s.set_device(suite_mod.DEVICES["desktop"])
    assert ("Emulation.setUserAgentOverride", {"userAgent": ""}) in calls  # back to the browser's own


def test_real_device_steps_come_from_the_desktop_scenarios_and_skip_writes(monkeypatch):
    from qajev import mobile

    monkeypatch.delenv("QAJEV_DEVICES", raising=False)
    data = base()
    data["scenarios"].append({"name": "send feedback", "url": "/feedback.html", "mode": "mutate",
                              "goal": "Send it. Stop when thanked.", "expect": {"text": ["Thanks"]}})
    s = suite_mod.parse({**data, "real_devices": ["ios"]})
    assert s.real_devices == ["ios"]
    steps = mobile.web_steps(s, "ios")
    assert [x["name"] for x in steps] == ["pricing (iOS Safari)", "then home (iOS Safari)",
                                          "send feedback (iOS Safari)"]
    assert steps[0]["open"] == "http://127.0.0.1:8765/" and steps[0]["expect"] == {"text": ["$29"]}
    assert "open" not in steps[1]  # a continuation stays on the same page, as on desktop
    assert steps[2].get("skip") and "changes data" in steps[2]["skip"]
    with pytest.raises(suite_mod.SuiteError, match="real_devices"):
        suite_mod.parse({**data, "real_devices": ["windows"]})


def test_a_read_only_device_run_hides_write_like_buttons():
    from qajev import native

    obs = {"readonly": True, "actions": [{"id": "a", "label": "Submit", "kind": "click"},
                                         {"id": "b", "label": "Pricing", "kind": "click"}]}
    assert [a["label"] for a in native.visible_actions(obs)[0]] == ["Pricing"]
