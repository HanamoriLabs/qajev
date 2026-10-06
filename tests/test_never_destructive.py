"""Production is read-only. Destructive is never. (José, 6 Oct: QAJev 0.4.0's top rule.)

Live, in QAJev's own throwaway headless Chrome: the fixture server listens on 127.0.0.1 only, and this Chrome maps
the production-looking name shop.example.com to it (--host-resolver-rules), so the same page is served once as
production and once as a local dev host. Nothing leaves the machine.
"""

import asyncio
import http.server
import json
import os
import subprocess
import sys
import threading
import time
from functools import partial
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("QAJEV_LIVE") != "1", reason="set QAJEV_LIVE=1 (starts a local Chrome)")

SITE = Path(__file__).parent / "fixtures" / "site"
PROD = "shop.example.com"
REFUSED = "QAJev never changes a production site: shop.example.com is not a local dev host."


class Server(http.server.SimpleHTTPRequestHandler):
    """The fixture site; every request that would change something is recorded as it reaches the server."""

    writes = []

    def log_message(self, *_):
        pass

    def _write(self):
        Server.writes.append((self.command, self.path))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    do_POST = do_PUT = do_PATCH = do_DELETE = _write


@pytest.fixture(scope="module")
def port():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Server, directory=str(SITE)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    from qajev import chrome

    old = os.environ.get("QAJEV_CHROME_FLAGS")
    os.environ["QAJEV_CHROME_FLAGS"] = f"{old or ''} '--host-resolver-rules=MAP {PROD} 127.0.0.1'".strip()
    try:
        record = chrome.start(f"never-destructive-{os.getpid()}", headless=True, ephemeral=True)
    finally:
        if old is None:
            os.environ.pop("QAJEV_CHROME_FLAGS", None)
        else:
            os.environ["QAJEV_CHROME_FLAGS"] = old
    yield record
    chrome.stop(record["state_key"])


def open_session(port, browser, **guard_opts):
    from qajev import session as session_mod
    from qajev.ledger import Ledger

    session_mod.configure_env(browser["cdp_url"])
    s = session_mod.Session(Ledger(0.0), headless=True, hosts={f"{PROD}:{port}", f"127.0.0.1:{port}"},
                            guard_opts=guard_opts)
    s.set_device({"width": 1280, "height": 900, "mobile": False, "scale": 1})
    return s


@pytest.fixture
def session(port, browser):
    from qajev import session as session_mod

    made = []

    def make(**guard_opts):
        made.append(open_session(port, browser, **guard_opts))
        return made[-1]

    yield make
    for s in made:
        s.close()
    session_mod.stop_daemon()


def marks(s):
    return s.evaluate("Object.fromEntries([...document.querySelectorAll('button')].map("
                      "(e) => [e.id, e.dataset.qajevGuard || 'shown']))")


def writes_after(s, before):
    s.evaluate("window.tryAll()")  # evaluate waits for the promise
    time.sleep(0.3)
    return Server.writes[before:]


def prod(port, path="/destructive.html"):
    return f"http://{PROD}:{port}{path}"


def local(port, path="/destructive.html"):
    return f"http://127.0.0.1:{port}{path}"


# ---- R3: on production, in every mode, whatever allow and allow_requests say ----

@pytest.mark.parametrize("mode", ["readonly", "mutate"])
def test_production_hides_destructive_controls_even_when_a_run_allows_them(session, port, mode):
    s = session(allow=["Delete", "Refund", "Archive"])
    s.arm(mode)
    s.navigate(prod(port))
    got = marks(s)
    assert got["del"].startswith("destructive") and got["refund"].startswith("destructive"), got
    assert got["archive"].startswith("destructive"), got
    assert got["delacct"] != "shown", got  # DENY: `allow` never un-hides it either
    assert got["save"].startswith("read-only"), got  # a production page is read-only even in a mutate run


@pytest.mark.parametrize("mode", ["readonly", "mutate"])
def test_production_never_sends_delete_put_or_patch_and_posts_only_what_is_allowed(session, port, mode):
    s = session(allow_requests=[r"/api/item", r"/api/search"])
    s.arm(mode)
    s.navigate(prod(port))
    before = len(Server.writes)
    sent = writes_after(s, before)
    assert [m for m, _ in sent] == ["POST"], sent  # only the allowed POST; DELETE/PUT/PATCH never, allowed or not
    probe = s.probe({})["probe"]
    assert {b["method"] for b in probe["blocked"]} >= {"DELETE", "PUT", "PATCH"}
    assert [(a["method"], a["url"].rsplit("/", 2)[-2:]) for a in probe["allowed"]] == [("POST", ["api", "search"])]


def test_production_posts_nothing_without_allow_requests(session, port):
    s = session()
    s.arm("mutate")
    s.navigate(prod(port))
    before = len(Server.writes)
    assert writes_after(s, before) == []
    s.evaluate("document.getElementById('f').requestSubmit()")
    time.sleep(0.3)
    assert Server.writes[before:] == []


def test_production_dismisses_confirm_prompt_and_beforeunload_and_records_them(session, port):
    s = session()
    s.arm("mutate")
    s.navigate(prod(port))
    s.evaluate("window.ask()")
    time.sleep(0.5)
    assert s.evaluate("window.asked") == {"confirm": "false", "prompt": "null"}
    assert s.navigate(prod(port, "/index.html")) is None  # beforeunload does not hold the tab on the page
    probe_kinds = {d["kind"] for d in s.probe({})["probe"]["dialogs"]}
    assert {"confirm", "prompt", "beforeunload"} <= probe_kinds


