"""Live checks against local fixture pages only (127.0.0.1), in QAJev's own throwaway headless Chrome.

QAJEV_LIVE=1      guard, deafness, read-only, smoke: no model calls.
QAJEV_LIVE_JEV=1  plus one end-to-end Jev check (a few paid TypeSafe decisions, about $0.01).
"""

import http.server
import json
import os
import threading
import time
from functools import partial
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("QAJEV_LIVE") != "1", reason="set QAJEV_LIVE=1 (starts a local Chrome)")

SITE = Path(__file__).parent / "fixtures" / "site"
ENV_FILE = os.environ.get("QAJEV_TEST_ENV_FILE", str(Path.home() / "Development/jev-ultrafast/.env"))


class Recorder(http.server.SimpleHTTPRequestHandler):
    requests = []

    def log_message(self, *_):
        pass

    def do_POST(self):
        Recorder.requests.append(("POST", self.path))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def _download_redirect(self):
        if self.path.startswith("/download?platform="):  # a "Download for Mac" link: a redirect to a file
            self.send_response(302)
            self.send_header("location", "/files/app.dmg")
            self.end_headers()
            return True
        return False

    def end_headers(self):
        if self.path == "/csp.html":  # report-only can only come from a header, not a <meta>
            self.send_header("Content-Security-Policy-Report-Only", "img-src 'self'")
        super().end_headers()

    def do_GET(self):
        Recorder.requests.append(("GET", self.path))
        if not self._download_redirect():
            super().do_GET()

    def do_HEAD(self):
        Recorder.requests.append(("HEAD", self.path))
        if not self._download_redirect():
            super().do_HEAD()


@pytest.fixture(scope="module")
def site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Recorder, directory=str(SITE)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    from qajev import chrome

    record = chrome.start(f"selftest-{os.getpid()}", headless=True, ephemeral=True)
    yield record
    chrome.stop(record["state_key"])
    assert not Path(record["profile_dir"]).exists(), "ephemeral profile must be deleted"


@pytest.fixture(scope="module")
def session(site, browser):
    from qajev import session as session_mod
    from qajev.ledger import Ledger

    session_mod.configure_env(browser["cdp_url"])
    s = session_mod.Session(Ledger(0.0), headless=True, hosts={site.split("//")[1]})
    s.set_device({"width": 1280, "height": 900, "mobile": False, "scale": 1})
    yield s
    s.close()
    session_mod.stop_daemon()


def heard(session, seconds=5.0):
    """What the page's mic handler wrote, once it has written something (the fake fires asynchronously)."""
    deadline = time.monotonic() + seconds
    text = ""
    while time.monotonic() < deadline:
        text = session.evaluate("document.getElementById('heard').textContent")
        if text:
            break
        time.sleep(0.1)
    return text


def hidden(session, selector):
    return session.evaluate(f"(() => {{ const e=document.querySelector({json.dumps(selector)}); "
                            f"return !!e && !e.checkVisibility(); }})()")


def test_guard_hides_danger_before_jev_sees_the_first_page(session, site):
    session.arm("readonly")
    assert session.navigate(site + "/") is None
    state = session.require_guard()
    assert state["deaf"] is True and state["mode"] == "readonly"
    for selector in ("#danger button:nth-of-type(1)", "#danger button:nth-of-type(2)",
                     "#danger button:nth-of-type(3)", "#save", 'a[href^="https://example.com"]'):
        assert hidden(session, selector), selector
    assert not hidden(session, 'a[href="/pricing.html"]')
    assert session.evaluate("document.getElementById('pw').disabled") is True
    # Jev's own element table must not offer them either.
    session.observe()
    labels = " | ".join(a["label"] for a in session.agent.state["page"]["actions"])
    for word in ("Sign out", "Close all", "Delete account", "Partner site", "Save"):
        assert word not in labels, labels
    assert "Pricing" in labels


