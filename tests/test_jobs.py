import json
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
emit(event="run", suite="demo", run_dir="/tmp/demo-run", scenarios=2)
emit(event="start", scenario="home")
emit(event="scenario", result={"name": "home", "outcome": "pass", "reason": "ok"})
emit(event="start", scenario="pricing")
emit(event="step", scenario="pricing", n=4, doing="click 'See pricing'", p=0.93, spent_usd=0.0035, at=time.time())
time.sleep(float(sys.argv[1]))
emit(event="scenario", result={"name": "pricing", "outcome": "fail", "reason": "no price"})
print(json.dumps({"gate": "FAIL", "run_dir": "/tmp/demo-run", "scenarios": []}))
"""


def fake(seconds):
    return [sys.executable, "-c", FAKE_RUN, str(seconds)]


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
    assert job["title"] == "check https://a.example"
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


def test_stopping_a_job_lets_the_run_finish_its_cleanup():
    job = jobs.start(["run", "suite.yaml"], command=fake(60))
    wait_for(lambda: jobs.status(job["id"])["progress"]["done"] == 1)
    stopped = jobs.stop(job["id"], wait=10)
    assert stopped["state"] == "stopped" and stopped["exit_code"] == 130  # the run handled SIGTERM itself
    assert stopped["gate"] == "INCOMPLETE"
    assert not jobs.alive(stopped["pid"])


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
    assert jobs.describe_argv(["run", "--project", "shop", "--suite", "core", "--json"]) == "run project shop"
    assert jobs.describe_argv(["check", "https://a.example", "--goal", "x"]) == "check https://a.example"
    assert jobs.describe_argv(["smoke", "--background", "https://b.example"]) == "smoke https://b.example"
    assert jobs.describe_argv(["play", "/g/mygame/godot", "--suite", "/q/suites/suho-long-play.yaml"]) == \
        "play mygame suho-long-play"
    assert jobs.describe_argv(["play", "/g/hypervolley", "--goal", "x"]) == "play hypervolley"


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


def test_a_foreground_run_is_a_job_too(capsys):
    code = main(["check", "http://127.0.0.1:9/", "--cdp-url", "http://127.0.0.1:9", "--expect-text", "x", "--quiet"])
    assert code == 3  # nothing listens there
    (job,) = [j for j in jobs.listing() if j["title"] == "check http://127.0.0.1:9/"]
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
