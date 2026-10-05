"""Live checks against local fixture pages only (127.0.0.1), in QAJev's own throwaway headless Chrome.

QAJEV_LIVE=1      guard, deafness, read-only, smoke: no model calls.
QAJEV_LIVE_JEV=1  plus one end-to-end Jev check (a few paid TypeSafe decisions, about $0.01).
QAJEV_LIVE_CLEF=1 plus two Clef checks with screenshots (Workers AI, under $0.01; needs the Cloudflare variables).
"""

import contextlib
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
        if self.path == "/reset.png":  # hang up without an answer: Chrome says net::ERR_EMPTY_RESPONSE
            self.close_connection = True
            return
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
                     "#danger button:nth-of-type(3)", "#save"):
        assert hidden(session, selector), selector
    assert not hidden(session, 'a[href="/pricing.html"]')
    # An off-site link (a store badge) stays on the page as visitors see it, but nothing can press it: a click at
    # its place lands on the page around it.
    partner = 'a[href^="https://example.com"]'
    assert not hidden(session, partner)
    assert session.evaluate(f"(() => {{ const a = document.querySelector({json.dumps(partner)}); const r = "
                            "a.getBoundingClientRect(); const hit = document.elementFromPoint(r.x + r.width / 2, "
                            "r.y + r.height / 2); return a.inert && !(hit && a.contains(hit)); })()") is True
    assert session.evaluate("document.getElementById('pw').disabled") is True
    # Jev's own element table must not offer them either.
    session.observe()
    labels = " | ".join(a["label"] for a in session.agent.state["page"]["actions"])
    for word in ("Sign out", "Close all", "Delete account", "Partner site", "Save"):
        assert word not in labels, labels
    assert "Pricing" in labels


def test_an_email_link_and_a_store_badge_read_as_visitors_see_them(session, site):
    # Issue #1: a Cloudflare-obfuscated address became a mailto: link once decoded, and the guard took it off the
    # page as off-site, so expectations on the address failed; store badges vanished from screenshots the same way.
    session.arm("readonly")
    session.navigate(site + "/email.html")
    session.require_guard()
    seen = session.probe({"visible": ["hello@example.test", "App Store"]})
    assert seen["visible"] == [True, True], seen
    session.observe()
    labels = " | ".join(a["label"] for a in session.agent.state["page"]["actions"])
    assert "hello@example.test" not in labels and "App Store" not in labels, labels  # still nothing to press


def test_a_js_expectation_is_judged_by_what_its_promise_settles_to(session, site):
    # SideGame1: a promise was truthy while still pending, so a check passed on a broken page. A promise is judged
    # by its value; a throw, a rejection or no answer in time is a failed check, never a pass.
    from qajev import verdict
    from qajev.session import HookFailed

    session.arm("readonly")
    session.navigate(site + "/")

    def judged(js):
        (check,) = verdict.page_checks({"js": js}, session.probe({"js": js}))
        return check

    assert judged("Promise.resolve(true)")["ok"] is True
    assert judged("Promise.resolve(false)")["ok"] is False
    thrown = judged("(() => { throw new Error('cooldownLeft of an invalid jutsu') })()")
    assert thrown["ok"] is False and "cooldownLeft" in thrown["detail"], thrown
    assert judged("Promise.reject(new Error('nope'))")["ok"] is False
    late = judged("new Promise(() => {})")
    assert late["ok"] is False and "no answer" in late["detail"], late
    for pending in ("Promise.resolve(false)", "Promise.reject(new Error('nope'))"):
        with pytest.raises(HookFailed, match="timed out"):
            session.run_hook({"wait_for": {"js": pending, "timeout": 1}}, site + "/")
    session.run_hook({"wait_for": {"js": "Promise.resolve(1)", "timeout": 1}}, site + "/")


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


def test_a_failed_load_says_why_in_chromes_words(session, site):
    # verse1: "main.js failed to load" said nothing of why. main.js itself came from the server; its three.js import
    # from a CDN had failed, so the finding now names that request and Chrome's error for it.
    from qajev import verdict

    session.arm("readonly")
    session.navigate(site + "/unloaded.html")
    time.sleep(0.5)
    probe = session.probe({})["probe"]
    found = verdict.findings_from_probe(probe, scenario="u", url=site, first_party_hosts={site.split("//")[1]},
                                        why=session.why_failed)
    details = [f["detail"] for f in found if f["kind"] == "failed to load"]
    assert any(d.startswith(f"{site}/reset.png (net::ERR_") for d in details), details
    module = next(d for d in details if d.startswith(f"{site}/mod.js"))
    assert "no network error for it; failed: " in module, module
    assert "http://127.0.0.2:9/three.module.js (net::ERR_" in module, module


