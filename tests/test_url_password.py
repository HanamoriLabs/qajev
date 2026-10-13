"""A URL that holds a user name or password is refused before Chrome starts (the Orchestrator, 13 Oct).

Redteam found that a smoke of https://alice:s3cret@shop.example:8443/ wrote "s3cret" into report.json and report.html,
and a cloud run uploads those files. QAJev never writes down a password, so it refuses the URL and says to sign in once
through a profile. Each test searches every file the run left behind for the password.
"""

import pytest

from qajev import chrome, cli, config

PASSWORD = "s3cret"
URL = f"https://alice:{PASSWORD}@shop.example:8443/"


@pytest.fixture
def started(monkeypatch, tmp_path):
    """Records each Chrome start (there must be none) and keeps every file this test's run writes under tmp_path."""
    home = tmp_path / "qajev-home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.chdir(tmp_path)
    for name in ("QAJEV_RUNNER", "QAJEV_CLOUD_URL", "QAJEV_CLOUD_KEY", "QAJEV_PROFILE", "QAJEV_REQUIRE_PLAN",
                 "QAJEV_JOB"):
        monkeypatch.delenv(name, raising=False)
    seen = []

    def start(*a, **k):
        seen.append(a)
        raise RuntimeError("no Chrome in tests")

    monkeypatch.setattr(chrome, "start", start)
    monkeypatch.setattr(chrome, "reap", lambda: [])
    return seen


def leaked(root, inputs=()):
    """Every file under root, except the inputs this test wrote itself, that holds the password."""
    return [str(p) for p in root.rglob("*")
            if p.is_file() and p not in inputs and PASSWORD in p.read_bytes().decode("utf-8", "replace")]


def suite_with(tmp_path, base, url="/"):
    path = tmp_path / "s.yaml"
    where = f"    url: {url}\n" if url else ""  # no url: the first scenario opens base_url as it is
    path.write_text(f"name: shop\nbase_url: {base}\nscenarios:\n  - name: home\n{where}"
                    "    about: the home page greets a visitor\n    expect: {text: [Welcome]}\n")
    return path


def project_with(tmp_path, base):
    repo = tmp_path / "shop"
    (repo / ".qajev").mkdir(parents=True)
    toml = repo / ".qajev" / "project.toml"
    toml.write_text(f'name = "shop"\n[env.prod]\nbase_url = "{base}"\n'
                    '[[objective]]\nname = "home"\ngoal = "Open the home page. Stop when it shows."\n'
                    'about = "the home page greets a visitor"\nexpect = { text = ["Welcome"] }\n')
    return repo, toml


@pytest.mark.parametrize("make", [
    lambda tmp: (["check", URL, "--expect-text", "Welcome"], ()),
    lambda tmp: (["smoke", URL, "--max-pages", "1"], ()),
    lambda tmp: (lambda p: (["run", str(p)], (p,)))(suite_with(tmp, URL)),
    lambda tmp: (lambda p: (["run", str(p)], (p,)))(suite_with(tmp, URL, None)),
    lambda tmp: (lambda p: (["run", str(p)], (p,)))(suite_with(tmp, "https://shop.example", URL + "cart")),
    lambda tmp: (lambda r: (["run", "--project", str(r[0])], (r[1],)))(project_with(tmp, URL)),
    lambda tmp: (lambda r: (["smoke", "--project", str(r[0])], (r[1],)))(project_with(tmp, URL)),
], ids=["check", "smoke", "suite base_url", "suite base_url alone", "suite scenario url", "project run",
        "project smoke"])
def test_a_url_with_a_password_is_refused_and_written_nowhere(started, tmp_path, capsys, make):
    argv, inputs = make(tmp_path)

    code = cli.main(argv)

    out, err = capsys.readouterr()
    assert code == cli.EXIT_CONFIG
    assert started == []  # refused before Chrome starts
    assert "user name or password" in err and "qajev browser login" in err
    assert PASSWORD not in out + err
    assert leaked(tmp_path, inputs) == []


def test_a_url_without_user_info_is_not_refused_for_it():
    assert config.user_info_refusal("https://shop.example:8443/a@b?c=d@e") is None  # an "@" in the path or query
    assert config.user_info_refusal("http://127.0.0.1:3000/") is None
    assert config.user_info_refusal("http://evil.com\\@localhost/") is None  # a browser goes to evil.com: no user info
    said = config.user_info_refusal(URL)
    assert said and PASSWORD not in said and "alice" not in said and "shop.example:8443" in said


@pytest.mark.parametrize("argv", [["check", URL], ["smoke", URL],
                                  ["check", "https://shop.example/", "--fetch", URL + "health=200"]])
def test_a_cloud_run_with_a_password_in_a_url_is_refused_before_anything_is_sent(started, monkeypatch, capsys, argv):
    from qajev import cloud

    sent = []
    monkeypatch.setattr(cloud, "connect", lambda *a, **k: sent.append(a) or pytest.fail("nothing goes to the cloud"))
    monkeypatch.setenv("QAJEV_CLOUD_URL", "https://cloud.invalid")
    monkeypatch.setenv("QAJEV_CLOUD_KEY", "test-key")

    code = cli.main([*argv, "--runner", "cloud"])

    err = capsys.readouterr().err
    assert code == cli.EXIT_CONFIG and sent == [] and started == []
    assert "user name or password" in err and PASSWORD not in err


def test_an_agent_run_with_a_password_in_its_url_is_refused_before_its_job_is_recorded(monkeypatch):
    import asyncio

    from mcp.server.mcpserver.exceptions import ToolError

    from qajev import jobs, mcp_server

    recorded = []
    monkeypatch.setattr(jobs, "start", lambda *a, **k: recorded.append(a))
    with pytest.raises(ToolError, match="user name or password") as refused:
        asyncio.run(mcp_server._run_report(["check", URL, "--expect-text", "Welcome"], None, background=True))
    assert recorded == [] and PASSWORD not in str(refused.value)
