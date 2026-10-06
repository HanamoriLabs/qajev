import json
import os
import re
import sys
import time

import pytest

from qajev import jobs
from qajev.cli import main

# Stands in for `qajev ... --json --events`: progress on stderr, the report JSON on stdout, SIGTERM like Ctrl-C.
FAKE_RUN = r"""
import json, signal, sys, time
emit = lambda **e: print(json.dumps(e), file=sys.stderr, flush=True)
def stop(*_):
    print(json.dumps({"gate": "INCOMPLETE", "interrupted": True, "scenarios": []})); sys.exit(130)
signal.signal(signal.SIGTERM, stop)
emit(event="run", suite="demo", run_dir="/tmp/demo-run", scenarios=2,
     **({"decider": sys.argv[2]} if len(sys.argv) > 2 else {}))
emit(event="start", scenario="home")
emit(event="scenario", result={"name": "home", "outcome": "pass", "reason": "ok"})
emit(event="start", scenario="pricing")
emit(event="decision", scenario="pricing", at=time.time() - 1, screen="Home", chose="BLOCKED", p=0.51,
     runner_up="CLICK", runner_up_p=0.4, options=5, ms=600)
emit(event="decision", scenario="pricing", at=time.time(), screen="Home", chose="See pricing", p=0.93,
     runner_up="Contact us", runner_up_p=0.05, options=4, ms=280)
emit(event="step", scenario="pricing", n=4, doing="click 'See pricing'", p=0.93, spent_usd=0.0035, at=time.time())
time.sleep(float(sys.argv[1]))
emit(event="scenario", result={"name": "pricing", "outcome": "fail", "reason": "no price"})
print(json.dumps({"gate": "FAIL", "run_dir": "/tmp/demo-run", "scenarios": []}))
"""


def fake(seconds, decider=None):
    return [sys.executable, "-c", FAKE_RUN, str(seconds), *([decider] if decider else [])]


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("condition not met in time")


def test_second_run_queues_behind_the_first_and_names_it():
    with jobs.machine_lock("check https://a.example"):
        h = jobs.holder()
        assert h["what"] == "check https://a.example" and h["pid"]
        events = []
        with pytest.raises(jobs.Busy, match="check https://a.example"):
            with jobs.machine_lock("smoke https://b.example", emit=events.append, wait=1.0):
                pass
        assert events and events[0]["event"] == "waiting" and events[0]["queued"]
        assert "check https://a.example" in events[0]["reason"]
    assert jobs.holder() is None  # released on exit
    with jobs.machine_lock("smoke https://b.example", wait=0.5):
        assert jobs.holder()["what"] == "smoke https://b.example"


def test_a_job_reports_progress_while_running_and_its_gate_when_done():
    job = jobs.start(["check", "https://a.example"], command=fake(1.5))
    assert job["title"] == "check a.example"
    running = wait_for(lambda: (s := jobs.status(job["id"], detail=True))["progress"]["done"] == 1 and s)
    assert running["state"] == "running" and running["current"] == "pricing"
    assert running["run_dir"] == "/tmp/demo-run" and running["progress"]["total"] == 2
    assert [r["name"] for r in running["scenarios"]] == ["home"]  # partial results while it runs
    assert running["now"]["doing"] == "click 'See pricing'" and running["now"]["scenario"] == "pricing"
    assert running["cost_usd"] == 0.0035
    done = wait_for(lambda: (s := jobs.status(job["id"], detail=True))["state"] == "done" and s)
    assert done["gate"] == "FAIL" and done["exit_code"] == 0 and done["progress"]["done"] == 2
    assert done["report"]["gate"] == "FAIL"
    assert [j["id"] for j in jobs.listing()][0] == job["id"]