def test_a_stale_move_says_what_was_in_the_way(session, site):
    # verse1's wardrobe: 27 decisions for "Character", none ran, and nothing said why. Now the decision says which
    # of Jev's checks failed (here: a popup over the button) and what changed on the page.
    from qajev.session import page_changes

    session.arm("readonly")
    session.navigate(site + "/covered.html")
    session.observe()
    page = session.agent.state["page"]
    go = next(a for a in page["actions"] if a["label"] == "Go")
    session.evaluate("document.getElementById('cover').hidden = false")
    why, now, node = session.why_stale("Target changed or is covered. Observe again.", page, {"choice": go["id"]})
    assert why["target"] == "click 'Go': covered by div#cover \"Please wait\"", why
    session.observe()
    changed = page_changes(page, session.agent.state["page"], now, node)
    assert any(c.startswith("target's text around it:") and "Please wait" in c for c in changed), changed
    assert any(c.startswith("text:") and "Please wait" in c for c in changed), changed


def test_the_guard_waits_for_react_to_hydrate(session, site):
    # FlockTab1's sign-in page (run 20261004-210258): the guard wrote `disabled` and data-qajev-guard onto the password
    # field before React hydrated it, and React's "attributes didn't match" error read as the page's own bug.
    from qajev import verdict

    session.arm("readonly")
    session.navigate(site + "/hydrate.html")  # it waits for the guard to settle, after React has hydrated
    assert session.evaluate("window.atHydration") == {"disabled": False, "marked": None}  # untouched until then
    probe = session.probe({})["probe"]
    found = verdict.findings_from_probe(probe, scenario="h", url=site, first_party_hosts={site.split("//")[1]})
    mismatch = [f for f in found if any(w in f["detail"] for w in ("did not match", "Extra attributes", "qajev"))]
    assert not mismatch and not probe.get("guard_hydration"), mismatch
    assert probe["pending"] == 0 and session.require_guard()["pending"] == 0
    marks = session.evaluate("[...document.querySelectorAll('#pw,#del,#out')].map(e => "
                             "[e.id, e.dataset.qajevGuard || null, e.disabled ?? null, e.inert])")
    assert marks[0] == ["pw", "field", True, False]  # the secret field: disabled, after hydration
    assert marks[1][:2] == ["del", "danger: Delete account"] and marks[1][3] is True  # the risky button: taken off
    assert marks[2][:2] == ["out", "off-site link"] and marks[2][3] is True  # the off-site link: inert


def test_jev_cannot_type_into_a_secret_field_while_the_guard_waits(session, site):
    # The deferral window: a card number field (Jev never lists password fields; it does list this one) is not
    # disabled until React has hydrated it. Jev may see it and choose it then, but before it acts QAJev waits for the
    # guard (require_guard: pending 0), so the move goes stale and nothing is typed.
    session.arm("readonly")
    session.call("Page.navigate", url=site + "/hydrate.html")  # not session.navigate: that would wait for the guard
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:  # loaded, React not yet hydrated: the guard is waiting on its controls
        with contextlib.suppress(RuntimeError):
            if session.evaluate("document.readyState === 'complete' && !window.atHydration && "
                                "window.__qajev.pending() > 0"):
                break
        time.sleep(0.02)
    else:
        pytest.fail("never caught the guard waiting")
    session.observe()
    page = session.agent.state["page"]
    field = next(a for a in page["actions"] if a["kind"] == "fill" and a["label"] == "Card number")  # Jev sees it
    assert session.require_guard()["pending"] == 0  # it waited until the guard had judged every control
    stale = session.jev.browser.StalePage
    with pytest.raises(stale):
        session.browser.act(field, page, text="hunter2")
    assert session.evaluate("[document.getElementById('card').disabled, document.getElementById('card').value]") == [
        True, ""]


