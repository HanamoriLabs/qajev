"""Malformed DNS labels fail plainly before routing, job creation or browser work."""
import asyncio
import json

import pytest

from qajev import cli, cloud, config, jobs, mcp_server, suite
from qajev.mcp_server import ToolError

INVALID = ["http://a..b/", "http://" + "a" * 64 + ".example/"]


@pytest.mark.parametrize("url", INVALID, ids=["empty-label", "long-label"])
@pytest.mark.parametrize("flags", [["--runner", "local"], ["--runner", "cloud"], ["--background"]],
                         ids=["local", "cloud", "background"])
def test_invalid_host_is_refused_before_routing_recording_or_browser_start(url, flags, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(cli, "_route", lambda *_: called.append("route") or 0)
    monkeypatch.setattr(jobs, "register", lambda *_a, **_k: called.append("record"))
    monkeypatch.setattr(jobs, "start", lambda *_a, **_k: called.append("start"))
    code = cli.main(["check", url, "--expect-text", "ready", *flags])
    out, err = capsys.readouterr()
    assert code == cli.EXIT_CONFIG
    assert "invalid URL host" in err and "DNS labels" in err
    assert "Traceback" not in out + err
    assert called == []


@pytest.mark.parametrize("url", INVALID, ids=["empty-label", "long-label"])
def test_invalid_host_has_a_structured_json_error(url, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_route", lambda *_: pytest.fail("must refuse before routing"))
    assert cli.main(["check", url, "--expect-text", "ready", "--json"]) == cli.EXIT_CONFIG
    out, err = capsys.readouterr()
    assert json.loads(out)["exit_code"] == cli.EXIT_CONFIG
    assert "invalid URL host" in json.loads(out)["error"]
    assert "Traceback" not in err


@pytest.mark.parametrize("url", INVALID, ids=["empty-label", "long-label"])
@pytest.mark.parametrize("background", [False, True])
def test_mcp_refuses_invalid_host_before_recording_or_submission(url, background, monkeypatch):
    monkeypatch.setattr(jobs, "start", lambda *_a, **_k: pytest.fail("no job may start"))
    monkeypatch.setattr(cloud, "connect", lambda *_a, **_k: pytest.fail("no cloud request"))
    with pytest.raises(ToolError, match="invalid URL host"):
        asyncio.run(mcp_server._dispatch(["check", url, "--expect-text", "ready"], ["--runner", "cloud"],
                                         None, background))


@pytest.mark.parametrize("url", INVALID, ids=["empty-label", "long-label"])
@pytest.mark.parametrize("base", [False, True], ids=["scenario-url", "base-url"])
def test_suite_refuses_invalid_host_before_browser_work(url, base):
    data = {"name": "host check", "scenarios": [{"name": "page", "url": "/" if base else url,
                                                 "expect": {"text": ["ready"]}}]}
    if base:
        data["base_url"] = url
    with pytest.raises(suite.SuiteError, match="invalid URL host"):
        suite.parse(data)


@pytest.mark.parametrize("url", ["https://bücher.example/", "https://valid.example/", "http://127.0.0.1:3000/",
                                 "http://[::1]:3000/", "/pricing", "https://valid.example./"])
def test_valid_hosts_and_relative_paths_are_not_refused(url):
    assert config.host_refusal(url) is None


def test_expect_url_pattern_is_not_a_host_target():
    args = cli.build_parser().parse_args(["check", "https://valid.example/", "--expect-url", ".*example"])
    args.argv = ["check", "https://valid.example/", "--expect-url", ".*example"]
    assert cli.url_refusal(args) is None
