import asyncio
from types import SimpleNamespace

import pytest

from qajev import cli, cloud, mcp_server, suite, ux_options


def test_defaults_and_old_boolean_modes_are_resolved_without_shared_lists():
    assert ux_options.resolve()["ux"] == "advisory"
    assert ux_options.resolve()["ux_keyboard_press_return"] is False
    assert ux_options.resolve()["ux_settle_timeout_ms"] == 10000
    assert ux_options.resolve({"ux": True})["ux"] == "advisory"
    assert ux_options.resolve({"ux": False})["ux"] == "off"
    first = ux_options.resolve()
    first["ux_checks"].clear()
    assert ux_options.resolve()["ux_checks"] == list(ux_options.CHECKS)


def test_run_options_beat_inherited_values_and_none_keeps_the_suite_value():
    inherited = {"ux": "strict", "ux_shift_px": 12, "ux_keyboard_press_return": True}
    options = SimpleNamespace(ux=None, ux_shift_px=0, ux_keyboard_press_return=False)
    resolved = ux_options.resolve(options, inherited)
    assert resolved["ux"] == "strict"
    assert resolved["ux_shift_px"] == 0
    assert resolved["ux_keyboard_press_return"] is False
    assert resolved["ux_settle_ms"] == 750


@pytest.mark.parametrize("cap", [0, 125, 10000])
def test_wait_cap_reaches_real_run_options_and_inherits_per_scenario(cap):
    from qajev.runner import Options

    loaded = suite.parse({"base_url": "http://example.test", "ux_settle_timeout_ms": 1000,
                          "scenarios": [{"name": "inherited", "expect": {"js": "true"}},
                                        {"name": "override", "ux_settle_timeout_ms": cap,
                                         "expect": {"js": "true"}}]})
    assert loaded.scenarios[0].ux_options["ux_settle_timeout_ms"] == 1000
    assert loaded.scenarios[1].ux_options["ux_settle_timeout_ms"] == cap
    assert ux_options.resolve(Options(ux_settle_timeout_ms=cap), loaded.ux_options)["ux_settle_timeout_ms"] == cap


@pytest.mark.parametrize("key,value", [
    ("ux", "enabled"), ("ux", 1),
    ("ux_checks", ["cover", "imagined"]), ("ux_checks", [1]),
    ("ux_keyboard_scope", "sometimes"), ("ux_keyboard_main", "scroll"),
    ("ux_keyboard_ratio", 0), ("ux_keyboard_ratio", 1),
    ("ux_keyboard_ratio", float("nan")), ("ux_keyboard_ratio", float("inf")),
    ("ux_keyboard_ratio", True), ("ux_keyboard_press_return", "false"),
    ("ux_settle_ms", -1), ("ux_settle_ms", 1.5), ("ux_settle_ms", True),
    ("ux_settle_timeout_ms", -1), ("ux_settle_timeout_ms", 10001),
    ("ux_settle_timeout_ms", 1.5), ("ux_settle_timeout_ms", True),
    ("ux_shift_px", -1), ("ux_shift_px", float("nan")),
    ("ux_tap_min_px", 0), ("ux_tap_min_px", float("inf")),
    ("ux_max_findings", 0), ("ux_max_findings", 1.5), ("ux_max_findings", True),
])
def test_invalid_values_are_refused_by_the_shared_parser_and_suite(key, value):
    with pytest.raises(ValueError, match=key):
        ux_options.resolve({key: value})
    data = {"base_url": "http://example.test", "scenarios": [{"expect": {"text": "ready"}}]}
    with pytest.raises(suite.SuiteError, match=key):
        suite.parse({**data, key: value})
    with pytest.raises(suite.SuiteError, match=key):
        suite.parse({**data, "scenarios": [{**data["scenarios"][0], key: value}]})


def test_suite_and_scenario_inherit_explicit_options_before_run_overrides():
    loaded = suite.parse({"base_url": "http://example.test", "ux": "strict", "ux_shift_px": 12,
                          "ux_checks": ["cover", "shift"], "device": "desktop", "scenarios": [
                              {"name": "first", "expect": {"text": "ready"}},
                              {"name": "second", "url": "/two", "ux": "off", "ux_shift_px": 0,
                               "expect": {"text": "ready"}},
                          ]})
    assert loaded.ux_options == {"ux": "strict", "ux_shift_px": 12, "ux_checks": ["cover", "shift"]}
    assert loaded.scenarios[0].ux_options == loaded.ux_options
    assert loaded.scenarios[1].ux_options["ux"] == "off"
    assert loaded.scenarios[1].ux_options["ux_shift_px"] == 0
    assert ux_options.resolve({"ux": "advisory"}, loaded.scenarios[1].ux_options)["ux"] == "advisory"
    loaded.scenarios[0].ux_options["ux_checks"].clear()
    assert loaded.ux_options["ux_checks"] == ["cover", "shift"]


