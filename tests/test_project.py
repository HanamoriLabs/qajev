import json
import time

import pytest

from qajev import project as P
from qajev import suite as S
from qajev.suite import parse

TOML = """
name = "shop"
default_env = "prod"
[env.prod]
base_url = "https://shop.example"
hosts = ["cdn.shop.example"]
[env.local]
base_url = "http://localhost:3000"
mode = "mutate"
[accounts.tester]
email_env = "SHOP_TEST_EMAIL"
profile = "shop-local"
[budget]
cost_cap_usd = 0.25
actions = 12
[[objective]]
name = "price is visible"
url = "/pricing"
goal = "Find the monthly price. Stop when it is visible."
expect = { text = ["per month"] }
tags = ["core"]
[[objective]]
name = "faq explains refunds"
goal = "Open the FAQ and find the refund rule."
expect = { text = ["refund"] }
[[objective]]
name = "add a first item"
env = "local"
goal = "Add a first item to the basket."
expect = { text = ["1 item"] }
tags = ["core"]
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("QAJEV_REPORTS", str(tmp_path / "central"))
    (tmp_path / "shop" / ".qajev").mkdir(parents=True)
    (tmp_path / "shop" / ".qajev" / "project.toml").write_text(TOML)
    return tmp_path / "shop"


def test_a_repo_config_loads_and_reports_stay_in_the_repo(repo):
    p = P.load(repo)
    assert p.name == "shop" and p.in_repo and p.repo == repo
    assert p.reports_dir == repo / ".qajev" / "reports"
    folder = P.run_dir_parent(p)
    assert folder.parent == repo / ".qajev" / "reports"
    assert (repo / ".qajev" / ".gitignore").read_text() == "reports/\n"


def test_a_central_config_points_at_its_repo_and_reports_centrally(tmp_path, monkeypatch):
    monkeypatch.setenv("QAJEV_PROJECTS", str(tmp_path / "projects"))
    monkeypatch.setenv("QAJEV_REPORTS", str(tmp_path / "central"))
    monkeypatch.setattr(P, "_source_root", lambda: None)  # only this test's projects, not the checkout's
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "shop.toml").write_text(f'repo = "{tmp_path}/shop-repo"\n' + TOML)
    p = P.load("shop")
    assert not p.in_repo and p.repo == tmp_path / "shop-repo"
    assert p.reports_dir == tmp_path / "central" / "shop"
    assert [x["name"] for x in P.listing()] == ["shop"]


def test_core_suite_in_prod_is_read_only_and_uses_prod_objectives_only(repo):
    data = P.suite_data(P.load(repo), tags=["core"])
    suite = parse(data)
    assert [s.name for s in suite.scenarios] == ["price is visible"]
    assert suite.scenarios[0].url == "https://shop.example/pricing"
    assert suite.scenarios[0].mode == "readonly" and suite.cost_cap_usd == 0.25
    assert suite.scenarios[0].budget["actions"] == 12
    assert set(suite.hosts) == {"shop.example", "cdn.shop.example"}


def test_every_prod_objective_without_a_tag_filter(repo):
    names = [s["name"] for s in P.suite_data(P.load(repo))["scenarios"]]
    assert names == ["price is visible", "faq explains refunds"]
    assert P.suite_data(P.load(repo))["scenarios"][1]["url"] == "/"  # independent, not a continuation


def test_local_env_may_mutate_on_loopback(repo):
    suite = parse(P.suite_data(P.load(repo), env="local", tags=["core"]))
    assert [(s.name, s.mode) for s in suite.scenarios] == [("add a first item", "mutate")]


def test_mutate_on_a_production_host_is_still_refused(repo):
    p = P.load(repo)
    p.envs["prod"]["mode"] = "mutate"
    with pytest.raises(S.Refused, match=r"production site: (cdn\.)?shop\.example is not a local dev host"):
        parse(P.suite_data(p))
    p.envs["prod"]["mode"] = "readonly"
    p.envs["prod"]["allow_destructive"] = True  # showing destructive controls: the same refusal
    with pytest.raises(S.Refused, match=r"shop\.example is not a local dev host"):
        parse(P.suite_data(p))


def test_an_ad_hoc_objective(repo):
    data = P.suite_data(P.load(repo), objective="A new visitor finds the UK price", url="/uk",
                        expect={"text": ["£"]})
    (s,) = parse(data).scenarios
    assert s.name == "a-new-visitor-finds-the-uk-price" and s.url == "https://shop.example/uk"
    assert s.expect["text"] == ["£"]


@pytest.mark.parametrize("snippet, message", [
    ('[accounts.bad]\npassword = "hunter2"\n', "names, never values"),
    ('[env.prod]\nbase_url = "https://x"\nport = 1\n', "unknown key"),
    ('surprise = 1\n', "unknown key"),
])
def test_bad_configs_are_refused(tmp_path, snippet, message):
    path = tmp_path / "bad.toml"
    base = '[env.prod]\nbase_url = "https://x"\n' if "[env.prod]" not in snippet else ""
    path.write_text(base + snippet)
    with pytest.raises(P.ProjectError, match=message):
        P.load(path)


def test_unknown_env_tag_or_name_is_a_clear_error(repo):
    p = P.load(repo)
    with pytest.raises(P.ProjectError, match="no env"):
        P.suite_data(p, env="staging")
    with pytest.raises(P.ProjectError, match="tagged"):
        P.suite_data(p, tags=["nightly"])
    with pytest.raises(P.ProjectError, match="no objective named"):
        P.suite_data(p, names=["nope"])


def test_record_writes_findings_and_appends_to_the_index(repo, tmp_path):
    p = P.load(repo)
    run_dir = P.run_dir_parent(p) / "run1"
    run_dir.mkdir()
    report = {
        "run_dir": str(run_dir), "gate": "FAIL", "seconds": 12.5, "cost": {"usd": 0.004},
        "scenarios": [
            {"name": "price is visible", "outcome": "fail", "reason": "Jev reported DONE but: page shows 'per month'",
             "end_url": "https://shop.example/pricing", "page_says": "Pricing ...", "shot": "shots/p.jpg",
             "checks": [{"check": "page shows 'per month'", "ok": False, "detail": None}],
             "history": [{"step": 1, "kind": "click", "action": "Pricing", "url": "https://shop.example/"}],
             "findings": [{"severity": "S3", "kind": "console error", "detail": "x", "url": "u"}]},
            {"name": "faq explains refunds", "outcome": "harness", "reason": "time budget spent", "checks": []},
            {"name": "ok one", "outcome": "pass", "reason": "all passed", "checks": []},
        ],
    }
    row = P.record(p, report, env="prod")
    findings = json.loads((run_dir / "findings.json").read_text())
    assert [(f["objective"], f["kind"], f["severity"]) for f in findings] == [
        ("price is visible", "product", "S1"), ("price is visible", "product", "S3"),
        ("faq explains refunds", "harness", "note")]
    assert findings[0]["steps"] == ["1. click Pricing  (https://shop.example/)"]
    assert findings[0]["evidence"]["failed_checks"][0]["check"] == "page shows 'per month'"
    assert row["findings"] == {"product": 2, "harness": 1}
    P.record(p, {**report, "gate": "PASS"}, env="prod")
    rows = P.index_rows("shop")
    assert [r["gate"] for r in rows] == ["PASS", "FAIL"]  # newest first
    assert rows[0]["objectives"][0]["name"] == "price is visible"
    assert time.strftime("%Y-%m-%d") in rows[0]["when"]


def test_the_example_fixture_project_is_valid():
    from pathlib import Path

    p = P.load(Path(__file__).parent.parent / "examples" / "fixture-project")
    suite = parse(P.suite_data(p, tags=["core"]))
    assert [s.name for s in suite.scenarios] == ["a visitor finds the Pro price", "a visitor sends feedback"]
    assert all(s.mode == "mutate" for s in suite.scenarios)


def test_cli_projects_and_reports(repo, capsys, monkeypatch):
    from qajev.cli import main

    monkeypatch.setenv("QAJEV_PROJECTS", str(repo.parent / "none"))
    assert main(["reports", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["run", "--project", str(repo), "--suite", "nightly", "--json"]) == 3
    assert "tagged ['nightly']" in capsys.readouterr().out
    assert main(["run", "--project", str(repo), "--expect-text", "x", "--json"]) == 3
    assert "go with --objective" in capsys.readouterr().out


def test_known_issues_are_listed_not_raised(repo):
    p = P.load(repo)
    p.known = [{"kind": "HTTP 404", "url": "https://shop.example/", "note": "by design, decision pending"}]
    finding = {"severity": "S2", "kind": "HTTP 404", "detail": "document response", "scenario": "/",
               "url": "https://shop.example/"}
    other = {"severity": "S3", "kind": "HTTP 404", "detail": "x", "scenario": "/a", "url": "https://shop.example/a"}
    report = {"findings": [finding, other], "scenarios": [{"findings": [finding, other]}]}
    P.apply_known(p, report)
    assert report["findings"] == [other] and report["scenarios"][0]["findings"] == [other]
    assert report["known_findings"] == [{**finding, "known": "by design, decision pending"}]


def test_a_known_entry_needs_a_note(tmp_path):
    path = tmp_path / "p.toml"
    path.write_text('[env.prod]\nbase_url = "https://x"\n[[known]]\nkind = "HTTP 404"\n')
    with pytest.raises(P.ProjectError, match="needs kind and note"):
        P.load(path)


def test_inside_its_repo_a_project_is_found_by_its_name(tmp_path, monkeypatch):
    # Launch check: from inside a repo with .qajev/project.toml, `--project shop` said "no project 'shop'".
    (tmp_path / ".qajev").mkdir()
    (tmp_path / ".qajev" / "project.toml").write_text(TOML)
    (tmp_path / "src" / "deep").mkdir(parents=True)
    monkeypatch.setenv("QAJEV_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path / "src" / "deep")
    assert P.find("shop") == tmp_path / ".qajev" / "project.toml"
    with pytest.raises(P.ProjectError):
        P.find("another-shop")  # a different name is not this repo's project