def test_production_keys_never_delete_outside_a_text_field(session, port):
    s = session()
    s.arm("mutate")
    s.navigate(prod(port))
    s.evaluate("document.activeElement && document.activeElement.blur()")
    s.press("Delete")
    s.press("Backspace")
    assert s.evaluate("window.keys") == []  # the page never sees them
    s.evaluate("document.getElementById('name').focus()")
    s.press("Backspace")
    assert s.evaluate("window.keys") == ["Backspace@name"]  # editing a text field still works


def test_production_closes_a_modal_with_escape_though_its_bare_cancel_is_hidden(session, port):
    s = session()
    s.arm("readonly")
    s.navigate(prod(port))
    s.evaluate("document.getElementById('modal').showModal()")
    assert marks(s)["cancel"].startswith("destructive")  # a bare Cancel says nothing about what it cancels
    s.press("Escape")
    assert s.evaluate("document.getElementById('modal').open") is False  # Escape still reaches the page: no dead end


@pytest.mark.parametrize("where", ["local", "production"])
def test_a_harmless_reset_clear_or_cancel_stays_and_one_that_names_what_matters_goes(session, port, where):
    s = session()
    s.arm("mutate")
    s.navigate(local(port) if where == "local" else prod(port))
    got = marks(s)
    assert got["cs"] == got["ce"] == "shown", got  # cancel a search, an edit: nothing is lost
    for name in ("co", "raf", "mix"):  # an order, an account, or another destructive word on the same control
        assert got[name].startswith("destructive"), (name, got)
    # Reset filters: shown locally; on production a read-only run hides every control that starts with "reset"
    assert got["rf"] == "shown" if where == "local" else got["rf"].startswith("read-only"), got


# ---- R1/R4: a local dev host ----

def test_local_mutate_still_writes_but_destructive_controls_wait_for_allow_destructive(session, port):
    s = session()
    s.arm("mutate")
    s.navigate(local(port))
    got = marks(s)
    assert got["save"] == "shown" and got["del"].startswith("destructive"), got  # QA of a local app; Delete held
    before = len(Server.writes)
    assert {m for m, _ in writes_after(s, before)} == {"DELETE", "PUT", "PATCH", "POST"}
    s2 = session(allow_destructive=True)
    s2.arm("mutate")
    s2.navigate(local(port))
    assert marks(s2)["del"] == "shown"


def test_a_local_mutate_run_is_read_only_on_a_production_page_it_reaches(session, port):
    s = session()
    s.arm("mutate")
    s.navigate(local(port))
    s.navigate(prod(port))  # the guard decides per page origin
    before = len(Server.writes)
    assert writes_after(s, before) == []
    assert marks(s)["save"].startswith("read-only")


# ---- R2/R4: every entry point refuses before Chrome starts ----

def qajev(*args, env=None):
    # QAJEV_CHROME points nowhere: a refusal must come before Chrome is started, or this says "Chrome not found"
    return subprocess.run([sys.executable, "-m", "qajev", *args], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "QAJEV_CHROME": "/nonexistent/chrome", **(env or {})})


def refused(p):
    out = json.loads(p.stdout)
    assert p.returncode == 5, (p.returncode, p.stderr)
    assert out["outcome"] == "refused" and REFUSED in out["reason"], out
    return out


def test_check_refuses_mutate_or_allow_destructive_on_production(port):
    refused(qajev("check", prod(port), "--expect-text", "Items", "--mode", "mutate", "--json", "--quiet"))
    refused(qajev("check", prod(port), "--expect-text", "Items", "--allow-destructive", "--json", "--quiet"))


def mutate_suite(port):
    return (f"name: s\nbase_url: http://{PROD}:{port}\nscenarios:\n  - name: wipe\n    url: /destructive.html\n"
            "    mode: mutate\n    expect: {text: [Items]}\n")


def test_a_suite_or_a_project_that_mutates_production_is_refused(port, tmp_path):
    suite = tmp_path / "s.yaml"
    suite.write_text(mutate_suite(port))
    refused(qajev("run", str(suite), "--json", "--quiet"))
    suite.write_text(f"name: s\nbase_url: http://{PROD}:{port}\nallow_destructive: true\nscenarios:\n"
                     "  - name: look\n    url: /destructive.html\n    expect: {text: [Items]}\n")
    refused(qajev("run", str(suite), "--json", "--quiet"))
    proj = tmp_path / "p.toml"
    proj.write_text(f'name = "p"\ndefault_env = "prod"\n\n[env.prod]\nbase_url = "http://{PROD}:{port}"\n'
                    'mode = "mutate"\n\n[[objective]]\nname = "o"\nurl = "/destructive.html"\n'
                    'expect = { text = ["Items"] }\n')
    refused(qajev("run", "--project", str(proj), "--json", "--quiet"))


def test_mcp_tools_refuse_mutate_and_allow_destructive_on_production(port, tmp_path, monkeypatch):
    from qajev import mcp_server

    monkeypatch.setenv("QAJEV_CHROME", "/nonexistent/chrome")
    suite = tmp_path / "s.yaml"
    suite.write_text(mutate_suite(port))
    proj = tmp_path / "p.toml"
    proj.write_text(f'name = "p"\ndefault_env = "prod"\n\n[env.prod]\nbase_url = "http://{PROD}:{port}"\n'
                    'mode = "mutate"\n\n[[objective]]\nname = "o"\nurl = "/destructive.html"\n'
                    'expect = { text = ["Items"] }\n')
    for result in (asyncio.run(mcp_server.qa_check(prod(port), None, expect_text=["Items"], mode="mutate")),
                   asyncio.run(mcp_server.qa_check(prod(port), None, expect_text=["Items"], allow_destructive=True)),
                   asyncio.run(mcp_server.qa_run_suite(None, suite_path=str(suite))),
                   asyncio.run(mcp_server.qa_project_run(str(proj), None))):
        assert result["outcome"] == "refused" and REFUSED in result["reason"], result
