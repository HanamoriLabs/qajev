"""--runner cloud|local|auto (José, 11 Oct): a run goes to QAJev Cloud or fails with the reason, never quietly to this
Mac; auto runs here only what the cloud may not take, saying why. A fake dashboard on 127.0.0.1 stands in for
cloud.qajev.com; the agent key must never show in any output or any child's arguments."""

import asyncio
import hashlib
import json
import signal
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from qajev import cli, cloud, config, jobs, mcp_server
from qajev.mcp_server import ToolError

KEY = "qjk_SECRETKEYVALUE0123456789abcdef"
RUN = "r_test1"


class FakeDashboard:
    """The worker's API, as qajev-cloud's docs/api-shapes.md says: records calls, checks the key and the sha256."""

    def __init__(self):
        self.calls, self.files, self.auth = [], {}, set()
        self.runs = [{"id": RUN, "gate": "pass", "counts": {"pass": 1}, "tests": []}]  # GET answers, the last repeats
        self.refuse_put = False
        self.default_runner = "cloud"  # the Settings page's "Where runs go by default"
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def _authed(self):
                outer.auth.add(self.headers.get("Authorization"))
                if self.headers.get("Authorization") != f"Bearer {KEY}":
                    self._reply(401, {"error": "no valid credential"})
                    return False
                return True

            def do_GET(self):
                if self.path == "/api/v1/config":
                    outer.calls.append(("GET", self.path, self.headers.get("Authorization")))
                    return self._reply(200, {"firebase": {"projectId": "x"}})
                if not self._authed():
                    return
                outer.calls.append(("GET", self.path, None))
                if self.path == "/api/v1/me":
                    me = {"kind": "agent", "scopes": ["read", "run"], "default_runner": outer.default_runner}
                    return self._reply(200, me)
                if self.path == f"/api/v1/runs/{RUN}":
                    run = outer.runs.pop(0) if len(outer.runs) > 1 else outer.runs[0]
                    return self._reply(200, {"id": RUN, **run})
                self._reply(404, {"error": "There is no such thing."})

            def do_POST(self):
                if not self._authed():
                    return
                body = json.loads(self._body() or b"null")
                outer.calls.append(("POST", self.path, body))
                if self.path == "/api/v1/runs":
                    return self._reply(201, {"run": {"id": RUN, "gate": "queued"}, "upload_urls": []})
                if self.path == f"/api/v1/runs/{RUN}/stop":
                    return self._reply(200, {"run": {"id": RUN, "gate": "stopped"}})
                self._reply(404, {"error": "no"})

            def do_PUT(self):
                if not self._authed():
                    return
                data = self._body()
                outer.calls.append(("PUT", self.path, len(data)))
                if outer.refuse_put:
                    return self._reply(400, {"error": "The file is larger than it said."})
                if self.headers.get("x-content-sha256") != hashlib.sha256(data).hexdigest():
                    return self._reply(400, {"error": "sha256 does not match"})
                outer.files[self.path.split("/files/", 1)[1]] = data
                self._reply(200, {"ok": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def sent(self, method=None):
        return [c for c in self.calls if method is None or c[0] == method]

    def created(self):
        return next(body for m, path, body in self.calls if m == "POST" and path == "/api/v1/runs")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Never the person's Keychain, settings or signal handlers; no wait between looks at a run."""
    home = tmp_path / "qajev-home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    for name in ("QAJEV_RUNNER", "QAJEV_CLOUD_URL", "QAJEV_CLOUD_KEY", "QAJEV_CLOUD_HOLD", "QAJEV_PROFILE",
                 "QAJEV_REQUIRE_PLAN",
                 "QAJEV_ENV_FILE", "QAJEV_JOB", "QAJEV_CLOUD_WAIT"):
        monkeypatch.delenv(name, raising=False)

    def no_keychain(args):
        raise OSError("no Keychain in tests")

    monkeypatch.setattr(cloud, "_security", no_keychain)
    monkeypatch.setattr(cloud, "POLL_S", 0.01)
    monkeypatch.setattr(cli, "_stop_when_orphaned", lambda: None)
    handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    monkeypatch.chdir(tmp_path)
    yield
    for s, h in handlers.items():
        signal.signal(s, h)


@pytest.fixture
def dash(monkeypatch):
    d = FakeDashboard()
    monkeypatch.setenv("QAJEV_CLOUD_URL", d.url)
    monkeypatch.setenv("QAJEV_CLOUD_KEY", KEY)
    yield d
    d.server.shutdown()


@pytest.fixture
def local(monkeypatch):
    """Records every run that would start on this Mac (it must not, unless the test says so)."""
    ran = []

    def here(args):
        ran.append(args.argv)
        return 0

    for name in ("cmd_run", "cmd_smoke", "cmd_play", "cmd_background"):
        monkeypatch.setattr(cli, name, here)
    monkeypatch.setattr(jobs, "register", lambda *a, **k: None)
    return ran


def suite_file(tmp_path, base="https://shop.example", name="s.yaml", extra=""):
    path = tmp_path / name
    path.write_text(f"name: shop\nbase_url: {base}\n{extra}scenarios:\n  - name: home\n    url: /\n"
                    "    about: the home page greets a visitor\n    expect: {text: [Welcome]}\n")
    return path


def site(tmp_path, files=2):
    folder = tmp_path / "dist"
    folder.mkdir()
    for i in range(files):
        (folder / f"f{i}.html").write_text(f"<p>page {i}</p>")
    return folder


# ---------------------------------------------------------------------------------------------------------------------
# --runner cloud refuses what the cloud may not take: nothing is sent, nothing runs here


@pytest.mark.parametrize("argv, said", [
    (["check", "https://shop.example/", "--profile", "work"], "signed-in runs stay on this Mac"),
    (["check", "http://127.0.0.1:3000/", "--mode", "mutate"], "the tunnel to this Mac is read-only"),
    (["check", "http://127.0.0.1/"], "a dev server port from 1024 to 65535"),
    (["check", "http://[::1]:8000/"], "the tunnel reaches this Mac's 127.0.0.1 or localhost only"),
    (["smoke", "http://shop.localhost:8000/"], "the tunnel reaches this Mac's 127.0.0.1 or localhost only"),
    (["play", "games/boss"], "the cloud does not take `play` runs yet"),
    (["check", "https://shop.example/", "--cdp-url", "http://127.0.0.1:9222"], "--cdp-url"),
    (["check", "https://shop.example/", "--real-devices", "ios"], "--real-devices"),
])
def test_cloud_refuses_what_must_stay_here(dash, local, capsys, argv, said):
    code = cli.main([*argv, "--runner", "cloud"])
    err = capsys.readouterr().err
    assert code == cli.EXIT_CONFIG and said in err and err.count("\n") == 1  # one line, with the reason
    assert dash.calls == [] and local == []


def test_a_run_on_this_macs_dev_server_goes_through_the_tunnel(dash, local, capsys, tmp_path):
    # QAJev Cloud 11 Oct (José: "Tunnel first, then a day"): the dispatcher on this Mac opens a read-only tunnel to
    # one dev server port, so most local runs can move to the cloud.
    assert cli.main(["check", "http://127.0.0.1:3000/shop", "--runner", "cloud"]) == 0
    assert dash.created()["tunnel"] == {"port": 3000} and "serve" not in dash.created()
    path = suite_file(tmp_path, base="http://localhost:4000")
    assert cli.main(["run", str(path), "--runner", "cloud"]) == 0
    assert [b["tunnel"] for m, p, b in dash.calls if m == "POST" and p == "/api/v1/runs"][-1] == {"port": 4000}
    assert local == []


def test_only_a_run_that_says_runner_cloud_reaches_this_macs_dev_server(dash, local, capsys, monkeypatch, tmp_path):
    # Orchestrator, 11 Oct: the tunnel passes GET, HEAD and OPTIONS only, so a page that posts or opens a WebSocket
    # would fail in the cloud for the tunnel's sake. Unless the command itself names the cloud, it stays here or stops.
    monkeypatch.setenv("QAJEV_RUNNER", "auto")
    assert cli.main(["check", "http://127.0.0.1:3000/shop"]) == 0
    assert dash.sent("POST") == [] and len(local) == 1
    assert "only with --runner cloud" in capsys.readouterr().err
    monkeypatch.setenv("QAJEV_RUNNER", "cloud")
    assert cli.main(["check", "http://127.0.0.1:3000/shop"]) == cli.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "only with --runner cloud" in err and "POST" in err and "WebSocket" in err
    assert dash.sent("POST") == [] and len(local) == 1
    assert cli.main(["check", "http://127.0.0.1:3000/shop", "--runner", "cloud"]) == 0
    assert dash.created()["tunnel"] == {"port": 3000}


def test_a_held_suite_stays_here_unless_the_command_says_runner_cloud(dash, local, capsys, monkeypatch, tmp_path):
    # Orchestrator, 12:32 and 14:48: a Verse suite goes to the cloud only with an OK for that suite, named on the run.
    monkeypatch.setenv("QAJEV_CLOUD_HOLD", "figgyz, verse")
    (tmp_path / "figgyz").mkdir()
    held = suite_file(tmp_path / "figgyz", base="https://verse.figgyz.com")
    for mode in ("auto", "cloud"):
        monkeypatch.setenv("QAJEV_RUNNER", mode)
        code = cli.main(["run", str(held)])
        err = capsys.readouterr().err
        assert dash.sent("POST") == [] and "held on this Mac" in err and "QAJEV_CLOUD_HOLD" in err
        assert code == (0 if mode == "auto" else cli.EXIT_CONFIG)
    assert len(local) == 1  # auto ran it here; cloud stopped with the reason
    assert cli.main(["check", "https://verse.figgyz.com/hair"]) == cli.EXIT_CONFIG  # a held target, by its host
    assert cli.main(["run", str(held), "--runner", "cloud"]) == 0
    assert dash.created()["kind"] == "run"
    assert cli.main(["check", "https://shop.example/"]) == 0  # nothing held there: it goes


@pytest.mark.parametrize("secret", [".env", ".dev.vars", ".env.production", "certs/server.pem", "deploy.key",
                                    "config/service-account.json", ".npmrc", ".git/config", "id_ed25519"])
def test_a_served_folder_that_holds_a_secret_is_refused(dash, local, capsys, tmp_path, secret):
    # Orchestrator, 11 Oct: a Verse build shipped with --serve must be a git-archive build with no .dev.vars, .env or
    # keys. The folder goes to a cloud machine, so one is refused with its name and nothing is sent.
    built = site(tmp_path)
    (built / secret).parent.mkdir(parents=True, exist_ok=True)
    (built / secret).write_text("TOKEN=x")
    code = cli.main(["check", "http://127.0.0.1:8877/", "--serve", str(built), "--runner", "cloud"])
    err = capsys.readouterr().err
    shown = ".git/" if secret.startswith(".git/") else secret
    assert code == cli.EXIT_CONFIG and shown in err and "git archive" in err
    assert dash.calls == [] and local == []


def test_a_public_target_needs_no_tunnel_and_a_shipped_build_never_gets_one(dash, local, tmp_path):
    assert cli.main(["check", "https://shop.example/", "--runner", "cloud"]) == 0
    assert "tunnel" not in dash.created()
    built = site(tmp_path)
    assert cli.main(["check", "http://127.0.0.1:8877/", "--serve", str(built), "--runner", "cloud"]) == 0
    sent = [b for m, p, b in dash.calls if m == "POST" and p == "/api/v1/runs"][-1]
    assert sent["serve"] == {"path": "site", "port": 8877} and "tunnel" not in sent


def test_a_suite_that_writes_or_opens_two_ports_stays_here_and_json_says_why(dash, local, capsys, tmp_path):
    writes = suite_file(tmp_path, base="http://127.0.0.1:4000", extra="mode: mutate\n")
    assert cli.main(["run", str(writes), "--runner", "cloud", "--json"]) == cli.EXIT_CONFIG
    out = json.loads(capsys.readouterr().out)
    assert out["runner"] == "cloud" and "read-only" in out["reason"] and out["exit_code"] == 3
    two = suite_file(tmp_path, base="http://127.0.0.1:4000", name="two.yaml",
                     extra="hosts: [127.0.0.1:4000, 127.0.0.1:5000]\n")
    assert cli.main(["run", str(two), "--runner", "cloud"]) == cli.EXIT_CONFIG
    assert "one dev server per run" in capsys.readouterr().err
    assert dash.calls == [] and local == []


# ---------------------------------------------------------------------------------------------------------------------
# The full tunnel (QAJev Cloud, 12 Oct): a suite's `tunnel:` names each dev server port and its command; the run goes to
# the cloud with several ports, writes on local data and WebSockets. The dispatcher on this Mac checks each process.

TUNNEL = ("tunnel:\n  ports:\n    8871: {command: \"node serve-folder.mjs\"}\n"
          "    8787: {command: \"wrangler dev\"}\n  data: local\n")


def checkout(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "qa").mkdir()
    return repo


def test_a_suite_with_a_tunnel_goes_to_the_cloud_with_its_ports_writes_and_folder(dash, local, monkeypatch, tmp_path):
    # verse-ship room 21: a game server and its API on this Mac, a PUT on local data. No --runner cloud needed: the
    # suite's tunnel says which processes may answer, so auto sends it too.
    monkeypatch.setenv("QAJEV_RUNNER", "auto")
    repo = checkout(tmp_path)
    path = suite_file(repo / "qa", base="http://127.0.0.1:8871",
                      extra=f"mode: mutate\nhosts: [127.0.0.1:8871, localhost:8787]\n{TUNNEL}")
    assert cli.main(["run", str(path)]) == 0
    assert dash.created()["tunnel"] == {
        "ports": {"8871": {"command": "node serve-folder.mjs"}, "8787": {"command": "wrangler dev"}},
        "data": "local", "root": str(repo.resolve()), "writes": True, "source": {"file": str(path.resolve())}}
    assert local == []


def test_qajev_tunnel_prints_what_the_file_declares_for_the_dispatcher_to_check(capsys, tmp_path):
    # redteam, 12 Oct: the dispatcher reads the suite or project file on disk again and refuses a run whose tunnel
    # differs from it, so a run record alone cannot open a port.
    repo = checkout(tmp_path)
    path = suite_file(repo / "qa", base="http://127.0.0.1:8871", extra=TUNNEL)
    assert cli.main(["tunnel", str(path)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ports": {"8871": {"command": "node serve-folder.mjs"}, "8787": {"command": "wrangler dev"}}, "data": "local"}
    plain = suite_file(repo / "qa", name="plain.yaml")
    assert cli.main(["tunnel", str(plain)]) == cli.EXIT_CONFIG
    assert "no tunnel" in capsys.readouterr().err


@pytest.mark.parametrize("extra, said", [
    # a port the run uses that the tunnel does not name
    (f"hosts: [127.0.0.1:8871, 127.0.0.1:9000]\n{TUNNEL}", "port 9000 on this machine"),
    # writes with no data: local
    ("mode: mutate\ntunnel:\n  ports:\n    8871: {command: \"node serve-folder.mjs\"}\n", "needs data: local"),
    # [::1] is loopback, but the tunnel listens on 127.0.0.1 and localhost only
    (f"hosts: ['[::1]:8871']\n{TUNNEL}", "127.0.0.1 or localhost only"),
])
def test_a_tunnel_run_that_breaks_a_rule_is_refused_and_nothing_is_sent(dash, local, capsys, tmp_path, extra, said):
    repo = checkout(tmp_path)
    path = suite_file(repo / "qa", base="http://127.0.0.1:8871", extra=extra)
    assert cli.main(["run", str(path), "--runner", "cloud"]) == cli.EXIT_CONFIG
    assert said in capsys.readouterr().err
    assert dash.calls == [] and local == []


def test_a_tunnel_needs_the_projects_folder(dash, local, capsys, tmp_path):
    # The guard checks that each dev server runs from inside the project's git checkout: no checkout, no tunnel.
    path = suite_file(tmp_path, base="http://127.0.0.1:8871", extra=TUNNEL)
    assert cli.main(["run", str(path), "--runner", "cloud"]) == cli.EXIT_CONFIG
    assert "needs the project's folder" in capsys.readouterr().err
    assert dash.calls == [] and local == []


def test_no_key_fails_and_nothing_runs_here(dash, local, capsys, monkeypatch):
    monkeypatch.delenv("QAJEV_CLOUD_KEY")
    monkeypatch.setattr(cloud, "_security", lambda args: subprocess.CompletedProcess(args, 44, "", "not found"))
    assert cli.main(["check", "https://shop.example/", "--runner", "cloud"]) == cli.EXIT_CONFIG
    assert "no QAJev Cloud agent key" in capsys.readouterr().err
    assert dash.calls == [] and local == []


def test_a_dashboard_that_does_not_answer_fails_with_the_reason(local, capsys, monkeypatch):
    import socket

    with socket.socket() as s:  # a port nobody listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("QAJEV_CLOUD_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("QAJEV_CLOUD_KEY", KEY)
    assert cli.main(["check", "https://shop.example/", "--runner", "cloud"]) == 4
    assert "does not answer" in capsys.readouterr().err and local == []


def test_a_key_never_travels_in_clear_text(local, capsys, monkeypatch):
    monkeypatch.setenv("QAJEV_CLOUD_URL", "http://cloud.example")
    monkeypatch.setenv("QAJEV_CLOUD_KEY", KEY)
    assert cli.main(["check", "https://shop.example/", "--runner", "cloud"]) == cli.EXIT_CONFIG
    assert "must be https://" in capsys.readouterr().err and local == []


# ---------------------------------------------------------------------------------------------------------------------
# --runner auto: here only what the cloud may not take, with one line why; a cloud failure is still a failure


def test_auto_runs_a_signed_in_run_and_a_writing_run_here_saying_why(dash, local, capsys):
    assert cli.main(["check", "https://shop.example/", "--profile", "work", "--runner", "auto"]) == 0
    assert cli.main(["check", "http://127.0.0.1:3000/", "--mode", "mutate", "--runner", "auto"]) == 0
    err = capsys.readouterr().err
    assert "signed-in runs stay on this Mac" in err and "the tunnel to this Mac is read-only" in err
    assert len(local) == 2 and dash.calls == []


def test_auto_never_falls_back_when_the_cloud_fails(dash, local, capsys, monkeypatch):
    monkeypatch.delenv("QAJEV_CLOUD_KEY")
    assert cli.main(["check", "https://shop.example/", "--runner", "auto"]) == cli.EXIT_CONFIG
    assert "no QAJev Cloud agent key" in capsys.readouterr().err and local == []


def test_local_is_todays_run_and_cloud_flags_need_a_cloud_run(dash, local, capsys, tmp_path):
    assert cli.main(["check", "https://shop.example/"]) == 0  # no flag, no setting: this Mac, nothing sent
    assert local == [["check", "https://shop.example/"]] and dash.calls == []
    assert cli.main(["check", "https://shop.example/", "--runner", "local", "--model", "clef"]) == cli.EXIT_CONFIG
    assert "--model go with cloud runs" in capsys.readouterr().err and len(local) == 1


# ---------------------------------------------------------------------------------------------------------------------
# A cloud run: queued, followed to its gate, reported like a run here


def test_a_pass_sends_only_the_runs_own_arguments_and_reports_it(dash, local, capsys):
    dash.runs = [{"gate": "queued"}, {"gate": "running", "progress": "0 of 1 tests", "now_test": "pricing"},
                 {"gate": "pass", "counts": {"pass": 1, "fail": 0},
                  "files": {"report_html": f"/api/v1/runs/{RUN}/files/report.html"},
                  "tests": [{"name": "pricing", "outcome": "pass", "about": "the price shows", "wrong": []}]}]
    code = cli.main(["check", "https://shop.example/", "--goal", "Find the price. Stop when it shows.",
                     "--expect-text", "$9", "--runner", "cloud", "--model", "clef", "--out", "/tmp/here",
                     "--headless", "--load-high", "0", "--jev-provider", "typesafe"])
    err = capsys.readouterr().err
    assert code == 0 and local == []
    body = dash.created()
    assert body["args"] == ["check", "https://shop.example/", "--goal", "Find the price. Stop when it shows.",
                            "--expect-text", "$9"]  # no flag that means something on this Mac only
    assert (body["kind"], body["surface"], body["runner"], body["model"]) == ("check", "web", "cloud", "clef")
    url = f"{dash.url}/run.html?id={RUN}"
    assert err.index(url) < err.index("running: 0 of 1 tests · pricing") < err.index("Gate: PASS")
    assert "✓ pass       pricing" in err and f"Report: {dash.url}/api/v1/runs/{RUN}/files/report.html" in err


def test_a_fail_exits_1_with_each_tests_reason(dash, local, capsys):
    dash.runs = [{"gate": "fail", "counts": {"pass": 0, "fail": 1},
                  "tests": [{"name": "pricing", "outcome": "fail", "wrong": ["Expected the text \"$9\"."]}]}]
    assert cli.main(["check", "https://shop.example/", "--runner", "cloud"]) == 1
    err = capsys.readouterr().err
    assert "✗ fail       pricing  Expected the text \"$9\"." in err and "Gate: FAIL" in err


def test_a_run_stopped_on_the_dashboard_fails_with_its_reason_and_address(dash, local, capsys):
    dash.runs = [{"gate": "stopped", "reason": "A person stopped the run.", "tests": []}]
    assert cli.main(["check", "https://shop.example/", "--runner", "cloud"]) == 2
    err = capsys.readouterr().err
    assert "Gate: STOPPED" in err and "Reason: A person stopped the run." in err and f"run.html?id={RUN}" in err


def test_a_queued_run_says_why_it_waits_and_when_that_ends(dash, local, capsys):
    # QAJev Cloud says why a queued run does not start (credits, a cap, machines off) in "waiting" (12 Oct).
    why = ("Waiting for credits: the run needs 120 for its 120-minute limit, and the balance is 100. "
           "Buy credits at https://cloud.qajev.com/credits.html, or give the run a shorter time limit.")
    dash.runs = [{"gate": "queued", "waiting": {"reason": "credits", "text": why, "need": 120, "balance": 100}},
                 {"gate": "queued", "waiting": None},
                 {"gate": "pass", "counts": {"pass": 1}, "tests": []}]
    assert cli.main(["check", "https://shop.example/", "--runner", "cloud"]) == 0
    err = capsys.readouterr().err
    assert err.index(f"queued: {why}") < err.index("queued: waiting for a cloud machine") < err.index("Gate: PASS")


def test_events_carry_the_waiting_reason(dash, local, capsys):
    why = {"reason": "machines off", "text": "Cloud machines are off. An admin turns them on in Settings."}
    dash.runs = [{"gate": "queued", "waiting": why}, {"gate": "pass", "counts": {"pass": 1}, "tests": []}]
    assert cli.main(["check", "https://shop.example/", "--runner", "cloud", "--events"]) == 0
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    assert {"gate": "queued", "waiting": why}.items() <= next(e for e in events if e.get("waiting")).items()


def test_a_wait_that_runs_out_fails_and_leaves_the_run_going(dash, local, capsys, monkeypatch):
    monkeypatch.setenv("QAJEV_CLOUD_WAIT", "0.05")
    dash.runs = [{"gate": "running", "progress": "1 of 9 tests"}]
    assert cli.main(["check", "https://shop.example/", "--runner", "cloud"]) == 4
    err = capsys.readouterr().err
    assert "has not finished; it goes on in the cloud" in err and f"run.html?id={RUN}" in err
    assert not any(path.endswith("/stop") for _, path, _ in dash.sent("POST"))


def test_an_upload_that_fails_stops_the_run(dash, local, capsys, tmp_path):
    dash.refuse_put = True
    assert cli.main(["run", str(suite_file(tmp_path)), "--runner", "cloud"]) == 4
    err = capsys.readouterr().err
    assert "the upload of in/s.yaml failed, so the run was stopped" in err and f"run.html?id={RUN}" in err
    assert ("POST", f"/api/v1/runs/{RUN}/stop", {}) in dash.calls and local == []


def test_json_is_one_object_in_the_agreed_shape(dash, local, capsys):
    dash.runs = [{"gate": "fail", "counts": {"pass": 1, "fail": 1}, "reason": None,
                  "files": {"report_html": f"/api/v1/runs/{RUN}/files/report.html"},
                  "tests": [{"name": "a", "outcome": "pass", "wrong": []},
                            {"name": "b", "outcome": "fail", "wrong": ["no price"],
                             "checks": [{"check": 'js "price()"', "ok": False, "value": "no price"}]}]}]
    assert cli.main(["check", "https://shop.example/", "--runner", "cloud", "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"runner", "run_id", "url", "gate", "counts", "tests", "report_url", "reason"}
    assert (out["runner"], out["run_id"], out["gate"]) == ("cloud", RUN, "FAIL")
    assert out["tests"][1]["reason"] == "no price" and out["tests"][1]["wrong"] == ["no price"]
    # verse-ship, 12 Oct: a gate reads tests[].checks[].value as on a local run; an older dashboard sends none
    assert out["tests"][1]["checks"] == [{"check": 'js "price()"', "ok": False, "value": "no price"}]
    assert out["tests"][0]["checks"] == []
    assert out["report_url"] == f"{dash.url}/api/v1/runs/{RUN}/files/report.html"


def test_background_returns_the_cloud_run_at_once_and_jobs_and_stop_take_its_id(dash, local, capsys):
    assert cli.main(["check", "https://shop.example/", "--runner", "cloud", "--background", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"runner": "cloud", "run_id": RUN, "url": f"{dash.url}/run.html?id={RUN}", "gate": "QUEUED"}
    assert not dash.sent("GET") and local == []
    dash.runs = [{"gate": "running", "progress": "1 of 2 tests"}]
    assert cli.main(["jobs", RUN, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["gate"] == "RUNNING"
    why = {"reason": "minutes", "text": "Today's 300 cloud minutes are used up."}
    dash.runs = [{"gate": "queued", "waiting": why}]
    assert cli.main(["jobs", RUN, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["waiting"] == why
    assert cli.main(["jobs", RUN]) == 0
    assert "waiting: Today's 300 cloud minutes are used up." in capsys.readouterr().out
    assert cli.main(["stop", RUN]) == 0
    assert ("POST", f"/api/v1/runs/{RUN}/stop", {}) in dash.calls


def test_a_suite_on_this_mac_ships_its_build_with_serve(dash, local, capsys, tmp_path):
    path = suite_file(tmp_path, base="http://127.0.0.1:5173")
    dist = site(tmp_path)
    assert cli.main(["run", str(path), "--runner", "cloud", "--serve", str(dist), "--only", "home"]) == 0
    body = dash.created()
    assert body["args"] == ["run", "in/s.yaml", "--only", "home"] and body["serve"] == {"path": "site", "port": 5173}
    assert sorted(dash.files) == ["in/s.yaml", "site/f0.html", "site/f1.html"]
    assert dash.files["in/s.yaml"] == path.read_bytes()


def test_a_big_build_ships_as_tar_parts(dash, local, tmp_path, monkeypatch):
    monkeypatch.setattr(cloud, "PACK_OVER_FILES", 2)
    assert cli.main(["check", "http://127.0.0.1:8877/", "--runner", "cloud", "--serve", str(site(tmp_path, 3))]) == 0
    assert list(dash.files) == ["site/qcloud-archive.000.part"]


def test_serve_on_another_port_than_the_run_opens_is_refused(dash, local, capsys, tmp_path):
    assert cli.main(["check", "http://127.0.0.1:3000/", "--runner", "cloud",
                     "--serve", f"{site(tmp_path)}:4000"]) == cli.EXIT_CONFIG
    assert "but the run opens http://127.0.0.1:3000/" in capsys.readouterr().err and dash.calls == []


def test_the_key_never_shows_in_output_or_a_childs_arguments(dash, local, capsys, monkeypatch, tmp_path):
    monkeypatch.delenv("QAJEV_CLOUD_KEY")  # from Keychain: `security` prints it to us, and only to us
    children = []
    monkeypatch.setattr(cloud, "_security", lambda args: children.append(["security", *args])
                        or subprocess.CompletedProcess(args, 0, KEY + "\n", ""))
    real_popen = subprocess.Popen

    def spy(args, *a, **k):
        children.append(list(args) if isinstance(args, (list, tuple)) else [args])
        return real_popen(args, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", spy)
    dash.runs = [{"gate": "fail", "reason": f"echo {KEY}", "tests": [{"name": "a", "outcome": "fail",
                                                                      "wrong": ["x"]}]}]
    cli.main(["check", "https://shop.example/", "--runner", "cloud"])
    cli.main(["check", "https://shop.example/", "--runner", "cloud", "--json"])
    dash.refuse_put = True
    cli.main(["run", str(suite_file(tmp_path)), "--runner", "cloud", "--json"])
    printed = capsys.readouterr()
    assert f"Bearer {KEY}" in dash.auth  # it was used
    assert children and all(c[:2] == ["security", "find-generic-password"] for c in children)
    assert all(KEY not in " ".join(c) for c in children)
    assert KEY not in printed.out and KEY not in printed.err  # not even when the dashboard's answer holds it


# ---------------------------------------------------------------------------------------------------------------------
# The default runner


def test_the_runner_comes_from_the_flag_the_project_the_shell_the_env_file_then_local(monkeypatch, tmp_path):
    # José, 11 Oct: a project can say where its runs go, and that wins over this Mac's mode.
    repo = tmp_path / "shop"
    (repo / ".qajev").mkdir(parents=True)
    toml = repo / ".qajev" / "project.toml"
    toml.write_text('name = "shop"\nrunner = "cloud"\n[env.prod]\nbase_url = "https://shop.example"\n')
    args = SimpleNamespace(runner=None, env_file=None, command="run", project=str(repo))
    assert cloud.runner_for(args) == ("cloud", f"runner in {toml}")
    monkeypatch.setenv("QAJEV_RUNNER", "local")
    assert cloud.runner_for(args)[0] == "cloud"  # the project wins over this Mac's mode
    assert cloud.runner_for(SimpleNamespace(**{**vars(args), "runner": "local"})) == ("local", "--runner")
    no_project = SimpleNamespace(**{**vars(args), "project": None})
    assert cloud.runner_for(no_project) == ("local", "QAJEV_RUNNER from the shell")
    monkeypatch.delenv("QAJEV_RUNNER")
    (config.HOME / ".env").write_text("QAJEV_RUNNER=auto\n")
    assert cloud.runner_for(no_project) == ("auto", f"QAJEV_RUNNER from {config.HOME / '.env'}")
    monkeypatch.setenv("QAJEV_RUNNER", "mars")
    with pytest.raises(cloud.CloudError, match="use local, cloud, auto or dashboard"):
        cloud.runner_for(no_project)
    monkeypatch.delenv("QAJEV_RUNNER")
    (config.HOME / ".env").write_text("")
    assert cloud.runner_for(no_project) == ("local", "default")


def test_qajev_mode_sets_a_project_and_default_hands_it_back_to_this_mac(capsys, tmp_path):
    repo = tmp_path / "shop"
    (repo / ".qajev").mkdir(parents=True)
    toml = repo / ".qajev" / "project.toml"
    toml.write_text('# the shop\nname = "shop"\n\n[env.prod]\nbase_url = "https://shop.example"\n')
    assert cli.main(["mode", "cloud", "--project", str(repo)]) == 0
    assert 'runner = "cloud"' in toml.read_text().split("[env.prod]")[0]  # a top-level key, before any table
    assert cli.main(["mode", "local", "--project", str(repo)]) == 0
    assert toml.read_text().count("runner = ") == 1 and 'runner = "local"' in toml.read_text()
    assert cli.main(["mode", "default", "--project", str(repo)]) == 0
    assert "runner" not in toml.read_text()
    assert "base_url" in toml.read_text() and "# the shop" in toml.read_text()
    assert cli.main(["mode", "default"]) != 0  # only a project goes back to the default


def test_a_projects_runner_must_be_one_of_the_three(tmp_path):
    from qajev import project

    path = tmp_path / "p.toml"
    path.write_text('runner = "mac"\n[env.prod]\nbase_url = "https://shop.example"\n')
    with pytest.raises(project.ProjectError, match="runner must be one of"):
        project.load(str(path))


def test_a_project_run_ships_its_project_file(dash, local, tmp_path):
    path = tmp_path / "shop.toml"
    path.write_text('name = "shop"\nrunner = "cloud"\n[env.prod]\nbase_url = "https://shop.example"\n'
                    '[[objective]]\nname = "home"\nurl = "/"\nabout = "it loads"\nexpect = { text = ["Hi"] }\n')
    assert cli.main(["run", "--project", str(path), "--suite", "core"]) == 0  # runner = "cloud" in the file
    body = dash.created()
    assert body["args"] == ["run", "--project", "in/shop.toml", "--suite", "core"] and "project" not in body
    assert dash.files["in/shop.toml"] == path.read_bytes() and local == []


def test_a_project_env_with_a_tunnel_sends_it_from_the_repo_and_a_bad_one_fails_at_load(dash, local, tmp_path):
    from qajev import project

    repo = checkout(tmp_path)
    (repo / ".qajev").mkdir()
    path = repo / ".qajev" / "project.toml"
    path.write_text('name = "rooms"\nrunner = "cloud"\n[env.local]\nbase_url = "http://127.0.0.1:8871"\n'
                    'tunnel = { ports = { 8871 = { command = "node serve-folder.mjs" } } }\n'
                    '[[objective]]\nname = "home"\nurl = "/"\nabout = "it loads"\nexpect = { text = ["Hi"] }\n')
    assert cli.main(["run", "--project", str(path), "--suite", "core"]) == 0
    assert dash.created()["tunnel"] == {"ports": {"8871": {"command": "node serve-folder.mjs"}},
                                        "root": str(repo.resolve()),
                                        "source": {"file": str(path.resolve()), "env": "local"}}
    assert cli.main(["tunnel", str(path), "--env", "local"]) == 0
    path.write_text(path.read_text().replace("node serve-folder.mjs", "node"))
    with pytest.raises(project.ProjectError, match="env.local: tunnel.ports.8871: give the dev server's command"):
        project.load(str(path))
    assert local == []


# ---------------------------------------------------------------------------------------------------------------------
# MCP


def test_the_run_tools_take_a_runner_in_plain_words():
    tools = {t.name: t for t in asyncio.run(mcp_server.server.list_tools())}
    for name in ("qa_check", "qa_run_suite", "qa_smoke", "qa_project_run"):
        props = tools[name].input_schema["properties"]
        assert {"runner", "serve", "model"} <= set(props), name
        assert "never run here instead" in tools[name].description
    assert "r_" in tools["qa_job"].description and "r_" in tools["qa_stop"].description
    assert "runner=" in mcp_server.INSTRUCTIONS


def test_mcp_background_cloud_run_returns_its_id_and_qa_job_and_qa_stop_take_it(dash, monkeypatch):
    async def never(*a, **k):
        raise AssertionError("ran on this machine")

    monkeypatch.setattr(mcp_server, "_run_report", never)
    out = asyncio.run(mcp_server.qa_check("https://shop.example/", None, runner="cloud", background=True,
                                          expect_text=["Hi"]))
    assert (out["runner"], out["job"], out["url"]) == ("cloud", RUN, f"{dash.url}/run.html?id={RUN}")
    assert dash.created()["args"][:2] == ["check", "https://shop.example/"]
    assert "--ephemeral" not in dash.created()["args"] and "--out" not in dash.created()["args"]
    dash.runs = [{"gate": "running", "progress": "1 of 3 tests", "now_test": "home"}]
    job = asyncio.run(mcp_server.qa_job(RUN))
    assert (job["state"], job["progress"], job["now_test"]) == ("running", "1 of 3 tests", "home")
    asyncio.run(mcp_server.qa_stop(RUN))
    assert ("POST", f"/api/v1/runs/{RUN}/stop", {}) in dash.calls


def test_mcp_cloud_run_waits_for_its_gate(dash, monkeypatch):
    dash.runs = [{"gate": "running"}, {"gate": "pass", "counts": {"pass": 1}, "tests": []}]
    out = asyncio.run(mcp_server.qa_smoke("https://shop.example/", None, runner="cloud"))
    assert (out["runner"], out["gate"]) == ("cloud", "PASS") and dash.created()["kind"] == "smoke"


def test_mcp_cloud_refuses_and_auto_runs_here_with_a_note(dash, monkeypatch):
    seen = []

    async def here(args, ctx, background, title=None):
        seen.append(args)
        return {"gate": "PASS", "run_dir": "/runs/x", "scenarios": []}

    monkeypatch.setattr(mcp_server, "_run_report", here)
    with pytest.raises(ToolError, match="the tunnel to this Mac is read-only"):
        asyncio.run(mcp_server.qa_check("http://127.0.0.1:3000/", None, runner="cloud", mode="mutate"))
    with pytest.raises(ToolError, match="signed-in runs stay"):
        asyncio.run(mcp_server.qa_check("https://shop.example/", None, runner="cloud", profile="work"))
    assert seen == [] and dash.calls == []
    out = asyncio.run(mcp_server.qa_check("http://127.0.0.1:3000/", None, runner="auto", mode="mutate"))
    assert "read-only" in out["runner_note"] and "--runner" not in seen[0] and dash.calls == []
    with pytest.raises(ToolError, match="runner must be"):
        asyncio.run(mcp_server.qa_check("https://shop.example/", None, runner="mars"))


# ---------------------------------------------------------------------------------------------------------------------
# qajev doctor


def test_doctor_says_where_the_key_is_never_its_value_and_whether_the_dashboard_answers(dash, monkeypatch):
    monkeypatch.setenv("QAJEV_RUNNER", "cloud")
    ok, detail = cloud.doctor()
    assert ok and "key found in QAJEV_CLOUD_KEY" in detail and "default runner cloud" in detail
    assert f"{dash.url} answers" in detail and KEY not in detail
    assert ("GET", "/api/v1/config", None) in dash.calls  # asked with no key
    monkeypatch.delenv("QAJEV_CLOUD_KEY")
    ok, detail = cloud.doctor(offline=True)
    assert not ok and "key not found" in detail and "not checked (--offline)" in detail
    monkeypatch.setenv("QAJEV_RUNNER", "local")
    assert cloud.doctor(offline=True)[0]  # runs stay here: a missing key is not a fault


def test_doctor_prints_a_cloud_line(dash, monkeypatch, capsys):
    from qajev import chrome

    monkeypatch.setattr(chrome, "binary", lambda: "/chrome")
    monkeypatch.setattr(chrome, "free_port", lambda: 9999)
    monkeypatch.setattr(chrome, "reap", lambda: [])
    monkeypatch.setattr(chrome, "status", lambda *a: [])
    cli.main(["doctor", "--json", "--offline"])
    checks = {c["check"]: c for c in json.loads(capsys.readouterr().out)["checks"]}
    assert "default runner local" in checks["cloud"]["detail"] and checks["cloud"]["ok"]
    assert KEY not in json.dumps(checks)


def test_qajev_runner_switches_this_mac_and_says_where_runs_go(capsys):
    # José, 11 Oct: "something that makes it super easy to change between cloud and local".
    env = config.HOME / ".env"
    env.write_text("TYPESAFE_MODEL=x\nQAJEV_RUNNER=local\n")
    assert cli.main(["mode", "cloud"]) == 0
    assert env.read_text().splitlines() == ["TYPESAFE_MODEL=x", "QAJEV_RUNNER=cloud"]
    assert "cloud" in capsys.readouterr().out
    assert cli.main(["mode", "auto"]) == 0
    assert env.read_text().count("QAJEV_RUNNER=") == 1
    assert cli.main(["mode", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert shown["runner"] == "auto" and shown["setting"] == "auto" and "~/.env" not in shown["source"]
    with pytest.raises(SystemExit):  # argparse refuses it
        cli.main(["mode", "mars"])


def test_qajev_runner_dashboard_follows_the_settings_page(dash, capsys):
    assert cli.main(["mode", "dashboard"]) == 0
    assert "QAJEV_RUNNER=dashboard" in (config.HOME / ".env").read_text()
    args = SimpleNamespace(runner=None, env_file=None, command="run", project=None)
    runner, source = cloud.runner_for(args)
    assert runner == "cloud" and "dashboard" in source
    dash.default_runner = "local"
    cloud._dashboard_cache.clear()
    assert cloud.runner_for(args)[0] == "local"
    # a flag still wins over the dashboard
    assert cloud.runner_for(SimpleNamespace(**{**vars(args), "runner": "cloud"})) == ("cloud", "--runner")


def test_a_silent_dashboard_keeps_the_run_here_and_says_why(monkeypatch):
    monkeypatch.setenv("QAJEV_RUNNER", "dashboard")
    monkeypatch.setenv("QAJEV_CLOUD_URL", "http://127.0.0.1:9")  # nothing listens
    monkeypatch.setenv("QAJEV_CLOUD_KEY", KEY)
    cloud._dashboard_cache.clear()
    runner, source = cloud.runner_for(SimpleNamespace(runner=None, env_file=None, command="run", project=None))
    assert runner == "local" and "did not answer" in source


# ---------------------------------------------------------------------------------------------------------------------
# A seeded TEST account in a cloud run (José via the Orchestrator, 12 Oct): only through the full tunnel with
# data: local, only seed:FILE#KEY from a fixture that says "test_account": true, and only the values the run uses.

ACCOUNT = ("account:\n  email: qa-a@example.test\n  password: seed:../fixtures/users.json#password_a\n"
           "  login: {url: /login}\n")


def seeded(tmp_path, fixture=None, extra=ACCOUNT, tunnel=TUNNEL):
    repo = checkout(tmp_path)
    (repo / "fixtures").mkdir()
    users = fixture if fixture is not None else {
        "test_account": True, "allowed_hosts": ["127.0.0.1", "localhost"], "password_a": "pw-a-1234",
        "password_b": "pw-b-5678", "admin_password": "never-ship-me"}
    (repo / "fixtures" / "users.json").write_text(json.dumps(users))
    path = suite_file(repo / "qa", base="http://127.0.0.1:8871", extra=f"{extra}{tunnel}")
    return repo, path


def test_a_seeded_account_goes_with_only_its_own_value_and_the_suite_points_at_it(dash, local, tmp_path):
    repo, path = seeded(tmp_path)
    assert cli.main(["run", str(path), "--runner", "cloud"]) == 0
    body = dash.created()
    fixture_file = str((repo / "fixtures" / "users.json").resolve())
    remote = cloud.seed_remote(fixture_file)
    assert remote in [f["path"] for f in body["files"]]
    assert json.loads(dash.files[remote]) == {"test_account": True, "allowed_hosts": ["127.0.0.1", "localhost"],
                                              "password_a": "pw-a-1234"}  # not B's, not the admin's
    suite = dash.files["in/s.yaml"].decode()
    assert f"seed:{fixture_file}#password_a" in suite and "../fixtures" not in suite
    assert path.read_text().count("../fixtures/users.json") == 1  # the file on this Mac is never changed
    assert body["tunnel"]["writes"] is True  # signing in posts a form
    blob = json.dumps(body)
    assert "pw-a-1234" not in blob and "pw-b-5678" not in blob  # values travel only in the fixture file
    assert local == []


@pytest.mark.parametrize("fixture, extra, tunnel, said", [
    # planted fault: a fixture that does not say it is a test account
    ({"allowed_hosts": ["127.0.0.1"], "password_a": "x"}, ACCOUNT, TUNNEL, "not a test-user fixture"),
    ({"test_account": True, "password_a": "x"}, ACCOUNT, TUNNEL, "allowed_hosts"),
    (None, ACCOUNT, TUNNEL.replace("  data: local\n", ""), "data: local"),
    (None, ACCOUNT, "", "goes to the cloud through tunnel: with data: local"),
])
def test_a_seeded_account_that_breaks_a_rule_is_refused_and_nothing_is_sent(dash, local, capsys, tmp_path, fixture,
                                                                            extra, tunnel, said):
    _, path = seeded(tmp_path, fixture=fixture, extra=extra, tunnel=tunnel)
    assert cli.main(["run", str(path), "--runner", "cloud"]) == cli.EXIT_CONFIG
    err = capsys.readouterr().err
    assert said in err and "pw-" not in err
    assert dash.calls == [] and local == []


def test_a_seeded_account_on_a_production_host_is_refused(dash, local, capsys, tmp_path):
    # planted fault: a production base_url. The suite itself refuses a password for a host that is not a local dev host.
    repo, _ = seeded(tmp_path)
    path = suite_file(repo / "qa", base="https://shop.example", name="prod.yaml",
                      extra=ACCOUNT.replace("{url: /login}", "{url: https://shop.example/login}") + TUNNEL)
    assert cli.main(["run", str(path), "--runner", "cloud"]) == cli.EXIT_CONFIG
    assert "local dev host" in capsys.readouterr().err
    assert dash.calls == [] and local == []


def test_a_project_account_with_a_keychain_password_stays_out_of_the_cloud(dash, local, capsys, tmp_path):
    repo = checkout(tmp_path)
    (repo / ".qajev").mkdir()
    path = repo / ".qajev" / "project.toml"
    path.write_text('name = "shop"\nrunner = "cloud"\naccount = "a"\n[env.local]\nbase_url = "http://127.0.0.1:8871"\n'
                    'tunnel = { ports = { 8871 = { command = "node serve-folder.mjs" } }, data = "local" }\n'
                    '[accounts.a]\nemail = "qa@example.test"\npassword = "keychain:shop/qa"\n'
                    'login = { url = "/login" }\n'
                    '[[objective]]\nname = "home"\nurl = "/"\nabout = "it loads"\nexpect = { text = ["Hi"] }\n')
    assert cli.main(["run", "--project", str(path), "--suite", "core"]) == cli.EXIT_CONFIG
    assert "never a keychain: reference" in capsys.readouterr().err
    assert dash.calls == [] and local == []


def test_a_project_account_from_the_seed_fixture_goes_through_the_tunnel(dash, local, tmp_path):
    repo, _ = seeded(tmp_path)
    (repo / ".qajev").mkdir()
    path = repo / ".qajev" / "project.toml"
    path.write_text('name = "shop"\nrunner = "cloud"\naccount = "b"\n[env.local]\nbase_url = "http://127.0.0.1:8871"\n'
                    'tunnel = { ports = { 8871 = { command = "node serve-folder.mjs" } }, data = "local" }\n'
                    '[accounts.b]\nemail = "qa-b@example.test"\npassword = "seed:fixtures/users.json#password_b"\n'
                    'login = { url = "/login" }\n'
                    '[[objective]]\nname = "home"\nurl = "/"\nabout = "it loads"\nexpect = { text = ["Hi"] }\n')
    assert cli.main(["run", "--project", str(path), "--suite", "core"]) == 0
    fixture_file = str((repo / "fixtures" / "users.json").resolve())
    assert json.loads(dash.files[cloud.seed_remote(fixture_file)])["password_b"] == "pw-b-5678"
    assert f'"seed:{fixture_file}#password_b"' in dash.files["in/project.toml"].decode()  # anchored to the repo
    assert "password_a" not in dash.files[cloud.seed_remote(fixture_file)].decode()
    assert local == []