def test_guard_holds_on_a_page_reached_by_a_click_and_on_late_controls(session, site):
    session.arm("readonly")
    session.navigate(site + "/")
    session.run_hook({"click": 'a[href="/pricing.html"]'}, site + "/")
    deadline = time.monotonic() + 10
    while "/pricing.html" not in session.evaluate("location.href") and time.monotonic() < deadline:
        time.sleep(0.1)
    assert "/pricing.html" in session.evaluate("location.href")
    session.require_guard()  # the new document was guarded by the init script, not by our evaluate
    time.sleep(0.8)  # the fixture injects a Sign out button at 300 ms
    assert hidden(session, "#late-signout")
    assert hidden(session, "#pricing-buy, [data-plan=pro] button")


def test_hooks_refuse_guarded_controls_and_secret_fields_are_disabled(session, site):
    from qajev.session import HookFailed

    session.arm("readonly")
    session.navigate(site + "/")
    with pytest.raises(HookFailed, match="guard hid this control"):
        session.run_hook({"click": "#danger button"}, site + "/")
    assert session.evaluate("window.__pressed || null") is None


def test_read_only_blocks_writes_and_records_them(session, site):
    session.arm("readonly")
    session.navigate(site + "/")
    before = [r for r in Recorder.requests if r[0] == "POST"]
    session.evaluate("document.getElementById('stats').click()")
    time.sleep(0.3)
    assert session.evaluate("document.getElementById('stats').textContent") == "blocked"
    probe = session.probe({})["probe"]
    assert probe["blocked"] and probe["blocked"][0]["method"] == "POST"
    assert [r for r in Recorder.requests if r[0] == "POST"] == before, "no POST may reach the server"


def test_mutate_mode_lets_writes_through_on_loopback(session, site):
    session.arm("mutate")
    session.navigate(site + "/")
    session.evaluate("document.getElementById('stats').click()")
    time.sleep(0.3)
    assert session.evaluate("document.getElementById('stats').textContent") == "posted"
    assert ("POST", "/api/stats") in Recorder.requests
    assert hidden(session, "#danger button:nth-of-type(1)"), "danger stays hidden in every mode"


def test_the_page_is_deaf(session, site):
    session.arm("readonly")
    session.navigate(site + "/")
    assert hidden(session, "#mic"), "mic controls are hidden when no transcript is supplied"
    session.evaluate("document.getElementById('mic').click()")
    assert heard(session) == "Mic error: no-speech"
    refused = session.evaluate("navigator.mediaDevices.getUserMedia({audio:true}).then(()=>'granted',e=>e.name)")
    assert refused == "NotAllowedError"


def test_fake_speech_is_heard_and_survives_navigation(session, site):
    session.arm("readonly", speech="What's on today?")
    session.navigate(site + "/pricing.html")
    session.navigate(site + "/")
    assert not hidden(session, "#mic")
    session.run_hook({"click": "#mic"}, site + "/")
    assert heard(session) == "Heard: What's on today?"


def test_errors_are_collected_as_findings(session, site):
    from qajev import verdict

    session.arm("readonly")
    session.navigate(site + "/broken.html")
    time.sleep(0.3)
    probe = session.probe({})["probe"]
    found = verdict.findings_from_probe(probe, scenario="b", url=site, first_party_hosts={site.split("//")[1]})
    kinds = {f["kind"] for f in found}
    assert "page error" in kinds and ({"failed to load", "HTTP 404"} & kinds)


def test_csp_blocks_and_report_only_violations_are_findings(session, site):
    # Live: a smoke of 9 sites could not say whether a tag manager's pixels hit a CSP; violations never reach
    # console.error, and the request failures they cause are third-party, so nothing was recorded.
    from qajev import verdict

    session.arm("readonly")
    session.navigate(site + "/csp.html")
    time.sleep(0.5)
    probe = session.probe({})["probe"]
    found = verdict.findings_from_probe(probe, scenario="c", url=site, first_party_hosts={site.split("//")[1]})
    got = {(f["severity"], f["kind"], f["detail"]) for f in found}
    assert ("S2", "blocked by CSP", "connect-src: http://127.0.0.2:9/collect") in got
    assert ("S3", "CSP violation (report-only)", "img-src: http://127.0.0.2:9/pixel.gif") in got


