import json

import pytest

from qajev import changes


def scenario(name, outcome, reason="why"):
    return {"name": name, "outcome": outcome, "reason": reason}


def finding(scenario, kind, url, detail="d", severity="S2"):
    return {"scenario": scenario, "kind": kind, "url": url, "detail": detail, "severity": severity}


def report(gate, scenarios, findings=(), **extra):
    return {"gate": gate, "scenarios": scenarios, "findings": list(findings), "started_at": 1790000000.0, **extra}


def test_diff_names_what_broke_what_was_fixed_and_what_else_moved():
    prev = report("FAIL", [scenario("pricing", "pass"), scenario("docs", "fail"), scenario("faq", "stuck")],
                  [finding("docs", "page error", "https://a.example/docs?x=1", "TypeError at 1"),
                   finding("pricing", "HTTP 404", "https://a.example/logo.png")])
    now = report("FAIL", [scenario("pricing", "fail", "price missing"), scenario("docs", "pass"),
                          scenario("faq", "harness")],
                 [finding("docs", "page error", "https://a.example/docs?x=2", "TypeError at 2"),  # same, detail differs
                  finding("pricing", "blank or spinner over 10 s", "https://a.example/pricing")])
    d = changes.diff(prev, now)
    assert [(i["name"], i["was"], i["now"]) for i in d["newly_failing"]] == [("pricing", "pass", "fail")]
    assert d["newly_failing"][0]["reason"] == "price missing"
    assert [i["name"] for i in d["fixed"]] == ["docs"]
    assert [(i["name"], i["now"]) for i in d["other"]] == [("faq", "harness")]
    assert [f["kind"] for f in d["new_findings"]] == ["blank or spinner over 10 s"]
    assert [f["kind"] for f in d["gone_findings"]] == ["HTTP 404"]
    assert d["changed"] and d["summary"] == ("1 newly failing, 1 fixed, 1 other outcome change(s), 1 new finding(s), "
                                             "1 finding(s) gone")


def test_a_subset_run_is_compared_only_on_what_both_ran():
    prev = report("PASS", [scenario("a", "pass"), scenario("b", "pass")], [finding("b", "page error", "https://x/")])
    now = report("PASS", [scenario("a", "pass")])
    d = changes.diff(prev, now)
    assert not d["changed"] and d["summary"] == "no changes" and not d["gone_findings"]


def test_a_gate_flip_alone_counts_as_a_change():
    d = changes.diff(report("PASS", [scenario("a", "pass")]), report("INCOMPLETE", [scenario("a", "pass")]))
    assert d["changed"] and d["summary"] == "gate PASS -> INCOMPLETE"


def test_compare_uses_the_previous_finished_run_of_the_same_kind(tmp_path):
    def save(name, data):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "report.json").write_text(json.dumps({**data, "run_dir": str(folder)}))
        return {"project": "shop", "env": "prod", "report": str(folder / "report.md")}

    rows = [  # newest first, as index_rows returns them
        save("20260930-120000-shop-prod-interrupted", report("INCOMPLETE", [scenario("a", "harness")],
                                                             interrupted=True)),
        save("20260930-110000-smoke-shop.example", report("PASS", [scenario("/", "pass")], smoke={"pages": 1})),
        save("20260930-100000-shop-prod", report("PASS", [scenario("a", "pass")])),
    ]
    now = report("FAIL", [scenario("a", "fail")], run_dir=str(tmp_path / "now"))
    d = changes.compare("shop", "prod", now, rows)
    assert d["since"]["run_dir"].endswith("20260930-100000-shop-prod")  # not the interrupted one, not the smoke
    assert [i["name"] for i in d["newly_failing"]] == ["a"]
    assert changes.compare("shop", "local", now, rows) is None  # first run in that env: nothing to compare
    assert changes.compare("shop", "prod", {**now, "interrupted": True}, rows) is None


def test_nightly_notifies_only_when_something_changed(monkeypatch, tmp_path):
    from qajev import nightly

    monkeypatch.setattr(nightly, "NIGHTLY", tmp_path / "nightly")
    monkeypatch.setattr(nightly, "plan", lambda names, pages: [
        ("shop", "objectives", ["run", "--project", "shop"]), ("shop", "smoke", ["smoke", "--project", "shop"])])
    notes = []
    monkeypatch.setattr(nightly, "_notify", lambda digest, path: notes.append(nightly.headline(digest)))

    quiet = {"gate": "PASS", "run_dir": "/r/1", "cost": {"usd": 0.01},
             "changes": {"summary": "no changes", "changed": False}}
    monkeypatch.setattr(nightly, "_run", lambda argv: quiet)
    d = nightly.run(emit=lambda _: None)
    assert not d["changed"] and notes == [] and d["cost_usd"] == 0.02
    assert nightly.last()["entries"][0]["html"] == "/r/1/report.html"

    broke = {"gate": "FAIL", "run_dir": "/r/2", "cost": {"usd": 0.01},
             "changes": {"summary": "1 newly failing", "changed": True, "newly_failing": [{"name": "pricing"}]}}
    monkeypatch.setattr(nightly, "_run", lambda argv: broke if argv[0] == "run" else {"error": "Chrome did not start"})
    d = nightly.run(emit=lambda _: None)
    assert d["changed"] and d["entries"][0]["newly_failing"] == ["pricing"]
    assert notes == ["shop objectives: 1 newly failing; shop smoke: could not run"]
    md = (tmp_path / "nightly" / f"{d['date']}.md").read_text()
    assert "**Something changed**" in md and "could not run:** Chrome did not start" in md


def test_nightly_notifies_without_macos_tools(monkeypatch, tmp_path):
    # On Linux there is no osascript: the notification must not crash the run, and the user's hook still runs.
    from qajev import nightly

    monkeypatch.setattr(nightly.shutil, "which", lambda name: None)
    ran = []
    monkeypatch.setattr(nightly.subprocess, "run", lambda args, **kw: ran.append(args))
    monkeypatch.setenv("QAJEV_NOTIFY_CMD", "echo changed")
    nightly._notify({"entries": [{"project": "shop", "kind": "smoke", "changed": True, "error": None,
                                  "changes": "1 newly failing"}]}, tmp_path / "d.md")
    assert ran == ["echo changed"]


def test_nightly_install_needs_launchd(monkeypatch):
    from qajev import nightly

    monkeypatch.setattr(nightly.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="cron"):
        nightly.install("03:30")