def test_a_hook_never_clicks_a_control_the_guard_has_not_judged(session, site, monkeypatch):
    # Orchestrator's review of #25: find() waited for the guard, then clicked anyway when it was still waiting. It
    # fails closed now, as require_guard does: the scenario ends guard_missing, and the button is never clicked.
    from qajev.session import GuardMissing

    monkeypatch.setattr(session, "GUARD_WAIT", 1.0)  # the page holds the guard for 5 s; give up sooner
    session.arm("readonly")
    session.navigate(site + "/never.html")
    with pytest.raises(GuardMissing, match=r"guard still waiting on 1 control\(s\)"):
        session.run_hook({"click": "#del"}, site + "/never.html")
    assert session.evaluate("window.clicked || 0") == 0
    with pytest.raises(GuardMissing, match="guard still waiting"):  # Jev's actions: the same helper, the same stop
        session.require_guard()


def test_the_guard_names_what_it_held_back_and_the_words_that_did_it(session, site):
    # verse1, 4 Oct: a tooltip's "buy" hid the wardrobe's "Shop" button; the stuck result read as a layout bug.
    from qajev import verdict

    session.arm("readonly")
    session.navigate(site + "/shop.html")
    held = session.probe({})["probe"]["hidden_controls"]
    assert held == [{"label": "Shop Show clothes and gear you can buy", "why": "danger", "match": "buy"},
                    {"label": "Card number", "why": "secret field", "match": None},
                    {"label": "password", "why": "secret field", "match": None},
                    {"label": "Elsewhere", "why": "off-site link", "match": "elsewhere.example"}]
    assert verdict.with_guard_note("stuck", "Jev found no way forward; unmet: text 'Crown'", held) == (
        "Jev found no way forward; unmet: text 'Crown'; guard hid: 'Shop Show clothes and gear you can buy' "
        "(danger: buy), 'Card number' (secret field), 'password' (secret field) and 1 more")


def test_tap_targets_are_counted_as_wcag_2_5_8_says_and_named(session, site):
    # FlockTab1, 5 Oct: "44 target(s)" said nothing about which ones to fix, and counted links inside sentences that
    # WCAG 2.5.8 exempts.
    from qajev import smoke

    session.navigate(site + "/targets.html")
    facts = session.evaluate(smoke.FACTS)
    assert facts["small_targets"] == 5  # the crowded pair and the three fields next to them
    assert facts["small_targets_skipped"] == {"inline": 1, "spaced": 1}  # the link in a sentence; the lone "?"
    assert facts["small_target_samples"] == [
        {"tag": "button", "text": "Close", "size": "16x16", "path": "#tools button.t.close"},
        {"tag": "button", "text": "x", "size": "16x16", "path": "#tools button.t"},
        {"tag": "input", "text": "PIN", "size": "60x16", "path": "input#pin"},
        {"tag": "select", "text": "size", "size": "60x16", "path": "select"},
        {"tag": "textarea", "text": "Note", "size": "16x16", "path": "textarea"},
    ]
    for value in ("hunter2", "Medium-option-text", "typed-secret"):  # fields by their names, never what they hold
        assert value not in str(facts)
    assert smoke.small_targets(facts) == (
        "5 target(s): button 'Close' 16x16 (#tools button.t.close), button 'x' 16x16 (#tools button.t), "
        "input 'PIN' 60x16 (input#pin), select 'size' 60x16 (select), textarea 'Note' 16x16 (textarea); "
        "not counted (WCAG 2.5.8): 1 inline in a sentence, 1 with room around them")


def test_key_hooks_press_real_keys_repeat_them_and_hold_them(session, site):
    # Real-key play-tests for every game change (Orchestrator, 5 Oct): Jev cannot press a game's keys, so hooks do.
    session.navigate(site + "/keys.html")
    log = lambda: session.evaluate("window.keyLog.splice(0)")  # noqa: E731 (read and empty the page's log)

    session.run_hook({"key": "Backquote"}, site)
    assert [(e["type"], e["key"], e["code"], e["trusted"]) for e in log()] == [
        ("keydown", "`", "Backquote", True), ("keyup", "`", "Backquote", True)]  # trusted: a real key press

    session.run_hook({"key": {"press": ["f", "j"], "repeat": 5, "interval_ms": 20}}, site)
    downs = [e["code"] for e in log() if e["type"] == "keydown"]
    assert downs == ["KeyF", "KeyJ"] * 5  # alternating, ten presses

    session.run_hook({"key": {"press": "Space", "hold_ms": 400}}, site)
    held = log()
    assert [(e["type"], e["code"]) for e in held] == [("keydown", "Space"), ("keyup", "Space")]
    assert 380 <= held[1]["at"] - held[0]["at"] < 1500, held  # down for the whole hold, then released