def test_a_jobs_decisions_are_shown_live_newest_first_with_how_sure_and_the_runner_up():
    # José, 3 Oct: "a way to see in real time the decisions that Jev or Clef are taking" (qajev top, d).
    from qajev import top

    job = jobs.start(["check", "https://b.example"], command=fake(1.5, "Clef"))
    seen = wait_for(lambda: len(jobs.decisions(job["id"])) == 2 and jobs.decisions(job["id"]))
    assert [d["chose"] for d in seen] == ["BLOCKED", "See pricing"]  # oldest first, as made
    lines = top.render_decisions(jobs.status(job["id"]), seen, width=140)
    assert lines[0][0].startswith("Decisions · check b.example · Clef · running")
    assert "2 decision(s) · median p 0.93 · 0 under 0.6 · 1 BLOCKED · median 600 ms" in lines[1][0]
    # a decision row starts with its clock time (by pattern: a run made at 08:59:59 is read at 09:00:00)
    rows = [(text, style) for text, style in lines if re.match(r"\d\d:\d\d:\d\d ", text)]
    assert "See pricing" in rows[0][0] and "Contact us" in rows[0][0] and rows[0][1] == "pass"  # newest first
    assert all(text.rstrip().endswith(ms) for (text, _), ms in zip(rows, ("280", "600")))  # nothing cut off
    assert all(len(text) <= 140 for text, _ in lines)
    assert "BLOCKED" in rows[1][0] and rows[1][1] == "harness"
    assert "See pricing" not in top.render_decisions(jobs.status(job["id"]), seen, width=140, scroll=1)[5][0]
    assert main(["top", "--decisions", job["id"], "--once"]) == 0
    assert main(["top", "--decisions", "no-such-job", "--once"]) == 3
    wait_for(lambda: jobs.status(job["id"])["state"] == "done")


FINISHED = r"""
import json, sys
print(json.dumps({"gate": "FAIL", "run_dir": "/tmp/r", "scenarios": [
    {"name": "home", "outcome": "pass"}, {"name": "pricing", "outcome": "fail"},
    {"name": "pricing (phone)", "outcome": "stuck"}, {"name": "docs", "outcome": "harness"},
    {"name": "blog", "outcome": "unverified"}, {"name": "faq", "outcome": "skipped"}][:int(sys.argv[1])]}))
"""


def test_a_finished_job_reruns_as_it_was_or_only_its_tests_that_did_not_pass(monkeypatch):
    # José, 3 Oct: "it should be possible to run specific tests" (qajev rerun JOB --failed, MCP qa_rerun).
    from qajev import cli

    def finished(argv, results):
        job = jobs.start(argv, command=[sys.executable, "-c", FINISHED, str(results)])
        return wait_for(lambda: jobs.status(job["id"])["state"] == "done" and job["id"])

    run = finished(["run", "suite.yaml", "--only", "home"], 6)
    assert jobs.rerun_argv(run) == (["run", "suite.yaml", "--only", "home"], "all of it")
    argv, what = jobs.rerun_argv(run, failed=True)  # fail, stuck and harness; its own --only replaced
    assert argv == ["run", "suite.yaml", "--only", "pricing", "--only", "pricing (phone)", "--only", "docs"]
    assert what == "its 3 test(s) that did not pass"
    check = finished(["check", "https://c.example", "--goal", "x"], 2)
    assert jobs.rerun_argv(check, failed=True)[0] == ["check", "https://c.example", "--goal", "x"]
    with pytest.raises(jobs.NothingToRerun):
        jobs.rerun_argv(finished(["play", "g", "--suite", "s.yaml"], 1), failed=True)
    with pytest.raises(ValueError, match="only check, run and play"):
        jobs.rerun_argv(finished(["smoke", "https://d.example"], 1))

    # A job id is QAJev's own folder name, never a path: a rerun replays the command recorded there.
    outside = jobs.JOBS.parent / "elsewhere"
    (outside / "x").mkdir(parents=True, exist_ok=True)
    (outside / "x" / "job.json").write_text(json.dumps({"argv": ["run", "evil.yaml", "--allow-commands"], "pid": 1,
                                                         "started_at": time.time()}))
    for bad in ("../elsewhere/x", "/tmp", "20261003-055116-afb3/../../elsewhere/x", ""):
        with pytest.raises(jobs.NoSuchJob):
            jobs.rerun_argv(bad)

    # SideGame1, 3 Oct: in `qajev jobs` a rerun looked just like the job it reran.
    rerun = jobs.start(argv, command=[sys.executable, "-c", FINISHED, "2"], rerun={"of": run, "failed": True})
    assert jobs.status(rerun["id"])["title"] == f"{jobs.status(run)['title']} · rerun of {run}, failed only"
    started = []
    monkeypatch.setattr(jobs, "start", lambda argv, title=None, **k: started.append((argv, k)) or
                        {"id": "x", "title": "t"})
    assert cli.cmd_rerun(cli.build_parser().parse_args(["rerun", run, "--failed", "--background", "--quiet"])) == 0
    assert started == [([*argv, "--quiet"], {"rerun": {"of": run, "failed": True}})]  # a job of its own
    assert cli._rerun is None  # a later run in this process is no rerun


