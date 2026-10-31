"""Live: a fresh Chrome for each test, on local fixture pages (QAJEV_LIVE=1), in QAJev's own throwaway headless
Chromes. No model calls: the tests have no goals."""

import json
import os
import subprocess
import sys
import time

import pytest
from test_live import Recorder, site  # noqa: F401  (the fixture server)

pytestmark = pytest.mark.skipif(os.environ.get("QAJEV_LIVE") != "1", reason="set QAJEV_LIVE=1 (starts a local Chrome)")


def seed(folder, password):
    (folder / "qa_test_user.json").write_text(json.dumps(
        {"test_account": True, "allowed_hosts": ["127.0.0.1"], "password": password}))


def test_the_second_test_of_a_suite_is_still_signed_in(site, tmp_path):  # noqa: F811
    # A session cookie ends with its Chrome, so each fresh Chrome signs the suite's account in again.
    suite = tmp_path / "account.yaml"
    suite.write_text(f"""
name: fresh account
base_url: {site}
devices: [desktop]
account:
  email: tester@example.test
  password: seed:qa_test_user.json#password
  login: {{url: /login.html}}
scenarios:
  - name: the first test is signed in
    url: /account.html
    expect: {{text: ["Signed in as tester@example.test"]}}
  - name: the second test is signed in too
    url: /account.html?again
    expect: {{text: ["Signed in as tester@example.test"]}}
""")
    seed(tmp_path, "fixture-pass-123")  # the fixture site's seeded test account (login.html)
    sign_ins = Recorder.requests.count(("POST", "/api/login"))
    p = subprocess.run([sys.executable, "-m", "qajev", "run", str(suite), "--headless", "--ephemeral",
                        "--profile", f"fresh-{os.getpid()}", "--out", str(tmp_path / "out"), "--json", "--quiet",
                        "--load-high", "0"], capture_output=True, text=True, timeout=300,
                       env={**os.environ, "QAJEV_RUNNER": "local"})
    built = json.loads(p.stdout)
    assert [s["outcome"] for s in built["scenarios"]] == ["pass", "pass"], built["scenarios"]
    assert built["browser"]["fresh"] is True and built["sign_in"]["ok"] is True
    assert Recorder.requests.count(("POST", "/api/login")) - sign_ins == 2  # one sign-in in each new Chrome
    (md,) = (tmp_path / "out").rglob("report.md")
    assert "- Browser: a fresh Chrome for each test" in md.read_text()


def test_a_hung_page_is_stopped_at_its_cap_and_the_other_tests_run(site, tmp_path, monkeypatch):  # noqa: F811
    from qajev import chrome, runner
    from qajev import suite as suite_mod

    monkeypatch.setattr(runner, "CAP_MARGIN", 4.0)  # the parent keeps the cap: its children need no patch
    profile = f"cap-{os.getpid()}"
    spec = suite_mod.parse({"name": "cap", "base_url": site, "devices": ["desktop"], "scenarios": [
        {"name": "a page that never answers", "url": "/hang.html", "budget": {"seconds": 1},
         "expect": {"text": ["never"]}},
        {"name": "the test after it", "url": "/index.html", "depends_on": ["a page that never answers"],
         "expect": {"text": ["QAJev fixture"]}},
        {"name": "a quick page", "url": "/index.html", "expect": {"text": ["QAJev fixture"]}}]})
    seen, real_stop, real_group = {}, chrome.stop, runner._stop_group

    def stop(key, group=False):  # the pid of the Chrome the parent stops at the cap, read before it stops it
        if group and chrome._state_file(key).exists():
            seen["chrome"] = json.loads(chrome._state_file(key).read_text())["pid"]
        return real_stop(key, group=group)

    def stop_group(pid, **kw):
        seen["daemon"] = pid
        return real_group(pid, **kw)

    monkeypatch.setattr(chrome, "stop", stop)
    monkeypatch.setattr(runner, "_stop_group", stop_group)
    started = time.monotonic()
    built = runner.run(spec, runner.Options(out_dir=tmp_path, headless=True, ephemeral=True, profile=profile,
                                            load_high=None))
    got = {r["name"]: r for r in built["scenarios"]}
    hung = got["a page that never answers"]
    assert (hung["outcome"], hung["stop"], hung["reason"]) == ("harness", "cap", "cap 5 s hit"), hung
    assert got["the test after it"]["outcome"] == "skipped"
    assert got["a quick page"]["outcome"] == "pass", got["a quick page"]
    assert time.monotonic() - started < 50  # the page hangs for 60 s
    assert not [r for r in chrome.status(profile) if r.get("alive")], "the hung test's Chrome must be stopped"
    assert set(seen) == {"chrome", "daemon"}, seen  # both by what the chain started, never by a name
    for what, pid in seen.items():  # Chrome's whole process group, and the daemon's
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            raise AssertionError(f"{what}'s process group still runs")
    (md,) = tmp_path.rglob("report.md")
    assert "cap 5 s hit" in md.read_text()