FLAGS = ["--ux", "strict", "--ux-checks", "cover,keyboard,clip", "--ux-keyboard-scope", "all",
         "--ux-keyboard-ratio", "0.4", "--ux-keyboard-main", "visible", "--ux-keyboard-press-return",
         "--ux-settle-ms", "300", "--ux-settle-timeout-ms", "250", "--ux-shift-px", "0",
         "--ux-tap-min-px", "48", "--ux-max-findings", "3"]


@pytest.mark.parametrize("command,target", [("check", "https://example.com/"), ("run", "test.yaml"),
                                          ("smoke", "https://example.com/")])
def test_every_web_cli_accepts_and_serializes_all_explicit_ux_flags(command, target, monkeypatch):
    args = cli.build_parser().parse_args([command, target, *FLAGS])
    expected = {"ux": "strict", "ux_checks": ["cover", "keyboard", "clip"], "ux_keyboard_scope": "all",
                "ux_keyboard_ratio": .4, "ux_keyboard_main": "visible", "ux_keyboard_press_return": True,
                "ux_settle_ms": 300, "ux_settle_timeout_ms": 250,
                "ux_shift_px": 0, "ux_tap_min_px": 48, "ux_max_findings": 3}
    assert ux_options.parse(args) == expected
    from qajev import runner

    monkeypatch.setattr(runner, "Options", lambda **kwargs: kwargs)
    options = cli._options(args)
    assert {key: options[key] for key in ux_options.KEYS} == expected
    assert ux_options.parse(cli.build_parser().parse_args([command, target])) == {}
    off = cli.build_parser().parse_args([command, target, "--no-ux", "--no-ux-keyboard-press-return"])
    assert ux_options.parse(off) == {"ux": "off", "ux_keyboard_press_return": False}


@pytest.mark.parametrize("flag,value", [("--ux", "bad"), ("--ux-checks", "bad"),
                                      ("--ux-keyboard-ratio", "nan"), ("--ux-settle-ms", "-1"),
                                      ("--ux-max-findings", "0")])
def test_invalid_cli_options_fail_before_a_run(flag, value):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["check", "https://example.com/", flag, value])


def test_cloud_plan_forwards_ux_flags_without_consuming_them():
    argv = ["check", "https://example.com/", "--expect-text", "ready", *FLAGS]
    args = cli.build_parser().parse_args(argv)
    args.argv = argv
    prepared = cloud.plan(args)
    assert ux_options.parse(cli.build_parser().parse_args(prepared.args)) == ux_options.parse(args)


def test_every_web_mcp_entry_point_forwards_explicit_options_and_preserves_suite_defaults(monkeypatch):
    seen = []

    async def fake_dispatch(args, where, ctx, background, title=None):
        seen.append(args)
        return {"gate": "PASS", "run_dir": "/runs/test", "scenarios": []}

    monkeypatch.setattr(mcp_server, "_dispatch", fake_dispatch)
    options = {"ux": "strict", "ux_checks": ["cover", "keyboard", "clip"], "ux_keyboard_scope": "all",
               "ux_keyboard_ratio": .4, "ux_keyboard_main": "visible", "ux_keyboard_press_return": False,
               "ux_settle_ms": 300, "ux_settle_timeout_ms": 250,
               "ux_shift_px": 0, "ux_tap_min_px": 48, "ux_max_findings": 3}
    for tool, args, kwargs in [(mcp_server.qa_check, ("http://example.test/", None), {}),
                               (mcp_server.qa_run_suite, (None,), {"suite_path": "test.yaml"}),
                               (mcp_server.qa_smoke, ("http://example.test/", None), {}),
                               (mcp_server.qa_project_run, ("demo", None), {})]:
        asyncio.run(tool(*args, **kwargs, **options))
        assert ux_options.parse(cli.build_parser().parse_args(seen[-1])) == options
        asyncio.run(tool(*args, **kwargs))
        assert ux_options.parse(cli.build_parser().parse_args(seen[-1])) == {}
    asyncio.run(mcp_server.qa_smoke("http://example.test/", None, ux=False))
    assert ux_options.parse(cli.build_parser().parse_args(seen[-1])) == {"ux": "off"}
    with pytest.raises(mcp_server.ToolError, match="ux_keyboard_ratio"):
        asyncio.run(mcp_server.qa_check("http://example.test/", None, ux_keyboard_ratio=2))


def test_trim_keeps_ux_findings_with_the_scenario():
    ux = {"mode": "advisory", "findings": [{"check": "UX-COVER", "message": "covered"}], "notes": []}
    report = {"run_dir": "/runs/test", "scenarios": [{"name": "save", "outcome": "pass", "about": "save", "ux": ux}]}
    assert mcp_server._trim(report, False)["scenarios"][0]["ux"] == ux