def test_liveness_needs_no_ps_and_an_exited_unreaped_child_is_not_alive(monkeypatch):
    # José, 3 Oct: `qajev top` "incredibly slow": one `ps` per job, every second, on a machine at load 56.
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    time.sleep(0.5)  # exited, not reaped: a zombie, which kill(pid, 0) still finds
    started = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: started.append(a) or real(*a, **k))
    assert jobs.alive(os.getpid()) and not jobs.alive(child.pid)
    assert started == []
    child.wait()


def test_a_finished_job_is_read_once_until_its_files_change(monkeypatch):
    job = jobs.start(["run", "suite.yaml"], command=[sys.executable, "-c", FINISHED, "2"])
    wait_for(lambda: jobs.status(job["id"])["state"] == "done")
    reads = []
    real = jobs._status
    monkeypatch.setattr(jobs, "_status", lambda *a: reads.append(a) or real(*a))
    first = jobs.status(job["id"])
    first["title"] = "changed by a caller"  # a caller's change is its own, nested ones too
    first["progress"]["outcomes"]["pass"] = 99
    again = jobs.status(job["id"])
    assert again["title"] != "changed by a caller" and again["progress"]["outcomes"].get("pass") != 99 and reads == []
    folder = jobs.JOBS / job["id"]
    meta = json.loads((folder / "job.json").read_text())
    (folder / "job.json").write_text(json.dumps({**meta, "title": "renamed", "auto_title": False}))
    assert jobs.status(job["id"])["title"] == "renamed" and len(reads) == 1


def test_a_rerun_runs_in_the_folder_its_job_ran_in(tmp_path, monkeypatch):
    # Live, 3 Oct: `qajev play tests/fixtures/godot_game ...` rerun from another folder found no Godot project there.
    from qajev import cli

    project, elsewhere = tmp_path / "project", tmp_path / "elsewhere"
    project.mkdir()
    elsewhere.mkdir()
    job = jobs.start(["play", "games/g", "--suite", "s.yaml"], command=[sys.executable, "-c", FINISHED, "2"],
                     cwd=str(project))
    run = wait_for(lambda: jobs.status(job["id"])["state"] == "done" and job["id"])
    assert jobs.rerun_cwd(run) == str(project)
    monkeypatch.chdir(elsewhere)
    ran = []
    monkeypatch.setattr(cli, "main", lambda argv: ran.append(os.getcwd()) or 0)
    assert cli.cmd_rerun(cli.build_parser().parse_args(["rerun", run, "--failed", "--quiet"])) == 0
    assert ran == [os.path.realpath(project)]
    project.rmdir()  # its relative paths mean nothing anywhere else
    with pytest.raises(ValueError, match="no longer exists"):
        jobs.rerun_cwd(run)
    assert cli.cmd_rerun(cli.build_parser().parse_args(["rerun", run, "--quiet"])) == 3


def test_stopping_a_job_lets_the_run_finish_its_cleanup():
    job = jobs.start(["run", "suite.yaml"], command=fake(60))
    wait_for(lambda: jobs.status(job["id"])["progress"]["done"] == 1)
    stopped = jobs.stop(job["id"], wait=10)
    assert stopped["state"] == "stopped" and stopped["exit_code"] == 130  # the run handled SIGTERM itself
    assert stopped["gate"] == "INCOMPLETE"
    assert not jobs.alive(stopped["pid"])


