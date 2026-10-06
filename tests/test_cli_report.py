import json
import re
import subprocess
import sys
import time
from types import SimpleNamespace

from qajev import report
from qajev.cli import build_parser, check_suite, main
from qajev.config import redact_tree
from qajev.runner import groups, select
from qajev.suite import parse


def test_check_flags_become_a_one_scenario_suite():
    args = build_parser().parse_args([
        "check", "http://localhost:3000/", "-g", "Open pricing. Stop when prices show.", "-t", "Pro", "-t", "Team",
        "-a", "Error", "-u", "/pricing", "--fetch", "/api/health", "--fetch", "/api/me=401", "--device", "phone",
    ])
    s = check_suite(args)
    (sc,) = s.scenarios
    assert sc.goal.startswith("Open pricing") and sc.device["mobile"]
    assert sc.expect["text"] == ["Pro", "Team"] and sc.expect["absent"] == ["Error"] and sc.expect["url"] == "/pricing"
    assert sc.expect["fetch"] == [{"url": "/api/health", "status": 200}, {"url": "/api/me", "status": 401}]


def test_help_is_fast_and_does_not_import_the_browser_stack():
    code = ("import sys, time; t=time.perf_counter(); import qajev.cli as c; c.build_parser(); "
            "print(time.perf_counter()-t, 'jev_ultrafast' in sys.modules, 'mcp' in sys.modules, 'yaml' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.split()
    assert out[1:] == ["False", "False", "False"]  # the contract: no browser stack, MCP or YAML at startup
    assert float(out[0]) < 3.0  # a gross-regression guard only; measured 0.07 s idle, up to 1.7 s at load 500


def test_init_writes_a_template_and_refuses_to_overwrite(tmp_path, capsys):
    path = tmp_path / "suite.yaml"
    assert main(["init", str(path)]) == 0
    assert "base_url" in path.read_text()
    assert main(["init", str(path)]) == 3


def test_bad_suite_exits_3_with_a_json_error(tmp_path, capsys):
    path = tmp_path / "bad.yaml"
    path.write_text("scenarios: []\n")
    assert main(["run", str(path), "--json", "--env-file", str(tmp_path / "missing.env")]) == 3
    assert "env file not found" in capsys.readouterr().out
    path.write_text("scenarios:\n  - url: https://example.com/\n    goal: x\n    mode: mutate\n")
    env = tmp_path / ".env"
    env.write_text("")
    assert main(["run", str(path), "--json", "--env-file", str(env)]) == 3
    assert "loopback" in json.loads(capsys.readouterr().out.strip().splitlines()[-1])["error"]


def test_only_pulls_in_dependencies_and_groups_follow_chains():
    s = parse({"scenarios": [
        {"name": "a", "url": "http://h/", "goal": "x"},
        {"name": "b", "goal": "y"},
        {"name": "c", "url": "http://h/c", "expect": {"text": "c"}},
        {"name": "d", "url": "http://h/d", "goal": "z", "depends_on": "c"},
    ]})
    assert [x.name for x in select(s, ["b"])] == ["a", "b"]
    assert [[x.name for x in g] for g in groups(s.scenarios)] == [["a", "b"], ["c", "d"]]


def result(name, outcome, **extra):
    return {"name": name, "outcome": outcome, "reason": "why | pipes", "seconds": 1.5, "checks": [], "findings": [],
            "screens": [], "end_url": "http://h/p?token=secret", **extra}


def test_report_gate_counts_and_markdown(tmp_path):
    results = [
        result("home", "pass", screens=[{"step": 1, "next_step": "[1] Pricing", "p": 0.9, "runner_up": "[2] Docs",
                                         "runner_up_p": 0.05}]),
        result("buy", "fail", findings=[{"severity": "S2", "kind": "page error", "detail": "TypeError",
                                         "scenario": "buy", "url": "http://h/buy?code=123"}],
               blocked_writes=[{"method": "POST", "url": "http://h/api/order"}]),
    ]
    ledger = {"usd": 0.01, "usd_typesafe_estimated": 0.01, "usd_text": 0.0, "calls": {"typesafe": 20, "text": 1},
              "tokens": {"typesafe": 0, "text": 30}, "errors": 0, "text_cost_reported": False, "cap_usd": 1.0}
    data = report.build(SimpleNamespace(name="demo"), results, [ledger], browser={"cdp_url": "http://127.0.0.1:9350"},
                        started_at=time.time(), strict=False, interrupted=False, run_dir=tmp_path)
    assert data["gate"] == "FAIL" and data["exit_code"] == 1
    assert data["counts"]["pass"] == 1 and data["counts"]["fail"] == 1
    assert data["obvious_next_step"] == {"screens": 1, "obvious": 1}
    report.write(tmp_path, data)
    md = (tmp_path / "report.md").read_text()
    assert "**Gate: FAIL**" in md and "why \\| pipes" in md
    assert "token=secret" not in md and "code=123" not in md  # query strings never reach the Markdown
    assert "blocked 1 write request" in md
    assert json.loads((tmp_path / "report.json").read_text())["suite"] == "demo"


def test_every_report_also_writes_a_self_contained_html_page(tmp_path):
    results = [
        result("<img src=x onerror=alert(1)>", "fail", shot="shots/buy.jpg",
               findings=[{"severity": "S2", "kind": "page error", "detail": "<b>TypeError</b>", "scenario": "buy",
                          "url": "http://h/buy?code=123"}],
               checks=[{"check": "text 'Thanks' on screen", "ok": False, "detail": "not found"}]),
        result("home", "pass", url="javascript:alert(1)"),
    ]
    ledger = {"usd": 0.01, "usd_typesafe_estimated": 0.01, "usd_text": 0.0, "calls": {"typesafe": 2, "text": 0},
              "tokens": {"typesafe": 0, "text": 0}, "errors": 0, "text_cost_reported": True, "cap_usd": 1.0}
    data = report.build(SimpleNamespace(name="demo"), results, [ledger], browser={}, started_at=time.time(),
                        strict=False, interrupted=False, run_dir=tmp_path)
    report.write(tmp_path, data)
    page = (tmp_path / "report.html").read_text()
    assert page.startswith("<!doctype html>") and "Gate: FAIL" in page
    assert "<script" not in page and "<img src=x" not in page and "<b>TypeError" not in page  # page text is escaped
    assert "&lt;b&gt;TypeError&lt;/b&gt;" in page and "not found" in page
    assert "token=secret" not in page and "code=123" not in page  # no query strings, as in the Markdown
    assert 'href="http://h/p"' in page and 'href="javascript:' not in page  # only http(s) URLs become links
    assert 'src="shots/buy.jpg"' in page  # screenshots stay relative to the run folder
    assert not re.search(r'<link[^>]*href="(https?:)?//', page) and "http://fonts" not in page  # nothing from elsewhere
    assert '<link rel="icon" href="data:image/svg+xml;base64,' in page  # QAJev's mark rides inline


def test_report_html_rebuilds_the_page_from_a_finished_run(tmp_path, capsys):
    ledger = {"usd": 0.0, "usd_typesafe_estimated": 0.0, "usd_text": 0.0, "calls": {"typesafe": 0, "text": 0},
              "tokens": {"typesafe": 0, "text": 0}, "errors": 0, "text_cost_reported": True, "cap_usd": None}
    data = report.build(SimpleNamespace(name="old run"), [result("home", "pass")], [ledger], browser={},
                        started_at=time.time(), strict=False, interrupted=False, run_dir=tmp_path)
    (tmp_path / "report.json").write_text(json.dumps(data))
    assert main(["report", str(tmp_path), "--html"]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "report.html")
    assert "old run" in (tmp_path / "report.html").read_text()
    (tmp_path / "report.json").write_text(json.dumps({"partial": True, "scenarios": []}))
    assert main(["report", str(tmp_path), "--html"]) == 3  # a run still in progress has no final report


def test_secrets_from_the_environment_are_redacted(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-super-secret-value")
    out = redact_tree({"a": ["typed ts-super-secret-value here"], "b": 3})
    assert out == {"a": ["typed [redacted] here"], "b": 3}


def test_smoke_obeys_robots_txt_for_public_hosts(monkeypatch):
    import io
    import urllib.request

    from qajev import smoke

    robots_txt = b"User-agent: *\nDisallow: /private/\nCrawl-delay: 3\n"
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=10: io.BytesIO(robots_txt))
    robots = smoke.robots_for("https://public.example/")
    assert robots.can_fetch(smoke.USER_AGENT, "https://public.example/docs")
    assert not robots.can_fetch(smoke.USER_AGENT, "https://public.example/private/x")
    assert robots.crawl_delay(smoke.USER_AGENT) == 3
    assert smoke.robots_for("http://127.0.0.1:8765/") is None  # loopback: our own fixture, no robots


def test_a_pooled_worker_keeps_one_daemon_name(monkeypatch):
    import pytest

    from qajev import session

    monkeypatch.delenv("BU_NAME", raising=False)
    monkeypatch.setattr(session, "_jev", None)
    first = session.configure_env("http://127.0.0.1:9350")
    monkeypatch.setattr(session, "_jev", object())  # jev (and browser_harness) now imported with that name
    assert session.configure_env("http://127.0.0.1:9350") == first
    with pytest.raises(RuntimeError, match="another Chrome"):
        session.configure_env("http://127.0.0.1:9351")


def test_load_wait_is_one_budget_for_the_whole_run(monkeypatch):
    from qajev import runner

    clock = {"t": 0.0}
    monkeypatch.setattr(runner.os, "getloadavg", lambda: (300.0, 0, 0))
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(runner.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    opts = runner.Options(load_high=60, load_ok=50, load_wait=30)
    assert runner.wait_for_quiet(opts)[0] is False and opts.load_waited >= 30
    before = clock["t"]
    assert runner.wait_for_quiet(opts)[0] is False
    assert clock["t"] == before, "the second busy scenario must be skipped without waiting again"


def test_load_gate_defaults_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("QAJEV_LOAD_HIGH", "400")
    monkeypatch.setenv("QAJEV_LOAD_OK", "350")
    monkeypatch.delenv("QAJEV_LOAD_WAIT", raising=False)  # the caller's value must not leak in
    args = build_parser().parse_args(["smoke", "http://127.0.0.1:1/"])
    assert (args.load_high, args.load_ok, args.load_wait) == (400.0, 350.0, 600.0)


def test_blocked_after_stale_moves_is_a_harness_stop_not_stuck(monkeypatch):
    import time as real_time
    from types import SimpleNamespace

    from qajev import runner

    def run(decisions, history):
        state = {"status": "blocked", "history": history, "decisions": decisions}
        session = SimpleNamespace(agent=SimpleNamespace(state=state), observe=lambda: None,
                                  scroll_further=lambda: True, tick=lambda: state.update(status="blocked"))
        scenario = SimpleNamespace(budget={"actions": 20, "seconds": 60})
        return runner.drive(session, scenario, read=lambda: None, page_ok=lambda: False,
                            started=real_time.monotonic())

    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    # five moves decided, none executed: the page changed under every one of them
    stop, detail = run([{"operation": "SCROLL_DOWN"}] * 5 + [{"operation": "BLOCKED"}] * 3, [])
    assert stop == "stale" and "5 of Jev's moves went stale" in detail
    # moves that ran, then a genuine dead end
    decisions = [{"operation": "SCROLL_DOWN"}, {"operation": "CLICK"}, {"operation": "BLOCKED"}]
    assert run(decisions, [{"step": 1}, {"step": 2}])[0] == "blocked"


def test_each_move_jev_makes_is_told_as_it_happens():
    import time as real_time
    from types import SimpleNamespace

    from qajev import runner

    moves = iter([{"step": 1, "action": "See pricing", "kind": "click", "text": None, "probability": 0.93},
                  {"step": 2, "action": "Email", "kind": "fill", "text": "ana@example.com", "probability": 0.8}])
    state = {"status": "ready", "history": [], "decisions": []}

    def tick():
        move = next(moves, None)
        if move:
            state["decisions"].append({"operation": "CLICK"})
            state["history"].append(move)
        else:
            state["status"] = "done"

    session = SimpleNamespace(agent=SimpleNamespace(state=state), tick=tick)
    told = []
    stop, _ = runner.drive(session, SimpleNamespace(budget={"actions": 20, "seconds": 60}), read=lambda: None,
                           page_ok=lambda: False, started=real_time.monotonic(),
                           step=lambda doing, **extra: told.append((doing, extra.get("p"), extra.get("n"))))
    assert stop == "done"
    assert told == [("click 'See pricing'", 0.93, 1), ("type 'ana@example.com' into 'Email'", 0.8, 2),
                    ("said DONE", None, 3)]


def test_the_progress_printer_names_a_native_game(capsys):
    from qajev.cli import _screen_printer

    emit = _screen_printer(SimpleNamespace(events=False, quiet=False))
    emit({"event": "run", "suite": "play imhim", "run_dir": "/r", "browser": {"surface": "native", "engine":
                                                                                "electron", "project": "/g/ImHim.app"}})
    assert "(electron game /g/ImHim.app)" in capsys.readouterr().err


def test_smoke_and_play_wait_for_the_load_gate_too(monkeypatch, capsys):
    # Shared machine: smoke and play queued for the browser slot but then opened Chrome, an emulator or a game at
    # any load; only check and run waited for the load gate.
    from qajev import native, runner, smoke

    clock = {"t": 0.0}
    monkeypatch.setattr(runner.os, "getloadavg", lambda: (300.0, 0, 0))
    monkeypatch.setattr(runner.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(runner.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))

    def never(*a, **kw):
        raise AssertionError("started while the machine was busy")

    monkeypatch.setattr(smoke, "run", never)
    monkeypatch.setattr(native, "reap", never)
    for argv in (["smoke", "http://127.0.0.1:1/"], ["play", "android:com.example.app"]):
        assert main([*argv, "--load-wait", "30", "--json"]) == 4
        error = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["error"]
        assert "load" in error and "300" in error


def test_the_report_prices_the_decisions_by_the_model_that_made_them():
    from qajev import report_html

    jev = {"cost": {"usd_typesafe_estimated": 0.0035}, "models": {"decider": "Jev"}}
    clef = {"cost": {"usd_typesafe_estimated": 0.0004}, "models": {"decider": "Clef-flash"}}
    assert report_html.decisions_cost(jev) == "TypeSafe $0.0035 (estimated per call)"
    assert report_html.decisions_cost({"cost": jev["cost"]}) == "TypeSafe $0.0035 (estimated per call)"  # older run
    assert report_html.decisions_cost(clef) == "Clef-flash $0.0004"  # Clef's own token count, not an estimate
    assert [report_html.decider(d) for d in (jev, clef, {"cost": jev["cost"]})] == ["Jev", "Clef-flash", "Jev"]


def test_about_is_in_the_report_for_every_test_and_the_run(tmp_path):
    # José, 3 Oct: the agent says what each test proves and why; the person reads it next to the result.
    suite = parse({"name": "shop", "about": "the shop takes money", "devices": ["desktop"], "scenarios": [
        {"name": "buy", "url": "http://h/", "goal": "Buy.", "about": "a visitor can pay <for> a plan"},
        {"name": "home", "url": "http://h/", "expect": {"text": "Hi"}}]})
    results = [result("buy", "pass"), result("home", "skipped")]
    ledger = {"usd": 0.0, "usd_typesafe_estimated": 0.0, "usd_text": 0.0, "calls": {"typesafe": 0, "text": 0},
              "tokens": {"typesafe": 0, "text": 0}, "errors": 0, "text_cost_reported": True, "cap_usd": 1.0}
    data = report.build(suite, results, [ledger], browser={}, started_at=time.time(), strict=False,
                        interrupted=False, run_dir=tmp_path)
    assert data["about"] == "the shop takes money"
    assert [r.get("about") for r in data["scenarios"]] == ["a visitor can pay <for> a plan", None]
    report.write(tmp_path, data)
    page, md = (tmp_path / "report.html").read_text(), (tmp_path / "report.md").read_text()
    assert "a visitor can pay &lt;for&gt; a plan" in page and "the shop takes money" in page
    assert "About: a visitor can pay <for> a plan" in md and "the shop takes money" in md


def test_check_and_play_take_about():
    args = build_parser().parse_args(["check", "http://h/", "-t", "Pro", "--about", "the Pro plan is on sale"])
    (sc,) = check_suite(args).scenarios
    assert sc.about == "the Pro plan is on sale"
    assert build_parser().parse_args(["play", "g", "--about", "the menu opens"]).about == "the menu opens"


def test_the_report_carries_the_qajev_mark_without_loading_anything(tmp_path):
    from pathlib import Path

    ledger = {"usd": 0.0, "usd_typesafe_estimated": 0.0, "usd_text": 0.0, "calls": {"typesafe": 0, "text": 0},
              "tokens": {"typesafe": 0, "text": 0}, "errors": 0, "text_cost_reported": True, "cap_usd": 1.0}
    data = report.build(SimpleNamespace(name="demo"), [result("home", "pass")], [ledger], browser={},
                        started_at=time.time(), strict=False, interrupted=False, run_dir=tmp_path)
    report.write(tmp_path, data)
    page = (tmp_path / "report.html").read_text()
    mark = (Path(__file__).resolve().parents[1] / "site" / "favicon.svg").read_bytes()
    import base64

    inline = "data:image/svg+xml;base64," + base64.b64encode(mark).decode()
    assert f'<link rel="icon" href="{inline}">' in page and f'src="{inline}"' in page
    assert "http://" not in page.split("<main>")[0] and "https://" not in page.split("<main>")[0]


def test_the_docs_links_resolve():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for doc in [root / "README.md", *sorted((root / "docs").glob("*.md"))]:
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", doc.read_text()):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            assert (doc.parent / target).exists(), f"{doc.name} links to {target}, which does not exist"
    assert "dashboard.md" in (root / "docs" / "README.md").read_text()
    assert "docs/dashboard.md" in (root / "README.md").read_text()


def test_the_guards_own_hydration_warnings_are_a_note_in_both_reports(tmp_path):
    # Orchestrator, 4 Oct: React's hydration warning about the guard's attributes is QAJev's doing, never the page's.
    ledger = {"usd": 0.0, "usd_typesafe_estimated": 0.0, "usd_text": 0.0, "calls": {"typesafe": 0, "text": 0},
              "tokens": {"typesafe": 0, "text": 0}, "errors": 0, "text_cost_reported": True, "cap_usd": 1.0}
    result = {"name": "login", "url": "http://127.0.0.1:3100/login", "goal": None, "mode": "readonly",
              "outcome": "pass", "reason": "ok", "checks": [], "findings": [], "screens": [], "guard_hydration": 2,
              "guard_hidden": 1, "guard_hidden_controls": [{"label": "Shop <b>", "why": "danger", "match": "buy"}],
              "guard_hydration_details": [
                  "A tree hydrated but some attributes ... <input - data-qajev-guard=\"field\""],
              "seconds": 1.0}
    data = report.build(SimpleNamespace(name="demo"), [result], [ledger], browser={}, started_at=time.time(),
                        strict=False, interrupted=False, run_dir=tmp_path)
    report.write(tmp_path, data)
    note = ("React reported 2 hydration mismatch(es) on attributes the read-only guard set (QAJev's doing, not the "
            "page's)")
    md, html = (tmp_path / "report.md").read_text(), (tmp_path / "report.html").read_text()
    assert note in md and note in html
    assert "- Guard hid or disabled 1 control(s) Jev must not use: 'Shop <b>' (danger: buy)" in md
    assert "Guard hid or disabled 1 control(s) Jev must not use: &#x27;Shop &lt;b&gt;&#x27; (danger: buy)" in html
    # re-classed, never hidden: React's own text under the note (escaped: it is the page's text)
    assert '  - A tree hydrated but some attributes ... <input - data-qajev-guard="field"' in md
    assert "A tree hydrated but some attributes ... &lt;input - data-qajev-guard=&quot;field&quot;" in html
    assert not data["scenarios"][0]["findings"]


def test_the_tap_target_finding_names_up_to_ten_and_what_wcag_let_off():
    from qajev import smoke

    samples = [{"tag": "a", "text": "", "size": "20x18", "path": f"#footer a.icon{i}"} for i in range(10)]
    facts = {"small_targets": 44, "small_target_samples": samples, "small_targets_skipped": {"inline": 7, "spaced": 0}}
    found = [f for f in smoke.lint(facts, mobile=True, name="home") if f["kind"] == "tap targets under 24 px"]
    assert found and found[0]["detail"].startswith("44 target(s): a 20x18 (#footer a.icon0), a 20x18 (#footer a.icon1)")
    assert found[0]["detail"].endswith("a 20x18 (#footer a.icon9) and 34 more; not counted (WCAG 2.5.8): "
                                       "7 inline in a sentence")
    assert smoke.small_targets({"small_targets": 2}) == "2 target(s)"  # an older page_facts: the count alone
    exempt_only = {"small_targets": 0, "small_targets_skipped": {"inline": 9}}  # nothing counted: no finding
    assert all(f["kind"] != "tap targets under 24 px" for f in smoke.lint(exempt_only, mobile=True, name="home"))


def test_a_report_names_the_qajev_commit_that_judged_it(tmp_path, monkeypatch):
    # The audit (6 Oct) could not tell which rules judged a run: every report before then said only a version.
    from qajev import config

    monkeypatch.setattr(config, "qajev_commit", lambda: "0123456789ab+dirty")
    ledger = {"usd": 0.0, "usd_typesafe_estimated": 0.0, "usd_text": 0.0, "calls": {"typesafe": 0, "text": 0},
              "tokens": {"typesafe": 0, "text": 0}, "errors": 0, "text_cost_reported": True, "cap_usd": 1.0}
    data = report.build(SimpleNamespace(name="demo"), [result("home", "pass")], [ledger], browser={},
                        started_at=time.time(), strict=False, interrupted=False, run_dir=tmp_path)
    assert data["qajev_commit"] == "0123456789ab+dirty"
    report.write(tmp_path, data)
    assert "0123456789ab+dirty" in (tmp_path / "report.md").read_text()
    assert "0123456789ab+dirty" in (tmp_path / "report.html").read_text()


def test_the_qajev_commit_comes_from_its_own_checkout_and_says_when_it_is_changed(tmp_path):
    import subprocess

    from qajev import config

    assert config.qajev_commit(tmp_path) is None  # a wheel: no checkout, no commit
    (tmp_path / "qajev").mkdir()
    (tmp_path / "qajev" / "rules.py").write_text("A = 1\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-C", str(tmp_path)]
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "x"]):
        subprocess.run(git + args, check=True)
    head = subprocess.run(git + ["rev-parse", "--short=12", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert config.qajev_commit(tmp_path) == head
    (tmp_path / "qajev" / "rules.py").write_text("A = 2\n")  # judged by rules no commit holds
    assert config.qajev_commit(tmp_path) == head + "+dirty"