def test_smoke_crawls_and_lints_the_fixture(site, browser, tmp_path):
    import subprocess
    import sys

    from qajev import chrome

    def tabs():  # page tabs only: Chrome's component extensions add workers on their own schedule
        return {t["id"] for t in chrome.targets(browser["cdp_url"]) if t.get("type") == "page"}

    tabs_before = tabs()
    out = subprocess.run(
        [sys.executable, "-m", "qajev", "smoke", site + "/", "--cdp-url", browser["cdp_url"], "--out", str(tmp_path),
         "--json", "--quiet", "--load-high", "0", "--check-links"],
        capture_output=True, text=True, timeout=180,
    )
    report = json.loads(out.stdout)
    pages = {r["name"]: r for r in report["scenarios"]}
    assert {"/", "/pricing.html", "/contact.html", "/broken.html", "/missing.html"} <= set(pages)
    assert pages["/missing.html"]["outcome"] == "fail"
    assert pages["/broken.html"]["outcome"] == "fail"
    assert pages["/pricing.html"]["outcome"] == "pass"
    download = pages["/download?platform=mac"]  # QAJev refuses downloads: the link is described, not failed
    assert download["outcome"] == "pass" and download["stop"] == "download", download
    assert pages["/"]["outcome"] == "pass" and sum(1 for r in report["scenarios"] if r["name"] == "/") == 1
    kinds = {(f["scenario"], f["kind"]) for f in report["findings"]}
    assert ("/contact.html", "form fields without a label") in kinds
    assert ("/contact.html", "buttons without an accessible name") in kinds
    contact = [f for f in report["findings"] if f["scenario"] == "/contact.html"]
    (no_alt,) = [f for f in contact if f["kind"] == "images without alt"]
    assert "1 visible image(s)" in no_alt["detail"] and "logo.svg" in no_alt["detail"]
    assert "pixel" not in no_alt["detail"]  # a hidden tracking pixel needs no alt text
    assert not [f for f in report["findings"] if f["kind"] == "duplicate ids"]  # empty id="" is not a duplicate
    assert report["gate"] == "FAIL" and out.returncode == 1
    assert report["cost"]["calls"] == {"typesafe": 0, "text": 0}
    assert (tmp_path / Path(report["run_dir"]).name / "report.md").exists()
    assert tabs() <= tabs_before, "the run must close its own tabs"


def test_a_stored_test_account_signs_in_before_the_scenarios_and_its_password_goes_nowhere(site, browser, tmp_path):
    # The sign-in form posts (the read-only guard would block it), so QAJev signs in itself, unguarded, in its own
    # tab; the scenarios then run guarded and signed in. The password is read from the vault reference only.
    import subprocess
    import sys

    suite = tmp_path / "account.yaml"
    suite.write_text(f"""
name: account
base_url: {site}
devices: [desktop]
account:
  email: tester@example.test
  password: env:QAJEV_FIXTURE_PASS
  login: {{url: /login.html}}
scenarios:
  - name: the account page knows who is signed in
    url: /account.html
    expect: {{text: ["Signed in as tester@example.test"]}}
""")

    def run(password, out):
        env = {**os.environ, "QAJEV_FIXTURE_PASS": password}
        p = subprocess.run([sys.executable, "-m", "qajev", "run", str(suite), "--cdp-url", browser["cdp_url"],
                            "--out", str(out), "--json", "--quiet", "--load-high", "0"],
                           capture_output=True, text=True, timeout=180, env=env)
        return json.loads(p.stdout), p

    wrong, _ = run("not-the-password", tmp_path / "wrong")
    (scenario,) = wrong["scenarios"]
    assert scenario["outcome"] == "harness" and "Wrong email or password" in scenario["reason"]
    assert wrong["sign_in"]["ok"] is False  # Jev never met a sign-in page it could not pass

    good, p = run("fixture-pass-123", tmp_path / "good")
    (scenario,) = good["scenarios"]
    assert good["sign_in"]["ok"] and good["sign_in"]["email"] == "tester@example.test", good["sign_in"]
    assert scenario["outcome"] == "pass", scenario
    assert ("POST", "/api/login") in Recorder.requests
    (md,) = (tmp_path / "good").rglob("report.md")
    assert "Sign-in: as tester@example.test (account tester@example.test)" in md.read_text()
    (page,) = (tmp_path / "good").rglob("report.html")
    assert "<dt>Sign-in</dt><dd>as tester@example.test (account tester@example.test)" in page.read_text()
    written = [f.read_text(errors="replace") for f in (tmp_path / "good").rglob("*") if f.is_file()]
    assert written and not any("fixture-pass-123" in text for text in written + [p.stdout, p.stderr])