def test_stopping_a_job_never_signals_a_process_that_took_its_pid():
    # The Orchestrator, 6 Oct: a job's recorded pid can belong to another process by the time someone stops it.
    import subprocess

    job = jobs.start(["run", "suite.yaml"], command=fake(60))
    wait_for(lambda: jobs.status(job["id"])["progress"]["done"] == 1)
    path = jobs.JOBS / job["id"] / "job.json"
    meta = json.loads(path.read_text())
    stranger = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        path.write_text(json.dumps({**meta, "pid": stranger.pid}))  # the recorded pid now names someone else
        jobs.stop(job["id"], wait=2)
        assert stranger.poll() is None  # not signalled: its start and command are not the job's
    finally:
        stranger.kill()
        stranger.wait()
        path.write_text(json.dumps(meta))
        jobs.stop(job["id"], wait=10)


def test_a_record_from_before_identities_is_never_signalled():
    # 0.4.0 review: as the reaper does, a stop acts only on a process it can prove is the job's. A record written
    # before QAJev kept identities cannot prove it, so the stop refuses and says how to stop it by hand.
    job = jobs.start(["run", "suite.yaml"], command=fake(60))
    wait_for(lambda: jobs.status(job["id"])["progress"]["done"] == 1)
    path = jobs.JOBS / job["id"] / "job.json"
    meta = json.loads(path.read_text())
    try:
        path.write_text(json.dumps({k: v for k, v in meta.items() if k != "identity"}))
        with pytest.raises(jobs.NotOurs, match=f"kill {meta['pid']}"):
            jobs.stop(job["id"], wait=2)
        assert jobs.alive(meta["pid"]) and jobs.status(job["id"])["state"] == "running"
        assert main(["stop", job["id"]]) == 3
    finally:
        path.write_text(json.dumps(meta))
        jobs.stop(job["id"], wait=10)


def test_a_job_whose_command_fails_is_failed_with_its_error():
    job = jobs.start(["smoke", "x"], command=[sys.executable, "-c", "import sys; print('boom', file=sys.stderr); "
                                                                     "sys.exit(3)"])
    st = wait_for(lambda: (s := jobs.status(job["id"]))["state"] != "running" and s)
    assert st["state"] == "failed" and st["exit_code"] == 3 and "boom" in st["error"]