def test_a_react_hook_answers_a_cue_at_human_speed_and_saves_the_frame(session, site, tmp_path):
    # SideGame1's roadside play-test (5 Oct): see the cue, wait a human 0.3 s, press and hold. Polling at 250 ms could
    # not answer a 0.6 s flash on time; a react hook polls the page every 50 ms.
    session.navigate(site + "/keys.html")
    session.evaluate("window.keyLog.length = 0; window.cueAt = performance.now() + 400; true")
    session.shot_dir, session.shot_prefix, session.react_log = tmp_path, "cue", []
    policy = ("(() => { const now = performance.now(); if (now < window.cueAt) return null;"
              " if (!window.downAt) { if (now - window.cueAt < 300) return null;"  # the human delay, in the suite
              " window.downAt = now; return [{down: 'Space'}, {shot: 'flash'}]; }"
              " if (now - window.downAt >= 600) { window.released = true; return {up: 'Space'}; } return null; })()")
    session.run_hook({"react": {"js": policy, "until": "window.released === true", "every_ms": 50, "for_s": 5}}, site)
    log, cue = session.evaluate("window.keyLog"), session.evaluate("window.cueAt")
    assert [(e["type"], e["code"], e["trusted"]) for e in log] == [("keydown", "Space", True), ("keyup", "Space", True)]
    assert 300 <= log[0]["at"] - cue < 450, log[0]["at"] - cue  # 0.3 s after the cue, within a poll or two
    assert 600 <= log[1]["at"] - log[0]["at"] < 800, log  # held as long as the policy said
    assert session.react_log[0]["shot"] == "flash" and (tmp_path / "cue-flash.jpg").stat().st_size > 1000


def test_ux_on_a_games_title_screen_raises_none_of_its_false_alarms(session, site):
    # SideGame1, 5 Oct, on a real game's title screen: a hidden live region "cut off", text under an opaque splash
    # "overlapping", a game that takes Tab "not reachable", and a hidden dev-menu h1 counted. None of those is real.
    from qajev import smoke, ux

    desktop = {"width": 1280, "height": 900, "mobile": False, "scale": 1}
    session.navigate(site + "/ux-game.html")
    notes = {n["kind"]: n for n in ux.notes(ux.measure(session, desktop))}
    # Main also called the hidden live region low contrast and cut off at 200% zoom: the one honest note is this.
    assert set(notes) == {"keyboard not measurable"}, notes
    assert notes["keyboard not measurable"]["detail"].startswith("Tab is taken by the game: all ")
    facts = session.evaluate(smoke.FACTS)
    assert (facts["h1"], facts["h1_hidden"]) == (1, 1)
    # An ordinary page that cancels Tab is no game: a keyboard user is locked out, and that is a real failure.
    session.navigate(site + "/ux-tab-form.html")
    form = {n["kind"]: n for n in ux.notes(ux.measure(session, desktop))}
    assert "keyboard blocked" in form and "keyboard not measurable" not in form, form
    assert form["keyboard blocked"]["rule"].startswith("WCAG 2.2 2.1.1")


def test_ux_measures_each_fault_with_its_rule_and_compares_pages(session, site):
    # José, 5 Oct: QA should say whether a site is easy to use and consistent. Every note here is measured.
    from qajev import ux

    desktop = {"width": 1280, "height": 900, "mobile": False, "scale": 1}
    session.navigate(site + "/ux.html")
    measured = ux.measure(session, desktop)
    notes = {n["kind"]: n for n in ux.notes(measured)}
    assert {"low text contrast", "contrast not measured", "text cut off", "text overlapping",
            "not reachable by keyboard", "no visible focus", "sideways scroll at 200% zoom", "dialog"} <= set(notes)
    assert any(x.startswith("'Faint words' 1.") for x in notes["low text contrast"]["samples"])  # #bbb on white
    assert notes["contrast not measured"]["detail"].startswith("1 text(s) image or gradient")
    assert any("'Cut off text here' (width" in x for x in notes["text cut off"]["samples"])
    assert any("'Over A text' and 'Over B text'" in x for x in notes["text overlapping"]["samples"])
    assert [x for x in notes["not reachable by keyboard"]["samples"] if "Fake button" in x]
    assert [x for x in notes["no visible focus"]["samples"] if "'Bare'" in x]
    assert not [x for x in notes["no visible focus"]["samples"] if "Fine link" in x]  # Chrome's focus ring counts
    assert "has no accessible name, is not marked modal, does not hold focus, does not close on Escape" in (
        notes["dialog"]["detail"])
    assert session.evaluate("innerWidth") == 1280  # the 200% zoom pass put the device back
    styles = (measured["facts"]["styles"])
    session.navigate(site + "/ux2.html")
    second = ux.measure(session, desktop)["facts"]["styles"]
    differs = ux.consistency([("ux", styles), ("ux2", second)])
    assert [n for n in differs if n["kind"] == "h2 style differs" and "font-size" in n["detail"]]