@pytest.mark.skipif(os.environ.get("QAJEV_LIVE_JEV") != "1", reason="set QAJEV_LIVE_JEV=1 (paid TypeSafe calls)")
def test_jev_reaches_pricing_end_to_end(site, browser, tmp_path):
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-m", "qajev", "check", site + "/", "--cdp-url", browser["cdp_url"], "--out", str(tmp_path),
         "--goal", "Open the pricing page. Stop when the plan prices are visible.",
         "--expect-url", "/pricing.html", "--expect-text", "$29 per month", "--max-actions", "6",
         "--cost-cap", "0.05", "--json", "--quiet", "--load-high", "0", "--env-file", ENV_FILE],
        capture_output=True, text=True, timeout=240,
    )
    report = json.loads(out.stdout)
    (scenario,) = report["scenarios"]
    assert scenario["outcome"] == "pass", scenario
    assert scenario["jev"]["actions"] >= 1
    assert report["cost"]["calls"]["typesafe"] >= 1 and report["cost"]["usd"] <= 0.05
    assert report["models"]["jev"].startswith(("typesafe", "openrouter"))
    assert out.returncode == 0


def test_text_checks_read_visible_text_and_ignore_case_is_opt_in(session, site):
    session.arm("readonly")
    session.navigate(site + "/")
    session.evaluate("document.querySelector('h1').style.textTransform = 'uppercase'")
    exact = session.probe({"text": ["QAJev fixture"]})
    anycase = session.probe({"text": ["QAJev fixture"], "ignore_case": True})
    assert exact["text"] == [False]  # a person sees "QAJEV FIXTURE"
    assert anycase["text"] == [True]



def test_text_checks_treat_line_breaks_as_spaces(session, site):
    session.arm("readonly")
    session.navigate(site + "/")
    assert session.probe({"text": ["Say what you need. We handle the rest."]})["text"] == [True]


def test_visible_means_on_screen_not_just_in_the_page(session, site):
    session.arm("readonly")
    session.navigate(site + "/pricing.html")
    probe = session.probe({"text": ["Deep footer note"], "visible": ["Deep footer note", "$29 per month"]})
    assert probe["text"] == [True] and probe["visible"] == [False, True]
    session.evaluate("document.getElementById('deep').scrollIntoView()")
    assert session.probe({"visible": ["Deep footer note"]})["visible"] == [True]


def test_a_small_decorative_progress_bar_is_not_a_spinner(session, site):
    session.arm("readonly")
    session.navigate(site + "/")
    session.probe({})
    time.sleep(1.2)
    assert session.probe({})["probe"]["blank_ms"] == 0


def test_scroll_further_moves_a_long_page_and_reports_a_locked_one(session, site):
    session.arm("readonly")
    session.navigate(site + "/pricing.html")
    assert session.scroll_further() is True
    session.navigate(site + "/locked.html")
    assert session.scroll_further() is False