def test_cli_jobs_and_stop(capsys):
    job = jobs.start(["check", "https://a.example"], command=fake(60))
    wait_for(lambda: jobs.status(job["id"])["state"] == "running")
    assert main(["jobs", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert job["id"] in [j["id"] for j in listed["jobs"]]
    assert main(["stop", job["id"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "stopped"
    assert main(["stop", "no-such-job"]) == 3


def test_describe_argv():
    assert jobs.describe_argv(["run", "--project", "shop", "--suite", "core", "--json"]) == "run project shop · core"
    assert jobs.describe_argv(["check", "https://a.example", "--goal", "x"]) == "check a.example · x"
    assert jobs.describe_argv(["smoke", "--background", "https://b.example"]) == "smoke b.example"
    assert jobs.describe_argv(["play", "/g/mygame/godot", "--suite", "/q/suites/suho-long-play.yaml"]) == \
        "play mygame · suho-long-play"
    assert jobs.describe_argv(["play", "/g/hypervolley", "--goal", "x"]) == "play hypervolley"


def test_a_job_always_says_which_game_or_site(monkeypatch, tmp_path):
    # qajev top listed "play game", "play desktop" and "check http://127.0.0.1:8765/": folders named after their role,
    # not the game, and no word on which site or what the check was for.
    from qajev import project

    monkeypatch.setattr(project, "listing", lambda: [{"name": "foley", "envs": {"prod": "https://foleyapp.com"}}])
    jobs._projects.cache_clear()  # cached per minute; another test may have filled it
    goal = "Find what the Pro plan costs per month. Stop when that price is visible."
    assert jobs.describe_argv(["check", "https://foleyapp.com/pricing", "--goal", goal]) == \
        "check foley /pricing · Find what the Pro plan costs per month"
    assert jobs.describe_argv(["check", "http://127.0.0.1:8765/", "--goal", goal]) == \
        "check 127.0.0.1:8765 · Find what the Pro plan costs per month"
    assert jobs.describe_argv(["smoke", "https://foleyapp.com/"]) == "smoke foley"
    # a game: its adapter or a real folder name, never "desktop", "game" or a worktree; then what the test is
    wt = "/r/games/sidescroller/.claude/worktrees/steam-de44e47/desktop"
    assert jobs.describe_argv(["play", wt, "--adapter", "imhim"]) == "play imhim"
    assert jobs.describe_argv(["play", wt]) == "play sidescroller"
    assert jobs.describe_argv(["play", "/r/suho/game"]) == "play suho"
    assert jobs.describe_argv(["play", "/r/out/ImHim-darwin-arm64/ImHim.app", "--name", "settings rehearsal"]) == \
        "play ImHim · settings rehearsal"
    suite = tmp_path / "quit-suite.yaml"
    suite.write_text("name: quit sends session_end\nadapter: imhim\nsteps: []\n")
    assert jobs.describe_argv(["play", wt, "--suite", str(suite)]) == "play imhim · quit sends session_end"
    assert jobs.describe_argv(["play", "/r/games/sevendawns/godot", "--adapter", "/r/qa/qajev_adapter.gd",
                               "--name", "Seven Dawns opening"]) == "play Seven Dawns opening"  # its own name
    assert jobs.describe_argv(["play", "android:com.example.app", "--name", "onboarding"]) == \
        "play android:com.example.app · onboarding"


def test_top_shows_the_browser_holder_queue_progress_and_reports():
    from qajev import top

    running = jobs.start(["run", "--project", "shop"], command=fake(60))
    wait_for(lambda: jobs.status(running["id"])["progress"]["done"] == 1)
    with jobs.machine_lock("run project shop"):
        snap = top.snapshot()
        text = top.plain(snap)
    assert "browser: run project shop" in text
    row = next(line for line in text.splitlines() if "run project shop" in line and "1/2" in line)
    assert "running" in row and "▸ pricing" in row  # progress and the scenario in flight
    assert "$0.0035" in row  # money spent so far, while it runs
    now = text.splitlines()[text.splitlines().index(row) + 1]
    assert "Jev ▸ click 'See pricing' (p 0.93) · step 4" in now  # what Jev is doing right now
    lines = top.render(snap, selected=0, detail=("job", running["id"]))
    assert [text for text, style in lines if style == "sel"] == [next(t for t, _ in lines if "1/2" in t)]
    assert any(style == "pass" and "home" in text for text, style in lines)  # finished scenarios, in detail
    jobs.stop(running["id"], wait=10)
    after = top.plain(top.snapshot())
    assert "browser: free" in after and "stopped" in after


def test_top_names_the_model_making_the_decisions():
    # Jev or Clef: with a choice of decision models, the live line says which one is deciding.
    from qajev import top

    running = jobs.start(["run", "--project", "shop"], command=fake(60, decider="Clef-flash"))
    wait_for(lambda: jobs.status(running["id"])["progress"]["done"] == 1)
    assert jobs.status(running["id"])["decider"] == "Clef-flash"
    text = top.plain(top.snapshot())
    assert "Clef-flash ▸ click 'See pricing' (p 0.93) · step 4" in text and "Jev ▸" not in text
    jobs.stop(running["id"], wait=10)


def test_top_sets_a_live_chrome_or_game_apart_from_the_empty_placeholders():
    # Live, a running Chrome was the same dim grey as "no game running" beside it: it read as nothing running.
    from qajev import top

    snap = {"holder": None, "jobs": [], "recent": [], "load": 1.0, "at": time.time(),
            "today": {"runs": 0, "gates": {"PASS": 0, "FAIL": 0, "INCOMPLETE": 0}, "cost_usd": 0}}
    idle = dict(top.render({**snap, "browsers": [], "native": []}))
    assert idle["  none running"] == "dim" and idle["  no game running (qajev play)"] == "dim"
    busy = top.render({**snap, "browsers": [
        {"name": "default.ephemeral-18362", "port": 9350, "headless": True, "tabs": 3, "pid": 28454, "age_s": 15,
         "alive": True},
        {"name": "old", "port": 9351, "headless": False, "tabs": None, "pid": 1, "age_s": None, "alive": False},
    ], "native": [{"name": "imhim", "engine": "electron", "adapter": "imhim", "pid": 7, "port": 9400,
                   "headless": True, "age_s": 30}]})
    chrome = next((t, s) for t, s in busy if "default.ephemeral-18362" in t)
    game = next((t, s) for t, s in busy if "imhim" in t and "electron" in t)
    dead = next((t, s) for t, s in busy if "old" in t and "9351" in t)
    for text, style in (chrome, game):
        assert style == "live" and text.startswith("  ● ")  # colour, and a mark for terminals without it
    assert dead[1] == "fail" and "●" not in dead[0]


def test_a_finished_jobs_duration_stops_growing():
    job = jobs.start(["check", "https://a.example"], command=fake(0.2))
    first = wait_for(lambda: (s := jobs.status(job["id"]))["state"] == "done" and s)["seconds"]
    time.sleep(1.2)
    assert jobs.status(job["id"])["seconds"] == first


def test_top_details_say_why_a_scenario_failed():
    from qajev import top

    lines = []
    top.explain([
        {"name": "privacy", "outcome": "stuck", "reason": "Jev found no way forward; unmet: url contains '/privacy' "
                                                          "(https://shop.example/)", "end_url": "https://shop.example/?t=1",
         "checks": [{"check": "url contains '/privacy'", "ok": False, "detail": "https://shop.example/"}],
         "findings": [{"severity": "S2", "kind": "blank or spinner over 10 s", "detail": f"{n} ms",
                       "url": "https://shop.example/"} for n in (10100, 11200, 12300)],
         "page_says": "Home Pricing", "seconds": 16.2, "jev": {"actions": 3}, "cost_usd": 0.0015},
        {"name": "pricing", "outcome": "pass", "reason": "all 1 check(s) passed",
         "checks": [{"check": "page shows '$9'", "ok": True}], "findings": []},
    ], lines.append, 120)
    text = "\n".join(t for t, _ in lines)
    assert "why: Jev found no way forward; unmet: url contains '/privacy'" in text
    assert "(https://shop.example/)" not in text.split("why:")[1].splitlines()[0]  # the excerpt is shown once
    assert "✗ url contains '/privacy' — found: https://shop.example/" in text
    assert "S2 blank or spinner over 10 s ×3: 12300 ms (shop.example/)" in text  # repeats collapse
    assert "ended at: shop.example/" in text and "?t=1" not in text  # no query strings
    assert "page said: Home Pricing" in text and "16.2 s · 3 Jev action(s) · $0.0015" in text
    assert [t for t, s in lines if s == "pass"] == ["    pass       pricing  (1 check(s) passed)"]


UX_NOTES = [{"basis": "measured", "kind": "low text contrast", "detail": "2 of 40 text(s) below WCAG AA"},
            {"basis": "opinion", "kind": "primary action unclear", "detail": "Clef answered no"},
            {"basis": "measured", "kind": "no visible focus", "detail": "3 control(s)"}]


def test_top_counts_a_tests_ux_notes_by_what_they_rest_on():
    # José, 5 Oct: the UX notes were only in the reports, not in qajev top.
    from qajev import top

    lines = []
    top.explain([{"name": "/", "outcome": "pass", "checks": [], "findings": [], "ux": UX_NOTES},
                 {"name": "/about", "outcome": "fail", "reason": "HTTP 500", "ux": UX_NOTES[:1]},
                 {"name": "/blog", "outcome": "pass", "checks": [], "findings": []}], lines.append, 120)
    ux = [t.strip() for t, _ in lines if "UX" in t]
    assert ux == ["UX 2 measured · 1 model opinion", "UX 1 measured"]  # the report's words; /blog has none


def test_top_shows_a_runs_ux_count_from_its_report():
    from qajev import top

    report = {"gate": "PASS", "run_dir": "/tmp/smoke-ux-run", "counts": {"pass": 2},
              "scenarios": [{"name": "/", "outcome": "pass", "ux": UX_NOTES}, {"name": "/about", "outcome": "pass"}],
              "ux": {"consistency": {"desktop": [{"basis": "measured", "kind": "h1 style differs", "detail": "x"}]}}}
    events = "".join(f"print({json.dumps({'event': 'scenario', 'result': s})!r}, file=sys.stderr)\n"
                     for s in report["scenarios"])
    job = jobs.start(["smoke", "https://shop.example/"],
                     command=[sys.executable, "-c", f"import sys\n{events}print({json.dumps(report)!r})"])
    wait_for(lambda: jobs.status(job["id"])["state"] == "done")
    snap = top.snapshot()
    recent = next(r for r in snap["recent"] if r.get("job") == job["id"])
    assert recent["ux"] == {"measured": 3, "opinion": 1}  # every test's notes, and the run's design consistency
    text = top.plain(snap, width=200)
    rows = [line for line in text.splitlines() if "smoke" in line and "UX 3 measured · 1 model opinion" in line]
    assert len(rows) == 2  # the job's row and its report's row
    detail = [t.strip() for t, _ in top.render(snap, width=200, detail=("job", job["id"]))]
    assert "design consistency (desktop): 1 difference(s): h1 style differs" in detail
    assert "UX 2 measured · 1 model opinion" in detail  # the test's own notes, under it
    plain ={**report, "scenarios": [{"name": "/", "outcome": "pass"}], "ux": {}}
    assert "UX" not in top.plain({**snap, "jobs": [], "recent": [{**recent, "ux": top.ux_counts(plain)}]})


def test_a_foreground_run_is_a_job_too(capsys):
    code = main(["check", "http://127.0.0.1:9/", "--cdp-url", "http://127.0.0.1:9", "--expect-text", "x", "--quiet"])
    assert code == 3  # nothing listens there
    (job,) = [j for j in jobs.listing() if j["title"] == "check 127.0.0.1:9"]
    assert job["state"] == "failed" and job["exit_code"] == 3 and "no Chrome DevTools endpoint" in job["error"]
    meta = json.loads((jobs.JOBS / job["id"] / "job.json").read_text())
    assert meta["foreground"] and meta["pid"] == __import__("os").getpid()


def test_top_shows_what_changed_for_a_run_that_was_a_job():
    from qajev import top

    report = {"gate": "FAIL", "run_dir": "/tmp/shop-run", "counts": {"fail": 1},
              "changes": {"changed": True, "summary": "1 newly failing"}}
    job = jobs.start(["run", "--project", "shop"], command=[sys.executable, "-c", f"print({json.dumps(report)!r})"])
    wait_for(lambda: jobs.status(job["id"])["state"] == "done")
    recent = next(r for r in top.snapshot()["recent"] if r.get("job") == job["id"])
    assert recent["changes"]["summary"] == "1 newly failing"  # the report's own comparison, shown as a Δ


def test_a_run_that_reports_through_its_events_is_done_not_failed():
    # Launch QA: a harness on QAJev's library registered itself, ended with a "done" event and exit 0, and wrote no
    # result.json; `qajev jobs` called it failed.
    def job(code, *events):
        folder = jobs.register(["play", "lib"], title="lib run")
        with open(folder / "events.jsonl", "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
        (folder / "exit_code").write_text(str(code))
        return jobs.status(folder.name)

    reported = job(0, {"event": "run", "suite": "lib", "run_dir": "/tmp/lib-run", "scenarios": 1},
                   {"event": "start", "scenario": "zoom"},
                   {"event": "scenario", "result": {"name": "zoom", "outcome": "pass", "reason": "ok"}},
                   {"event": "done", "gate": "PASS", "run_dir": "/tmp/lib-run"})
    assert reported["state"] == "done" and reported["gate"] == "PASS" and reported["run_dir"] == "/tmp/lib-run"
    silent = job(0)
    assert silent["state"] == "done" and silent["gate"] == "INCOMPLETE"  # exit 0 is never "failed"
    assert job(1)["state"] == "failed"
    early = job(130)  # stopped while it was still starting: nothing judged, nothing failed
    assert early["gate"] == "INCOMPLETE" and early["error"] == "stopped before a report"