def test_a_secret_fields_value_never_reaches_the_reason_or_the_reports(session, site, tmp_path):
    # Orchestrator's review of #26: a record's label joined an input's value, so a filled secret field leaked it into
    # the stuck reason and both reports. The page holds the values; nothing QAJev writes may.
    from types import SimpleNamespace

    from qajev import report, verdict

    session.arm("readonly")
    session.navigate(site + "/shop.html")
    assert session.evaluate("[card.value, pw.value]") == ["4111111111111111", "hunter2"]  # the page does hold them
    probe = session.probe({})["probe"]
    held = probe["hidden_controls"]
    reason = verdict.with_guard_note("stuck", "Jev found no way forward", held)
    ledger = {"usd": 0.0, "usd_typesafe_estimated": 0.0, "usd_text": 0.0, "calls": {"typesafe": 0, "text": 0},
              "tokens": {"typesafe": 0, "text": 0}, "errors": 0, "text_cost_reported": True, "cap_usd": 1.0}
    result = {"name": "wardrobe", "url": site + "/shop.html", "goal": "Open the shop", "mode": "readonly",
              "outcome": "stuck", "reason": reason, "checks": [], "findings": [], "screens": [], "seconds": 1.0,
              "guard_hidden": probe["hidden"], "guard_hidden_controls": held}
    data = report.build(SimpleNamespace(name="leak"), [result], [ledger], browser={}, started_at=time.time(),
                        strict=False, interrupted=False, run_dir=tmp_path)
    report.write(tmp_path, data)
    (tmp_path / "probe.json").write_text(json.dumps(probe))  # what the run read from the page, too
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert {p.name for p in files} >= {"report.json", "report.md", "report.html", "probe.json"}
    assert "'password' (secret field)" in (tmp_path / "report.md").read_text()  # named, by its attributes
    for secret in ("4111111111111111", "hunter2"):
        assert secret not in reason and secret not in json.dumps(held)
        assert [p.name for p in files if secret in p.read_text(errors="replace")] == []  # 0 hits in the run folder


def test_a_hook_never_clicks_on_a_page_without_the_guard(session, site):
    # Orchestrator's review of #25: with the guard armed for the tab but absent from the page, find() clicked anyway.
    from qajev.session import GuardMissing

    session.arm("readonly")
    session.call("Page.removeScriptToEvaluateOnNewDocument", identifier=session.script_id)  # the next page: no guard
    try:
        session.navigate(site + "/never.html")
        assert session.guard_state() is None
        with pytest.raises(GuardMissing, match="guard absent on"):
            session.run_hook({"click": "#del"}, site + "/never.html")
        assert session.evaluate("window.clicked || 0") == 0
    finally:
        session.script_id = session.guard_cfg = None  # the next test arms afresh


def test_a_hydration_warning_about_only_the_guards_attributes_is_a_harness_note(session, site):
    # The fallback, if React hydrates later than the guard waits: React's warning names only what the guard set.
    session.arm("readonly")
    session.navigate(site + "/")
    session.probe({})  # empty the error buffer
    session.evaluate("console.error('Warning: Extra attributes from the server: %s%s', ['disabled', "
                     "'data-qajev-guard'], '\\n    in input'); console.error('Warning: Prop `%s` did not match. "
                     "Server: %s Client: %s%s', 'className', '\"a\"', '\"b\"', '\\n    in div')")
    # React 19: one diff that lists the guard's attribute and the page's own className stays the page's finding
    session.evaluate("console.error(\"A tree hydrated but some attributes of the server rendered HTML didn't match "
                     "the client properties.%s\", '\\n  <div\\n+   className=\"a\"\\n-   className=\"b\"\\n-   "
                     "data-qajev-guard=\"field\"\\n  >')")
    probe = session.probe({})["probe"]
    assert probe["guard_hydration"] == 1  # ours: a note, its text kept for the report
    assert probe["guard_hydration_details"][0].startswith("Warning: Extra attributes from the server: %s%s disabled,")
    assert [e["detail"][:40] for e in probe["errors"]] == [  # the page's: findings
        "Warning: Prop `%s` did not match. Server", "A tree hydrated but some attributes of t"]
    assert 'className="a"' in probe["errors"][1]["detail"]


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


