import http.client
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from qajev import dashboard, jobs

# Stands in for a game session run: its run folder (relative to where it ran) with a report and a screenshot, a
# decision, and the report JSON on stdout.
FAKE_PLAY = r"""
import json, pathlib, sys, time
emit = lambda **e: print(json.dumps(e), file=sys.stderr, flush=True)
run = pathlib.Path("runs/boss-run"); (run / "shots").mkdir(parents=True, exist_ok=True)
(run / "shots" / "kira.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")
(run / "notes.py").write_text("print('not a run file')")
emit(event="run", suite="play imhim", run_dir=str(run), scenarios=2)
emit(event="start", scenario="kira: beaten")
emit(event="decision", scenario="kira: beaten", at=time.time(), screen="LEVEL UP", chose="Spiral Orb", p=0.55,
     runner_up="Shield", runner_up_p=0.4, options=3, ms=310)
steps = [{"name": "kira: beaten", "outcome": "pass", "reason": "ok", "shot": "shots/kira.jpg",
          "about": "Kira's fight can be won at level 18",
          "checks": [{"check": "boss gone", "ok": True, "detail": None}]},
         {"name": "kira: on the ledger", "outcome": "fail", "reason": "not on file", "shot": None}]
for s in steps:
    emit(event="scenario", result=s)
report = {"gate": "FAIL", "run_dir": str(run), "scenarios": steps, "about": "every boss can be beaten"}
(run / "report.json").write_text(json.dumps(report))
(run / "report.html").write_text("<p>report</p>")
print(json.dumps(report))
"""


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("condition not met in time")


@pytest.fixture
def played(tmp_path, monkeypatch):
    monkeypatch.setenv("QAJEV_REPORTS", str(tmp_path / "reports"))  # no project reports but this test's
    dashboard._finished.clear()
    argv = ["play", "games/sidescroller/desktop", "--adapter", "imhim", "--suite", "s.yaml", "--name", "bosses"]
    job = jobs.start(argv, command=[sys.executable, "-c", FAKE_PLAY], cwd=str(tmp_path))
    return wait_for(lambda: jobs.status(job["id"])["state"] == "done" and job["id"]), tmp_path