def leak_seed(folder, email, password, totp, session):
    (folder / "qa_test_user.json").write_text(json.dumps(
        {"test_account": True, "allowed_hosts": ["127.0.0.1"], "email": email, "password": password,
         "totp_secret_base32": totp, "session": session}))


@pytest.mark.parametrize("browser", ["fresh", "shared"])
@pytest.mark.parametrize("by", ["password", "cookie"])
def test_a_page_that_prints_the_sign_in_secrets_leaves_none_in_the_report(site, tmp_path, browser, by):  # noqa: F811
    # The email (by vault reference), the password, the one-time code and the cookie value are read only where the
    # account signs in: in a fresh Chrome's own process. leak.html prints them all; the report holds none of them.
    from test_live import FIXTURE_TOTP, fixture_codes

    email, password, session = "leak-person@example.test", "fixture-leak-8842", "sess-leak-value-5c2d"
    leak_seed(tmp_path, email, password, FIXTURE_TOTP, session)
    login = ("  password: seed:qa_test_user.json#password\n  totp: seed:qa_test_user.json#totp_secret_base32\n"
             "  login: {url: /login-leak.html}\n") if by == "password" else (
             "  cookie: {name: leak-session, value: 'seed:qa_test_user.json#session', http_only: false}\n"
             "  login: {url: /leak.html, signed_in: {text: Signed in}}\n")
    suite = tmp_path / "leak.yaml"
    suite.write_text(f"""
name: leak
base_url: {site}
devices: [desktop]
browser: {browser}
account:
  email: seed:qa_test_user.json#email
{login}scenarios:
  - name: the page prints what the sign-in left
    url: /leak.html
    expect: {{text: ["This page leaks"]}}
""")
    started = time.time()
    p = subprocess.run([sys.executable, "-m", "qajev", "run", str(suite), "--headless", "--ephemeral",
                        "--profile", f"leak-{os.getpid()}", "--out", str(tmp_path / "out"), "--json", "--quiet",
                        "--load-high", "0", "--events"], capture_output=True, text=True, timeout=300,
                       env={**os.environ, "QAJEV_RUNNER": "local"})
    built = json.loads(p.stdout)
    assert built["sign_in"]["ok"] is True, built["sign_in"]
    secrets = {"email": email, "password": password, "session": session}
    if by == "password":  # the code typed: this step's or the one before (the server takes both)
        secrets |= {f"code {c}": c for t in (started, time.time()) for c in fixture_codes(FIXTURE_TOTP, t)}
    written = {str(f): f.read_text(errors="replace") for f in (tmp_path / "out").rglob("*") if f.is_file()}
    written |= {"stdout": p.stdout, "stderr (events)": p.stderr}
    leaked = sorted({(name, where) for where, text in written.items() for name, value in secrets.items()
                     if value in text})
    assert not leaked, leaked  # which value, where: never the value itself
    (report,) = (tmp_path / "out").rglob("report.json")
    assert "[redacted]" in report.read_text()  # the page did print them