@pytest.mark.skipif(os.environ.get("QAJEV_LIVE_CLEF") != "1",
                    reason="set QAJEV_LIVE_CLEF=1 with CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN (paid)")
def test_clef_reads_the_screen_to_pick_a_drawn_button_and_to_catch_a_cut_off_one(site, browser, tmp_path):
    import subprocess
    import sys

    def check(page, *args):
        out = subprocess.run(
            [sys.executable, "-m", "qajev", "check", site + page, "--cdp-url", browser["cdp_url"], "--out",
             str(tmp_path / page.strip("/")), "--max-actions", "4", "--cost-cap", "0.05", "--json", "--quiet",
             "--load-high", "0", "--env-file", ENV_FILE, *args],
            capture_output=True, text=True, timeout=240, env={**os.environ, "QAJEV_JEV_PROVIDER": "cloudflare"},
        )
        return out.returncode, json.loads(out.stdout)

    # The two buttons' labels are pixels on a canvas, the wrong one first: only the screenshot tells them apart.
    code, report = check("/canvas.html", "--vision", "--expect-url", "/pricing.html",
                         "--goal", "Open the pricing page. Stop when the page heading says Pricing.")
    (drawn,) = report["scenarios"]
    assert drawn["outcome"] == "pass" and code == 0, drawn
    assert report["models"]["decider"].startswith("Clef") and report["cost"]["usd"] <= 0.05
    # The words are in the page (a text check passes) but cut off on screen: the looks check fails the run.
    code, report = check("/clipped.html", "--expect-text", "Sign up now", "--expect-looks",
                         "The 'Sign up now' button is fully visible and its words are readable, not cut off")
    (clipped,) = report["scenarios"]
    assert clipped["outcome"] == "fail" and code == 1, clipped
    assert [(c["check"].split(":")[0], c["ok"]) for c in clipped["checks"]] == [
        ("page shows 'Sign up now'", True), ("looks", False)]


def test_imhim_names_the_outfits_screen_over_the_title_and_offers_its_way_out(session, site):
    # I'M HIM! QA, 2 Oct: OUTFITS open over the (inert) title read as TITLE MENU, so "back from outfits" passed on a
    # screen check while the wardrobe was still open, and every later menu step started inside it.
    from qajev import electron

    session.navigate(site + "/imhim-outfits.html")
    session.evaluate((Path(electron.__file__).parent / "bridges/web/adapters/imhim.js").read_text())
    obs = session.evaluate(electron.OBSERVE)
    labels = [a["label"] for a in obs["actions"]]
    assert obs["screen"] == "OUTFITS" and obs["state"]["screen"] == "OUTFITS"
    assert "Close the outfits screen (Esc)" in labels and "BACK · ESC" in labels
    assert not {"NEW GAME", "CASE FILE"} & set(labels)  # the title underneath is not offered
    assert obs["texts"][-1].startswith("OUTFITS")


def test_imhim_talk_says_where_the_conversation_is_and_offers_one_way_out(session, site):
    # 2 Oct: Clef-flash quit long talks after 3-4 Continues and Clef hesitated on a talk after a death. The adapter now
    # numbers the lines, says the talk ends by itself, and names what Continue does on a line still typing.
    from qajev import electron

    session.navigate(site + "/imhim-talk.html")
    adapter = (Path(electron.__file__).parent / "bridges/web/adapters/imhim.js").read_text()
    session.evaluate(adapter)

    def look():
        obs = session.evaluate(electron.OBSERVE)
        return obs, [a["label"] for a in obs["actions"]]

    obs, labels = look()
    assert obs["screen"] == "TALK" and obs["state"]["talk_line"] == 1
    assert labels == ["Continue: finish this line (Enter)", "Close the conversation (Esc)"]  # no second ✕ close
    assert obs["texts"][0].startswith("Conversation, line 1: Continue shows the next line; it closes by itself")
    assert "still typing" in obs["texts"][0]
    session.evaluate("nextLine('Kagemaru: The court will hear the evidence.')")  # finishes typing: same line
    obs, labels = look()
    assert obs["state"]["talk_line"] == 1 and labels[0] == "Continue: the next line (Enter)"
    session.evaluate("nextLine('Kagemaru: The court will hear the evidence.')")  # the next line
    obs, _ = look()
    assert obs["state"]["talk_line"] == 2 and obs["texts"][0].startswith("Conversation, line 2:")


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