@pytest.fixture
def server():
    srv = dashboard.make_server(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def request(srv, method, path, *, host=None, cookie=True, key=None, body=None):
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if cookie:
        headers["Cookie"] = f"qajev_dashboard={srv.RequestHandlerClass.key}"
    if key:
        headers["X-QAJev-Key"] = key
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    res = conn.getresponse()
    data = res.read()
    return res.status, dict(res.getheaders()), data


def test_every_run_is_listed_by_project_with_its_tests_screenshots_and_decisions(played):
    job_id, run_root = played
    row = next(r for r in dashboard.state()["runs"] if r["id"] == job_id)
    assert row["subject"] == "imhim" and row["command"] == "play" and row["gate"] == "FAIL"
    assert row["thumb"] == f"/files/{job_id}/shots/kira.jpg" and row["failed"] == 1
    assert ("imhim", 1) in dashboard.state()["subjects"]

    d = dashboard.detail(job_id)
    assert [s["name"] for s in d["steps"]] == ["kira: beaten", "kira: on the ledger"]
    assert d["steps"][0]["shot"] == f"/files/{job_id}/shots/kira.jpg" and d["steps"][0]["checks"][0]["ok"]
    assert d["steps"][0]["decisions"][0]["chose"] == "Spiral Orb" and d["steps"][0]["decisions"][0]["p"] == 0.55
    assert d["folder"] == str((run_root / "runs" / "boss-run").resolve())  # its run folder, wherever it ran
    assert d["report_html"] == f"/files/{job_id}/report.html"
    assert d["command_line"].startswith("qajev play games/sidescroller/desktop")
    assert d["about"] == "every boss can be beaten" and d["steps"][0]["about"] == "Kira's fight can be won at level 18"


def test_only_a_runs_own_files_are_served(played):
    job_id, run_root = played
    assert dashboard.run_file(job_id, "shots/kira.jpg").read_bytes().startswith(b"\xff\xd8")
    (run_root / "secret.txt").write_text("not this run's")
    for rel in ("../../secret.txt", "..%2F..%2Fsecret.txt", "/etc/passwd", "notes.py", "missing.jpg"):
        with pytest.raises(jobs.NoSuchJob):
            dashboard.run_file(job_id, rel)
    with pytest.raises(jobs.NoSuchJob):
        dashboard.run_file("../elsewhere", "shots/kira.jpg")


def test_the_page_needs_its_key_and_actions_need_the_key_the_page_carries(played, server):
    job_id, _ = played
    key = server.RequestHandlerClass.key
    status, _, _ = request(server, "GET", "/api/state", cookie=False)
    assert status == 401
    status, headers, _ = request(server, "GET", f"/?k={key}", cookie=False)
    assert status == 303 and f"qajev_dashboard={key}" in headers["Set-Cookie"] and "SameSite=Strict" in \
        headers["Set-Cookie"]
    status, _, page = request(server, "GET", "/")
    assert status == 200 and f'const KEY = "{key}"' in page.decode()
    status, _, data = request(server, "GET", "/api/state")
    assert status == 200 and any(r["id"] == job_id for r in json.loads(data)["runs"])
    status, headers, data = request(server, "GET", f"/files/{job_id}/shots/kira.jpg")
    assert status == 200 and headers["Content-Type"] == "image/jpeg"
    status, headers, _ = request(server, "GET", f"/files/{job_id}/report.html")
    assert status == 200 and headers["Content-Security-Policy"].startswith("sandbox")  # no script runs as the page
    assert request(server, "GET", f"/files/{job_id}/..%2F..%2Fsecret.txt")[0] == 404

    # A page elsewhere: its own host name pointed at 127.0.0.1, or a form posting with the person's cookie.
    assert request(server, "GET", "/api/state", host="evil.example:80")[0] == 403
    assert request(server, "POST", f"/api/run/{job_id}/rerun", body={"failed": True})[0] == 403
    assert request(server, "POST", f"/api/run/{job_id}/rerun", key="wrong", body={})[0] == 403


def test_the_page_reruns_a_runs_failed_tests_where_it_ran(played, server, monkeypatch):
    job_id, run_root = played
    started = []
    monkeypatch.setattr(jobs, "start", lambda argv, title=None, **k: started.append((argv, k)) or
                        {"id": "20261003-160000-abcd", "title": "t"})
    status, _, data = request(server, "POST", f"/api/run/{job_id}/rerun", key=server.RequestHandlerClass.key,
                              body={"failed": True})
    assert status == 200 and json.loads(data)["job"] == "20261003-160000-abcd"
    argv, kwargs = started[0]
    assert argv[-2:] == ["--only", "kira: on the ledger"]
    assert kwargs == {"cwd": str(run_root), "rerun": {"of": job_id, "failed": True}}


def test_the_page_never_reruns_shell_commands(monkeypatch):
    monkeypatch.setattr(jobs, "rerun_argv", lambda job, failed: (["run", "s.yaml", "--allow-commands"], "all"))
    monkeypatch.setattr(jobs, "start", lambda *a, **k: pytest.fail("must not start"))
    with pytest.raises(dashboard.DashboardError, match="allow-commands"):
        dashboard.rerun("20261003-055116-afb3", True)


def test_a_project_run_starts_only_for_a_known_project_env_and_objective(monkeypatch):
    monkeypatch.setattr(dashboard, "projects", lambda: [
        {"name": "shop", "envs": ["prod", "staging"], "default_env": "prod", "objectives": ["checkout"]}])
    started = []
    monkeypatch.setattr(jobs, "start", lambda argv, title=None, **k: started.append(argv) or {"id": "x", "title": "t"})
    dashboard.start_project("shop", "staging", "checkout")
    assert started == [["run", "--project", "shop", "--env", "staging", "--objective", "checkout"]]
    for bad in (("nope", None, None), ("shop", "dev", None), ("shop", None, "--allow-commands")):
        with pytest.raises(dashboard.DashboardError):
            dashboard.start_project(*bad)
    assert len(started) == 1


@pytest.mark.parametrize("argv, cwd_suite, want", [
    (["run", "--project", "shop", "--objective", "x"], None, "shop"),
    (["play", "/g/sidescroller/.claude/worktrees/qa/desktop", "--adapter", "imhim"], None, "imhim"),
    (["play", "/games/sevendawns/game", "--suite", "s.yaml"], None, "sevendawns"),  # "game" says nothing
    (["check", "https://www.example.com/pricing", "--goal", "x"], None, "example.com"),
    (["run", "suite.yaml"], "base_url: https://shop.example/\n", "shop.example"),
    (["smoke"], None, ""),
])
def test_a_run_is_grouped_under_its_project_site_or_game(tmp_path, argv, cwd_suite, want):
    if cwd_suite:
        (tmp_path / "suite.yaml").write_text(cwd_suite)
    assert jobs.subject(argv, cwd=str(tmp_path)) == want


def test_a_second_dashboard_points_at_the_one_running(tmp_path, monkeypatch, capsys):
    from qajev.cli import main

    monkeypatch.setattr(dashboard, "STATE", tmp_path / "dashboard.json")
    srv = dashboard.make_server(0)
    thread = threading.Thread(target=dashboard.serve, args=(srv,), daemon=True)
    thread.start()
    wait_for(lambda: (tmp_path / "dashboard.json").exists())
    assert oct((tmp_path / "dashboard.json").stat().st_mode & 0o777) == "0o600"  # it holds the key
    assert main(["dashboard", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"url": f"http://127.0.0.1:{srv.server_address[1]}/?k={srv.RequestHandlerClass.key}",
                   "already_running": True}
    srv.shutdown()
    thread.join(5)
    assert not (tmp_path / "dashboard.json").exists()
    assert Path(dashboard.PAGE).is_file()


def test_a_background_dashboard_outlives_its_command_and_stops_on_request(capsys):
    # José, 3 Oct: the dashboard died with the terminal (session) that started it.
    from qajev.cli import main

    assert dashboard.running() is None
    assert main(["dashboard", "--background", "--json", "--port", "0"]) == 0
    started = json.loads(capsys.readouterr().out)
    there = dashboard.running()
    assert there and there["pid"] != os.getpid() and started["url"] == there["url"]
    port = there["port"]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", f"/?k={there['key']}", headers={"Host": f"127.0.0.1:{port}"})
    assert conn.getresponse().status == 303
    assert main(["dashboard", "--json"]) == 0 and json.loads(capsys.readouterr().out)["already_running"]
    assert main(["dashboard", "--stop", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"stopped": there["pid"]}
    wait_for(lambda: dashboard.running() is None and not dashboard.STATE.exists())  # it removed its own record


def test_only_one_dashboard_can_start_at_a_time():
    held = dashboard.claim()
    try:
        assert held is not None and dashboard.claim() is None
    finally:
        os.close(held)
    again = dashboard.claim()
    assert again is not None
    os.close(again)


def test_a_bad_body_length_is_refused_at_once(server):
    # Orchestrator review: Content-Length -1 made rfile.read(-1) wait for the socket to close, holding a thread.
    import socket

    key, port = server.RequestHandlerClass.key, server.server_address[1]

    def post(length, body=b""):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:  # times out if the server waits
            s.sendall(f"POST /api/run/20261003-055116-afb3/rerun HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                      f"Cookie: qajev_dashboard={key}\r\nX-QAJev-Key: {key}\r\nContent-Length: {length}\r\n\r\n"
                      .encode() + body)
            return s.recv(200).split(b"\r\n", 1)[0]

    assert b" 404 " in post("-1")  # read nothing, went on: no such job
    assert b" 400 " in post("abc")
    assert b" 400 " in post("2", b"[]")  # not a JSON object