REDUCED = "matchMedia('(prefers-reduced-motion: reduce)').matches"


def test_pages_are_told_the_visitor_prefers_reduced_motion(session, site):
    session.arm("readonly")
    session.navigate(site + "/")
    assert session.evaluate(REDUCED) is True  # the default, set before the first navigation


def test_motion_full_leaves_the_media_query_alone(site, browser, tmp_path):
    import subprocess
    import sys

    def check(*extra):
        out = subprocess.run(
            [sys.executable, "-m", "qajev", "check", site + "/", "--cdp-url", browser["cdp_url"], "--out",
             str(tmp_path), "--expect-js", REDUCED, "--json", "--quiet", "--load-high", "0", "--no-shots", *extra],
            capture_output=True, text=True, timeout=120,
        )
        return json.loads(out.stdout)

    reduced, full = check(), check("--motion", "full")
    assert reduced["gate"] == "PASS" and reduced["motion"] == "reduce"
    assert full["gate"] == "FAIL" and full["motion"] == "full"


def test_after_a_wheel_turn_qajev_waits_for_smooth_scrolling_to_finish(session, site):
    session.arm("readonly")
    session.navigate(site + "/smooth.html")
    session.call("Input.dispatchMouseEvent", type="mouseWheel", x=640, y=450, deltaX=0, deltaY=560)
    session.settle_scroll()
    settled = session.evaluate("scrollY")
    time.sleep(0.6)
    assert settled > 500 and session.evaluate("scrollY") == settled  # it had stopped moving: Jev looks at a still page


def test_a_foreground_run_can_be_stopped_like_a_job(site, browser, tmp_path):
    import subprocess
    import sys

    from qajev import jobs

    env = {k: v for k, v in os.environ.items() if k != "QAJEV_JOB"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "qajev", "smoke", site + "/", "--cdp-url", browser["cdp_url"], "--out", str(tmp_path),
         "--delay", "3", "--quiet", "--load-high", "0", "--no-shots"], env=env,
    )
    deadline = time.monotonic() + 30
    job = None
    while time.monotonic() < deadline and not job:
        job = next((j for j in jobs.listing() if j["pid"] == proc.pid and j["state"] == "running"), None)
        time.sleep(0.2)
    assert job, "the foreground run should list itself as a running job"
    stopped = jobs.stop(job["id"], wait=20)
    assert proc.wait(5) == 130
    assert stopped["state"] == "stopped" and stopped["exit_code"] == 130 and stopped["gate"] == "INCOMPLETE"


def test_qajev_scrolls_to_an_expected_text_that_is_just_below_the_fold(session, site):
    from qajev.runner import scroll_to_visible

    session.arm("readonly")
    session.navigate(site + "/pricing.html")
    assert session.probe({"visible": ["Deep footer note"]})["visible"] == [False]
    assist = scroll_to_visible(session, {"visible": ["Deep footer note"]})
    assert assist and assist["found"] and 1 <= assist["scrolled"] <= 6
    assert session.probe({"visible": ["Deep footer note"]})["visible"] == [True]
    session.navigate(site + "/pricing.html")
    assert scroll_to_visible(session, {"visible": ["Not on this page at all"]}) is None  # not a visibility problem


def test_downloads_are_refused_and_nothing_is_saved(session, site):
    downloads = Path.home() / "Downloads"
    before = {p.name for p in downloads.glob("app*")} if downloads.exists() else set()
    session.arm("readonly")
    error = session.navigate(site + "/download?platform=mac")  # a redirect to /files/app.dmg
    time.sleep(2)
    after = {p.name for p in downloads.glob("app*")} if downloads.exists() else set()
    assert after == before, f"a download was saved: {after - before}"
    assert error and "ABORTED" in error  # Chrome refused the download instead of saving it