def _qajev(*args, env=None, timeout=180):
    import subprocess
    import sys

    return subprocess.run([sys.executable, "-m", "qajev", *args], capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **(env or {})})


def test_a_run_sent_to_a_sign_in_page_says_it_needs_sign_in_and_what_to_do(site, browser, session, tmp_path):
    session.call("Network.clearBrowserCookies")  # signed out: an earlier test signed this browser in
    suite = tmp_path / "members.yaml"
    suite.write_text(f"""
name: members
base_url: {site}
devices: [desktop]
scenarios:
  - name: members see the members page
    url: /members.html
    expect: {{text: ["Members only"]}}
  - name: the sign-in page greets returning visitors
    url: /login.html
    expect: {{text: ["Welcome back"]}}
""")
    p = _qajev("run", str(suite), "--cdp-url", browser["cdp_url"], "--out", str(tmp_path), "--json", "--quiet",
               "--load-high", "0")
    report = json.loads(p.stdout)
    members, greeting = report["scenarios"]
    # Not a product failure: the run could not get past the door. Said so, with what the person can do about it.
    assert members["outcome"] == "harness" and members["reason"].startswith("needs sign-in"), members
    assert members["needs_sign_in"]["url"].endswith("/login.html?next=/members.html")
    assert greeting["outcome"] == "fail"  # a scenario that sets out to test the sign-in page itself is judged as usual
    wall = report["needs_sign_in"]
    assert wall["pages"] == [members["needs_sign_in"]["url"]]
    assert "qajev account add" in wall["next_step"] and "never ask for" in wall["next_step"]
    (md,) = tmp_path.rglob("report.md")
    assert "**Needs sign-in:** 1 page(s)" in md.read_text()

    crawl = _qajev("smoke", site + "/members.html", "--cdp-url", browser["cdp_url"], "--out", str(tmp_path / "s"),
                   "--json", "--quiet", "--load-high", "0", "--max-pages", "1", "--devices", "desktop", "--no-shots")
    smoke = json.loads(crawl.stdout)
    assert smoke["smoke"]["behind_sign_in"] == [site + "/members.html"], smoke["smoke"]
    assert "behind sign-in" in smoke["scenarios"][0]["reason"]


def test_account_add_saves_the_account_by_reference_and_proves_the_sign_in(site, browser, tmp_path):
    proj = tmp_path / "fixture.toml"
    proj.write_text(f'name = "fixture"\ndefault_env = "local"\n\n[env.local]\nbase_url = "{site}"\n\n'
                    '[[objective]]\nname = "account"\nurl = "/account.html"\n'
                    'expect = { text = ["Signed in as tester@example.test"] }\n')
    add = _qajev("account", "add", "tester", "--email", "tester@example.test", "--login-url", "/login.html",
                 "--password", "env:QAJEV_FIXTURE_PASS", "--project", str(proj), "--default", "--cdp-url",
                 browser["cdp_url"], env={"QAJEV_FIXTURE_PASS": "fixture-pass-123"})
    assert add.returncode == 0, add.stderr
    assert "ok: signed in as tester@example.test" in add.stdout
    text = proj.read_text()
    assert "[accounts.tester]" in text and "env:QAJEV_FIXTURE_PASS" in text and 'account = "tester"' in text
    assert "fixture-pass-123" not in text + add.stdout + add.stderr
    wrong = _qajev("account", "check", "tester", "--project", str(proj), "--cdp-url", browser["cdp_url"],
                   env={"QAJEV_FIXTURE_PASS": "not-the-password"})
    assert wrong.returncode == 2 and "Wrong email or password" in wrong.stderr, wrong.stderr
