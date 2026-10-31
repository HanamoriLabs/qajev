"""Opt-in rendered fixtures on an approved box Chrome; no model or browser launcher.

Set QAJEV_UX_CDP to a dedicated browser endpoint. These tests create and close
only their own targets. Ordinary pytest runs skip them and launch no browser.
"""
import functools
import http.server
import io
import json
import os
import threading
import time
import urllib.request
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from qajev.session import Session
from qajev.ux_checks import attach

ENDPOINT = os.environ.get("QAJEV_UX_CDP")
pytestmark = pytest.mark.skipif(not ENDPOINT or os.uname().sysname == "Darwin",
                               reason="rendered UX fixtures run only on an explicitly approved box endpoint")
FIXTURES = Path(__file__).parent / "fixtures" / "ux"
PHONE = {"width": 390, "height": 844, "mobile": True, "scale": 3}


@pytest.fixture(autouse=True)
def no_model_initialization(monkeypatch):
    def forbidden(*_, **__):
        raise AssertionError("deterministic UX fixtures must not initialize Jev or a model")
    monkeypatch.setattr("qajev.session.load", forbidden)


class CDP:
    def __init__(self, endpoint):
        from websockets.sync.client import connect

        with urllib.request.urlopen(endpoint + "/json/version", timeout=5) as reply:
            version = json.load(reply)
        if "Headless" in version["Browser"]:
            assert os.environ.get("QAJEV_UX_ALLOW_HEADLESS") == "1" and os.uname().sysname == "Linux"
        self.socket = connect(version["webSocketDebuggerUrl"], max_size=32 * 2**20)
        self.sequence = 0

    def call(self, method, session_id=None, **params):
        self.sequence += 1
        request = {"id": self.sequence, "method": method, "params": params}
        if session_id:
            request["sessionId"] = session_id
        self.socket.send(json.dumps(request))
        while True:
            reply = json.loads(self.socket.recv(timeout=20))
            if reply.get("id") == self.sequence:
                if reply.get("error"):
                    raise RuntimeError(str(reply["error"]))
                return reply.get("result") or {}


@pytest.fixture(scope="module")
def site():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(FIXTURES)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def page(tmp_path):
    cdp = CDP(ENDPOINT)
    target = cdp.call("Target.createTarget", url="about:blank")["targetId"]
    sid = cdp.call("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
    session = Session.__new__(Session)
    session.browser = SimpleNamespace(call=lambda method, **params: cdp.call(method, session_id=sid, **params))
    session.device, session.net, session.guard_cfg = None, None, None
    session.jev = SimpleNamespace(cdp=cdp.call)
    session.agent, session.headless, session.script_id = None, True, None
    session.hosts, session.guard_opts = set(), {}
    session.carried, session.last_test_page = {}, None
    session.motion, session.color_scheme = "reduce", None
    session.loaded_at, session.user_agent = 0, None
    session.ux_observer, session.ux_script_id = None, None
    session.observe = lambda: None
    session.call("Page.enable")
    session.set_device(PHONE)
    def open_page(url, before_scan=None, **options):
        observer = attach(session, options, tmp_path, url)
        session.call("Page.navigate", url=url)
        session.evaluate("new Promise(r => document.readyState === 'complete' ? r() : "
                         "addEventListener('load', r, {once:true}))")
        if before_scan:
            session.evaluate(before_scan)
        if observer:
            observer.start()
        return observer
    session.open_ux = open_page
    yield session, tmp_path
    if session.ux_observer:
        (tmp_path / "ux.json").write_text(json.dumps(session.ux_observer.result, indent=2))
    cdp.call("Target.closeTarget", targetId=target)
    cdp.socket.close()


def counts(observer):
    return Counter(f["check"] for f in observer.result["findings"])


def test_continuous_updates_use_short_wait_and_still_find_small_target(page, site):
    session, folder = page
    began = time.monotonic()
    observer = session.open_ux(site + "/ux-busy.html", ux_settle_timeout_ms=125)
    assert time.monotonic() - began < 3, "a busy page must not consume the old ten-second wait"
    assert counts(observer) == {"UX-TAP": 1}
    assert observer.result["notes"] == ["page did not settle"]
    assert (folder / observer.result["findings"][0]["screenshot"]).is_file()
    observer.finish()
    assert observer.result["findings"][0]["repeats"] == 2


def test_short_wait_still_catches_real_delayed_layout_shift(page, site):
    session, folder = page
    observer = session.open_ux(site + "/ux-shift.html", ux_settle_timeout_ms=500)
    session.run_hook({"click": "#take"}, site)
    assert counts(observer) == {"UX-SHIFT": 1}
    item = observer.result["findings"][0]
    assert item["data"]["dy"] == 160
    assert (folder / item["screenshot"]).is_file()
    assert "page did not settle" in observer.result["notes"]


def test_covered_control_has_exact_geometry_and_first_crop(page, site):
    from PIL import Image

    session, folder = page
    observer = session.open_ux(site + "/ux-covered.html")
    assert counts(observer) == {"UX-COVER": 1}
    item = observer.result["findings"][0]
    assert item["element"]["key"] == "#save"
    assert item["data"]["points_covered"] == 3
    assert item["data"]["cover"]["position"] == "fixed"
    assert item["data"]["cover"]["rect"][2:] == [64, 64]
    assert (folder / item["screenshot"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    colors = set(Image.open(folder / item["screenshot"]).convert("RGB").getdata())
    assert (224, 0, 0) in colors, "control outline must be visible in the saved crop"
    assert (238, 134, 0) in colors, "cover outline must be visible in the saved crop"
    observer.finish()
    assert item["repeats"] == 2
    assert session.evaluate("innerHeight") == 844


def test_keyboard_real_resize_and_tap_outside_guard_find_both_faults(page, site):
    session, folder = page
    observer = session.open_ux(site + "/ux-keyboard.html")
    session.run_hook({"fill": {"#name": "Comic Fiesta"}}, site)
    assert counts(observer) == {"UX-KEYBOARD-MAIN": 1, "UX-KEYBOARD-DISMISS": 1}
    assert all((folder / item["screenshot"]).is_file() for item in observer.result["findings"])
    assert "768 px" in observer.result["findings"][0]["message"]
    assert "464 px" in observer.result["findings"][0]["message"]
    assert session.evaluate("innerHeight") == 844
    assert session.evaluate("document.querySelector('#name').value") == "Comic Fiesta"


def test_inserted_tray_shifts_other_control_and_names_cause(page, site):
    from PIL import Image

    session, folder = page
    captures = []
    original_capture = session.capture_region_png

    def capture(region):
        captures.append(dict(region))
        return original_capture(region)

    session.capture_region_png = capture
    observer = session.open_ux(site + "/ux-shift.html")
    session.run_hook({"click": "#take"}, site)
    assert counts(observer) == {"UX-SHIFT": 1}
    item = observer.result["findings"][0]
    assert item["data"]["dx"] == 0 and item["data"]["dy"] == 160
    assert "Take the next photo" in item["message"] and "Photos" in item["message"]
    crop = next(region for region in captures if region["name"] == "UX-SHIFT")
    # Fixture coordinates, written by hand: tray y=166..326, old control
    # y=178..230, new control y=338..390, all x=16..374.
    assert crop["x"] <= 16 and crop["y"] <= 166
    assert crop["x"] + crop["width"] >= 374 and crop["y"] + crop["height"] >= 390
    image = Image.open(io.BytesIO((folder / item["screenshot"]).read_bytes())).convert("RGB")
    scale = image.width / crop["width"]

    def strip(x, y, width=24, height=2):
        return image.crop(tuple(round(value * scale) for value in
                                (x - crop["x"], y - crop["y"],
                                 x + width - crop["x"], y + height - crop["y"])))

    red, orange = (224, 0, 0), (238, 134, 0)
    assert red in set(strip(16, 178).getdata()), "old position must have its dashed red outline in the saved crop"
    assert red in set(strip(16, 338).getdata()), "new position must have its red outline in the saved crop"
    assert orange in set(strip(16, 166).getdata()), "the cause must have its orange outline in the saved crop"


def test_near_label_date_and_money_use_the_correct_input_modes(page, site):
    session, _ = page
    observer = session.open_ux(site + "/ux-input-kind.html")
    assert counts(observer) == {"UX-INPUT-DATE": 1, "UX-INPUT-NUMBER": 1}, observer.result
    date, money = observer.result["findings"]
    assert 'First day' in date["element"]["desc"] and "2026-11-22" in date["message"]
    assert money["data"]["kind"] == "money" and money["data"]["inputmode"] == "decimal"


def test_small_target_and_primary_ellipsis_each_have_a_crop(page, site):
    session, folder = page
    observer = session.open_ux(site + "/ux-tap-target.html")
    assert counts(observer) == {"UX-TAP": 1, "UX-CLIP": 1}
    assert all((folder / item["screenshot"]).is_file() for item in observer.result["findings"])


@pytest.mark.parametrize("options", [{}, {"ux": "strict"}, {"ux_keyboard_scope": "all"},
                                     {"ux": "strict", "ux_keyboard_scope": "all"}])
def test_clean_page_all_keyboard_paths_and_actions_have_zero_findings(page, site, options):
    session, _ = page
    observer = session.open_ux(site + "/ux-clean.html", **options)
    session.run_hook({"fill": {"#name": "Comic Fiesta"}}, site)
    session.run_hook({"fill": {"#notes": "Bring the PSA slabs"}}, site)
    session.run_hook({"click": "#take"}, site)
    observer.finish()
    assert counts(observer) == {}, observer.result
    result = {"outcome": "pass", "reason": "checked"}
    observer.apply(result)
    assert result["outcome"] == "pass"
    assert session.evaluate("innerHeight") == 844


def test_all_keyboard_scope_merges_fields_per_id_and_screen(page, site):
    session, _ = page
    observer = session.open_ux(site + "/ux-keyboard.html", ux_keyboard_scope="all")
    assert counts(observer) == {"UX-KEYBOARD-MAIN": 1, "UX-KEYBOARD-DISMISS": 1}
    assert all("Show name" in item["message"] and "Venue" in item["message"] for item in observer.result["findings"])


def test_strict_and_off_modes_use_the_real_fixture_findings(page, site):
    session, _ = page
    observer = session.open_ux(site + "/ux-covered.html", ux="strict")
    result = {"outcome": "pass", "reason": "checked"}
    observer.apply(result)
    assert result["outcome"] == "fail" and result["reason"] == "ux: 1 findings"
    assert session.open_ux(site + "/ux-covered.html", ux="off") is None
    assert session.ux_observer is None


def test_input_kind_ten_replays_recover_one_forced_scan_budget_pause(page, site):
    session, folder = page
    rows = []
    pause = """(() => {
      const original = window.getComputedStyle, seen = new Set();
      window.getComputedStyle = function(el, ...rest) {
        if (el instanceof HTMLInputElement) {
          seen.add(el);
          if (seen.size === 2) {
            window.getComputedStyle = original;
            const until = performance.now() + 250;
            while (performance.now() < until) { /* one slow layout/CPU slice */ }
          }
        }
        return original.call(this, el, ...rest);
      };
    })()"""
    for index in range(10):
        observer = session.open_ux(site + "/ux-input-kind.html", before_scan=pause if index == 9 else None)
        initial = dict(counts(observer))
        skipped = list(observer.result["skipped"])
        observer.finish()
        final = dict(counts(observer))
        rows.append({"forced_pause_ms": 250 if index == 9 else 0, "initial": initial,
                     "initial_skipped": skipped, "final": final})
        assert final == {"UX-INPUT-DATE": 1, "UX-INPUT-NUMBER": 1}, rows
    (folder / "ten-replays.json").write_text(json.dumps(rows, indent=2))
    assert all(row["initial"].get("UX-INPUT-NUMBER") == 1 for row in rows[:9])
    assert "UX-INPUT-NUMBER" not in rows[9]["initial"], rows[9]
    assert any("200" in row["reason"] for row in rows[9]["initial_skipped"]), rows[9]
    assert observer.result["status"] == "incomplete"
    assert any(el["key"] == "#fee" for row in rows[9]["initial_skipped"] for el in row.get("unchecked", []))


def test_hidden_focused_field_is_reported_and_metrics_are_restored(page, site):
    session, _ = page
    observer = session.open_ux(site + "/ux-keyboard.html", ux_checks=["keyboard"])
    session.evaluate("document.querySelector('#name').style.cssText = "
                     "'position:fixed;top:600px;left:16px;width:300px;height:48px'")
    session.run_hook({"fill": {"#name": "Comic Fiesta"}}, site)
    assert counts(observer)["UX-KEYBOARD-FOCUS"] == 1, observer.result
    assert session.evaluate("innerHeight") == 844


def test_desktop_skips_keyboard_but_still_reports_cover(page, site):
    session, _ = page
    session.set_device({"width": 1280, "height": 800, "mobile": False, "scale": 1})
    observer = session.open_ux(site + "/ux-covered.html")
    assert counts(observer) == {"UX-COVER": 1}
    assert any("not a phone" in row["reason"] for row in observer.result["skipped"])


def inline(session, site, html, checks):
    return session.open_ux(site + "/ux-input-kind.html", ux_checks=checks,
                           before_scan="document.body.innerHTML = " + json.dumps(html))


@pytest.mark.parametrize("tag,preceding_controls", [("button", 0), ("a", 0), ("button", 300)])
def test_keyboard_neutral_point_never_clicks_backdrop_controls_even_past_the_cap(page, site, tag, preceding_controls):
    session, _ = page
    hidden = '<button style="position:fixed;left:-1000px;top:-1000px">Off screen</button>' * preceding_controls
    attributes = 'type="button"' if tag == "button" else 'href="#clicked-control"'
    html = (hidden + f'<{tag} id="backdrop" {attributes} style="position:fixed;inset:0;display:block;'
            'width:100%;height:100%;padding:0;margin:0;border:0;z-index:0">'
            f'<span style="position:absolute;inset:0">Backdrop control</span></{tag}>'
            '<input id="name" aria-label="Name" style="position:fixed;left:16px;top:32px;'
            'width:300px;height:48px;z-index:2">')
    observer = inline(session, site, html, ["keyboard"])
    session.evaluate("window.proofControlClicks=0;document.querySelector('#backdrop').addEventListener('click',"
                     "()=>proofControlClicks++);")
    field = observer.probe("element", "#name")
    neutral = observer.probe("neutralPoint", field["id"])
    session.run_hook({"fill": {"#name": "Seeded fixture"}}, site)
    assert {"neutral_point": neutral, "control_clicks": session.evaluate("proofControlClicks")} == {
        "neutral_point": None, "control_clicks": 0,
    }
    assert session.evaluate("document.querySelector('#name').value") == "Seeded fixture"


def test_default_keyboard_probe_sends_no_enter_event_or_app_send_action(page, site):
    session, _ = page
    inline(session, site, '<input id="message" aria-label="Message" enterkeyhint="send" '
           'style="position:fixed;left:16px;top:32px;width:300px;height:48px">', ["keyboard"])
    session.evaluate("window.proofSends=0;document.querySelector('#message').addEventListener('keydown',"
                     "event=>{if(event.key==='Enter')proofSends++;});")
    original = session.call
    enter_events = []

    def call(method, **params):
        if method == "Input.dispatchKeyEvent" and (params.get("key") == "Enter"
                                                 or params.get("windowsVirtualKeyCode") == 13):
            enter_events.append(dict(params))
        return original(method, **params)

    session.call = call
    session.run_hook({"fill": {"#message": "Keep this draft"}}, site)
    assert {"enter_events": enter_events, "send_actions": session.evaluate("proofSends")} == {
        "enter_events": [], "send_actions": 0,
    }
    assert session.evaluate("document.querySelector('#message').value") == "Keep this draft"


def test_typed_desktop_keyboard_probe_makes_no_click_or_metrics_change(page, site):
    session, _ = page
    desktop = {"width": 1280, "height": 800, "mobile": False, "scale": 1}
    session.set_device(desktop)
    observer = inline(session, site, '<input id="name" aria-label="Name" '
                      'style="position:fixed;left:16px;top:32px;width:300px;height:48px">', ["keyboard"])
    session.evaluate("window.proofFocus=0;document.querySelector('#name').addEventListener('focus',"
                     "()=>proofFocus++);")
    original_call, original_click = session.call, session.trusted_click
    metrics, clicks = [], []

    def call(method, **params):
        if method in {"Emulation.setDeviceMetricsOverride", "Emulation.clearDeviceMetricsOverride"}:
            metrics.append({"method": method, **params})
        return original_call(method, **params)

    def click(x, y):
        clicks.append([x, y])
        return original_click(x, y)

    session.call, session.trusted_click = call, click
    # Exercise the real pre-typing boundary, without the user's legitimate fill
    # action, so every recorded click or viewport change belongs to the probe.
    assert observer.probe("element", "#name")["is_text_field"] is True
    with session.ux_action("#name", typing=True, action="type into #name"):
        pass
    assert {"clicks": clicks, "metrics": metrics, "focus": session.evaluate("proofFocus")} == {
        "clicks": [], "metrics": [], "focus": 0,
    }
    assert session.device == desktop
    assert session.evaluate("({width:innerWidth,height:innerHeight})") == {"width": 1280, "height": 800}
    assert {"check": "UX-KEYBOARD", "reason": "viewport is not a phone (1280 px wide)"} in observer.result["skipped"]


@pytest.mark.parametrize("positions,expected", [([(145, 145)], 1), ([(115, 115)], 0),
                                                ([(115, 115), (175, 115)], 1)])
def test_cover_center_or_two_inset_points_boundary(page, site, positions, expected):
    session, _ = page
    button = '<button id="target" style="position:fixed;left:100px;top:100px;width:100px;height:100px">Save</button>'
    covers = ''.join(f'<span style="position:fixed;left:{x}px;top:{y}px;width:10px;height:10px;z-index:20"></span>'
                     for x, y in positions)
    observer = inline(session, site, button + covers, ["cover"])
    assert counts(observer)["UX-COVER"] == expected, observer.result


@pytest.mark.parametrize("overlay", [
    '<div style="position:fixed;inset:0;z-index:20">Modal screen</div>',
    '<div role="status" style="position:fixed;left:100px;top:100px;width:100px;height:100px;z-index:20">Saved</div>',
])
def test_cover_skips_other_screens_and_live_notices(page, site, overlay):
    session, _ = page
    target = '<button style="position:fixed;left:100px;top:100px;width:100px;height:100px">Save</button>'
    observer = inline(session, site, target + overlay, ["cover"])
    assert counts(observer) == {}, observer.result


def test_cover_rechecks_after_animation_and_does_not_report_transient_overlay(page, site):
    session, _ = page
    setup = """document.body.innerHTML = '<button ' +
      'style="position:fixed;left:100px;top:100px;width:100px;height:100px">Save</button>' +
      '<div id="cover" style="position:fixed;left:100px;top:100px;width:100px;height:100px;z-index:20"></div>';
      setTimeout(() => document.querySelector('#cover').remove(), 1200);"""
    observer = session.open_ux(site + "/ux-clean.html", ux_checks=["cover"], before_scan=setup)
    assert counts(observer) == {}, observer.result


def test_cover_scan_keeps_a_scroll_box_whose_own_scrollto_reads_x_y(page, site):
    # grailtab, 31 Oct: React Native Web gives each ScrollView element its own scrollTo({x, y, animated}). The
    # scan's restore called el.scrollTo({left, top}), which that reads as x 0, y 0, animated: the box glided to
    # the top before the end screenshot. The button's own scrollIntoView does nothing, as a page's could.
    session, _ = page
    html = ('<div id="box" style="height:600px;overflow:auto"><div style="position:relative;height:1500px">'
            '<button id="covered" style="position:absolute;top:950px;left:20px;width:200px;height:48px">'
            'Covered</button>'
            '<p id="target" style="position:absolute;top:1400px;margin:0">Target line</p></div></div>'
            '<div style="position:fixed;left:0;top:0;width:390px;height:120px;z-index:5">Bar</div>')
    rnw = """const box = document.querySelector('#box'); window.ownCalls = []; window.seen = [];
      box.scrollTo = (y, x, animated) => { window.ownCalls.push('scrollTo');
        if (typeof y !== 'number') ({x = 0, y = 0, animated = true} = y || {});
        box.scroll({top: y, left: x, behavior: animated === false ? 'auto' : 'smooth'}); };
      document.querySelector('#covered').scrollIntoView = () => window.ownCalls.push('scrollIntoView');
      box.scrollTop = box.scrollHeight;
      box.addEventListener('scroll', () => window.seen.push(box.scrollTop));"""
    observer = session.open_ux(site + "/ux-input-kind.html", ux_checks=["cover"],
                               before_scan="document.body.innerHTML = " + json.dumps(html) + ";" + rnw)
    session.evaluate("new Promise(r => setTimeout(r, 1000))")  # a smooth glide ends in about 0.5 s
    state = session.evaluate("""(() => { const box = document.querySelector('#box'), b = box.getBoundingClientRect();
      const t = document.querySelector('#target').getBoundingClientRect();
      return {top: box.scrollTop, calls: window.ownCalls, moved: Math.min(...window.seen) < 900,
              inside: t.top >= b.top && t.bottom <= b.bottom && t.bottom <= innerHeight}; })()""")
    # moved: the scan did scroll the box to look at the covered button, so the restore was really tested.
    assert state == {"top": 900, "calls": [], "moved": True, "inside": True}, (state, observer.result)


DEEP_BOX = ('<div id="box" style="height:400px;overflow:auto"><div style="position:relative;height:2000px">'
            '<input id="deep" aria-label="Deep" style="position:absolute;top:1500px;left:20px;width:200px;height:48px">'
            '</div></div>')
OWN_NO_OP = """window.ownCalls = []; window.clicks = 0; const deep = document.querySelector('#deep');
  deep.scrollIntoView = () => window.ownCalls.push('scrollIntoView');
  deep.addEventListener('click', () => window.clicks++);"""
DEEP_STATE = """(() => { const b = document.querySelector('#box').getBoundingClientRect();
  const r = document.querySelector('#deep').getBoundingClientRect();
  return {calls: window.ownCalls, clicks: window.clicks,
          inside: r.top >= b.top && r.bottom <= b.bottom && r.bottom <= innerHeight}; })()"""


def test_scroll_field_uses_the_browsers_scrollintoview_not_the_fields_own(page, site):
    # redteam L1 on #230: scrollField (the UX-INPUT crop and the keyboard check) calls the browser's scrollIntoView.
    session, _ = page
    inline(session, site, DEEP_BOX, ["cover"])
    session.evaluate(OWN_NO_OP)
    assert session.evaluate(DEEP_STATE)["inside"] is False
    assert session.evaluate("window.__qajevUx.scrollField(document.querySelector('#deep'))") is True
    assert session.evaluate(DEEP_STATE) == {"calls": [], "clicks": 0, "inside": True}


@pytest.mark.parametrize("hook", [{"click": "#deep"}, {"click_at": {"selector": "#deep", "x": 10, "y": 10}}],
                         ids=["click", "click_at"])
def test_a_click_scrolls_with_the_browsers_scrollintoview_not_the_elements_own(page, site, hook):
    # The click hooks bring the target into view first. An element's own scrollIntoView that does nothing must not
    # leave it out of view: the click would then land on whatever is under that point, or fail as covered.
    session, _ = page
    inline(session, site, DEEP_BOX, ["cover"])
    session.evaluate(OWN_NO_OP)
    session.run_hook(hook, site)
    assert session.evaluate(DEEP_STATE) == {"calls": [], "clicks": 1, "inside": True}


def test_scroll_is_not_a_content_shift_and_eight_pixels_is_exclusive(page, site):
    session, _ = page
    observer = inline(session, site, '<div style="height:2000px">'
                       '<button id="a" style="position:absolute;top:200px;height:48px">A</button>'
                       '<button id="b" style="position:absolute;top:400px;height:48px">B</button></div>', ["shift"])
    observer.probe("shiftBefore", None)
    session.evaluate("scrollBy(0,100)")
    assert not observer.probe("shiftAfter", observer.config)["findings"]
    observer.probe("shiftBefore", "#a")
    session.evaluate("document.querySelector('#b').style.transform = 'translateY(8px)'")
    assert not observer.probe("shiftAfter", observer.config)["findings"]
    observer.probe("shiftBefore", "#a")
    session.evaluate("document.querySelector('#b').style.transform = 'translateY(17px)'")
    assert len(observer.probe("shiftAfter", observer.config)["findings"]) == 1


def test_tap_rounding_checkbox_label_union_and_inline_sentence_exemption(page, site):
    session, _ = page
    observer = inline(session, site, '<button id="edge" style="width:43.5px;height:43.5px">A</button>'
                       '<button id="small" style="width:43px;height:44px">B</button>'
                       '<label for="box" style="display:inline-block;width:100px;height:48px">Accept</label>'
                       '<input id="box" type="checkbox" style="width:12px;height:12px">'
                       '<p>Read the <a href="#">terms</a> first.</p>', ["tap"])
    assert [f["element"]["key"] for f in observer.result["findings"]] == ["#small"], observer.result


def test_picker_phone_and_numeric_keyboard_exemptions(page, site):
    session, _ = page
    observer = inline(session, site, '<input aria-label="First day" readonly value="2026-11-22">'
                       '<input aria-label="Date" role="combobox" value="2026-11-22">'
                       '<input aria-label="Phone number" value="1234567">'
                       '<input aria-label="Account" value="1234567">'
                       '<input aria-label="Table fee" inputmode="decimal" value="1.50">'
                       '<input aria-label="Tables" inputmode="numeric" value="2">', ["input-kind"])
    assert counts(observer) == {}, observer.result


@pytest.mark.parametrize("attributes", [
    'aria-label="Search stock"',
    'placeholder="Search stock"',
    'type="search" aria-label="Stock"',
    'role="searchbox" aria-label="Stock"',
    'aria-label="Search price"',
    'placeholder="Search price"',
], ids=["label-stock", "placeholder-stock", "type-search", "role-searchbox", "label-price", "placeholder-price"])
@pytest.mark.parametrize("query", ["Pikachu", "151"], ids=["card-name", "numeric-query"])
def test_search_fields_keep_text_keyboards_without_hiding_numeric_controls(page, site, attributes, query):
    session, _ = page
    observer = inline(session, site, f'<input id="query" {attributes} value="{query}">'
                       '<input id="quantity" aria-label="Stock quantity" value="151">'
                       '<input id="fee" aria-label="Table fee" value="1.50">'
                       '<input id="price-filter" aria-label="Filter price" value="10">'
                       '<input id="research" aria-label="Research fee" value="2">'
                       '<input id="searcher" aria-label="Searcher fee" value="3">', ["input-kind"])
    assert [(item["check"], item["element"]["key"]) for item in observer.result["findings"]] == [
        ("UX-INPUT-NUMBER", "#quantity"), ("UX-INPUT-NUMBER", "#fee"),
        ("UX-INPUT-NUMBER", "#price-filter"), ("UX-INPUT-NUMBER", "#research"),
        ("UX-INPUT-NUMBER", "#searcher")], observer.result
    assert [item["data"]["kind"] for item in observer.result["findings"]] == [
        "number", "money", "money", "money", "money"]


def test_numeric_fields_near_search_metadata_keep_numeric_findings(page, site):
    session, _ = page
    observer = inline(session, site, '<input id="suffix" aria-label="Max price to search" value="10">'
                       '<section><h2>Search filters</h2><input id="context" value="2"></section>'
                       '<input id="search-max-price" value="10">', ["input-kind"])
    assert [(item["check"], item["element"]["key"]) for item in observer.result["findings"]] == [
        ("UX-INPUT-NUMBER", "#suffix"), ("UX-INPUT-NUMBER", "#context"),
        ("UX-INPUT-NUMBER", "#search-max-price")], observer.result


@pytest.mark.parametrize("label", [
    '<label for="price">Price</label>',
    '',
], ids=["associated-label", "aria-label"])
def test_numeric_label_wins_over_a_search_placeholder(page, site, label):
    session, _ = page
    aria = '' if label else 'aria-label="Price"'
    observer = inline(session, site, label + f'<input id="price" {aria} placeholder="Search..." value="10">',
                      ["input-kind"])
    assert [(item["check"], item["element"]["key"]) for item in observer.result["findings"]] == [
        ("UX-INPUT-NUMBER", "#price")], observer.result
    assert observer.result["findings"][0]["data"]["kind"] == "money"


def test_secondary_ellipsis_is_allowed_but_partial_hard_clip_is_reported(page, site):
    session, _ = page
    observer = inline(session, site, '<div style="font-size:14px;font-weight:400;width:60px;overflow:hidden;'
                       'text-overflow:ellipsis;white-space:nowrap">A long secondary card name</div>'
                       '<div id="hard" style="width:160px;height:30px;overflow:hidden;white-space:nowrap">'
                       '<span>A long uncut primary label</span></div>', ["clip"])
    assert counts(observer) == {"UX-CLIP": 1}, observer.result
    assert observer.result["findings"][0]["data"]["kind"] == "hard-clip"


def test_textarea_return_hint_alone_does_not_prove_keyboard_dismissal(page, site):
    session, _ = page
    observer = inline(session, site, '<textarea id="notes" enterkeyhint="done" '
                       'style="width:300px;height:100px"></textarea>', ["keyboard"])
    session.run_hook({"fill": {"#notes": "A note"}}, site)
    assert counts(observer) == {"UX-KEYBOARD-DISMISS": 1}, observer.result


def test_overlay_mark_and_unmark_do_not_count_as_page_mutations(page, site):
    session, _ = page
    observer = session.open_ux(site + "/ux-tap-target.html")
    before = observer.probe("state")["mutation_version"]
    field = observer.probe("element", "#close")
    observer.probe("mark", {"elementId": field["id"]})
    session.evaluate("new Promise(r => requestAnimationFrame(r))")
    observer.probe("unmark")
    session.evaluate("new Promise(r => requestAnimationFrame(r))")
    assert observer.probe("state")["mutation_version"] == before


def test_whole_screen_translation_is_not_an_action_layout_fault(page, site):
    session, _ = page
    html = '<div id="screen">' + ''.join(
        f'<button style="display:block;width:100px;height:48px;margin:8px">Control {index}</button>'
        for index in range(4)) + '</div>'
    observer = inline(session, site, html, ["shift"])
    observer.probe("shiftBefore", None)
    session.evaluate("document.querySelector('#screen').style.transform='translateY(100px)'")
    after = observer.probe("shiftAfter", observer.config)
    assert after["findings"] == [] and "screen transition" in after["notes"]


def test_app_ignore_attributes_suppress_only_the_named_checks(page, site):
    session, _ = page
    observer = session.open_ux(site + "/ux-tap-target.html", before_scan=
        "document.querySelector('#close').setAttribute('data-qa-ux-ignore','UX-TAP');"
        "document.querySelector('#print').setAttribute('data-qa-ux-ignore','UX-CLIP')")
    assert counts(observer) == {}, observer.result


@pytest.mark.parametrize("width", [390, 1280])
def test_generated_fixture_report_opens_crop_in_lightbox_and_closes_with_escape(page, site, width):
    from qajev import report

    session, folder = page
    observer = session.open_ux(site + "/ux-covered.html")
    result = {"name": "Covered Save", "about": "The fixture page opens.", "outcome": "pass", "reason": "loaded",
              "checks": [], "findings": [], "screens": []}
    observer.apply(result)
    data = report.build(SimpleNamespace(name="UX rendered fixture"), [result], [], browser={},
                        started_at=time.time(), strict=False, interrupted=False, run_dir=folder)
    report.write(folder, data)
    assert data["gate"] == "PASS" and data["cost"]["usd"] == 0
    session.set_device({"width": width, "height": 844, "mobile": width == 390, "scale": 1})
    session.call("Page.navigate", url=(folder / "report.html").as_uri())
    session.evaluate("new Promise(r => document.readyState==='complete' ? r() : "
                     "addEventListener('load',r,{once:true}))")
    assert "PASS · UX 1 advisory" in session.evaluate("document.body.innerText")
    point = session.evaluate("(() => { const a=document.querySelector('a.shot'); a.scrollIntoView({block:'center'}); "
                             "const r=a.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}; })()")
    session.trusted_click(point["x"], point["y"])
    session.evaluate("new Promise(r => requestAnimationFrame(r))")
    assert session.evaluate("!!document.querySelector('.lb:target')")
    assert session.evaluate("document.querySelector('.lb:target img').complete && "
                            "document.querySelector('.lb:target img').naturalWidth > 0")
    frame = session.capture_region_png({"name": "report lightbox", "x": 0, "y": 0, "width": width, "height": 844})
    (folder / f"report-{width}.png").write_bytes(frame)
    session.press("Escape")
    session.evaluate("new Promise(r => requestAnimationFrame(r))")
    assert not session.evaluate("!!document.querySelector('.lb:target')")
    assert session.evaluate("document.activeElement.matches('a.shot')")


@pytest.mark.parametrize("width,incomplete", [(390, False), (1280, False), (390, True)])
def test_paired_cloud_run_page_shows_real_ux_table_crop_and_incomplete_warning(page, site, width, incomplete):
    public = os.environ.get("QAJEV_UX_CLOUD_UI")
    if not public:
        pytest.skip("requires the paired Cloud checkout's static worker/public source")
    session, folder = page
    observer = session.open_ux(site + "/ux-covered.html")
    finding = dict(observer.result["findings"][0])
    crop = (folder / finding["screenshot"]).read_bytes()
    finding["screenshot"] = "/api/v1/runs/fixture/files/ux/cover.png"
    ux = {"mode": "advisory", "status": "incomplete" if incomplete else "complete",
          "findings": [] if incomplete else [finding], "notes": [], "skipped": [
              {"check": "UX-INPUT-NUMBER", "reason": "scan incomplete: 200 ms budget exhausted",
               "unchecked": [{"key": "#fee", "desc": 'input[text] "Table fee (RM)"'}]}] if incomplete else []}
    run = {"id": "fixture", "name": "UX renderer fixture", "project": None, "gate": "pass",
           "summary": "Local synthetic fixture", "counts": {"pass": 1, "fail": 0, "stuck": 0, "harness": 0},
           "where": {"kind": "mac", "label": "Fixture runner"}, "commands": [], "files": {},
           "tests": [{"name": "Phone form", "outcome": "pass", "about": "The fixture form opens.", "seconds": 1,
                      "wrong": [], "screenshots": [], "last_step": "", "decisions": 0, "for_agents": "", "ux": ux}]}
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            body = None
            kind = "application/json"
            if path == "/api/v1/config":
                body = b"{}"  # no Firebase SDK or credential lookup
            elif path == "/api/v1/me":
                body = b'{"email":"fixture@example.invalid"}'
            elif path == "/api/v1/overview":
                body = json.dumps({"credits": {"total": 10, "free": 10, "paid": 0},
                                   "machines": {"cap": 1, "busy": 0}, "running": {"total": 0}, "live": []}).encode()
            elif path == "/api/v1/runs/fixture":
                body = json.dumps(run).encode()
            elif path == finding["screenshot"]:
                body, kind = crop, "image/png"
            if body is None:
                return super().do_GET()
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=public))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        session.set_device({"width": width, "height": 844, "mobile": width == 390, "scale": 1})
        session.call("Page.navigate", url=f"http://127.0.0.1:{server.server_port}/run.html?id=fixture")
        until = time.monotonic() + 15
        while not session.evaluate("!!document.querySelector('.ux-section')"):
            assert time.monotonic() < until, session.evaluate("document.body.innerText")
            time.sleep(0.1)
        text = session.evaluate("document.querySelector('.ux-section').innerText")
        if incomplete:
            assert "scan incomplete" in text and "#fee" in text and "Table fee (RM)" in text
            assert "No UX findings" not in text
        else:
            assert "UX-COVER" in text and "#save" in text
            point = session.evaluate("(() => { const b=document.querySelector('.ux-table .shot[data-src]'); "
                                     "b.scrollIntoView({block:'center'}); const r=b.getBoundingClientRect(); "
                                     "return {x:r.x+r.width/2,y:r.y+r.height/2}; })()")
            session.trusted_click(point["x"], point["y"])
            session.evaluate("new Promise(r => requestAnimationFrame(r))")
            assert session.evaluate("!!document.querySelector('dialog.lightbox[open]')")
            session.press("Escape")
            session.evaluate("new Promise(r => requestAnimationFrame(r))")
            assert not session.evaluate("!!document.querySelector('dialog.lightbox[open]')")
            assert session.evaluate("document.activeElement.matches('.ux-table .shot')")
        (folder / f"cloud-ui-{width}-{incomplete}.png").write_bytes(session.capture_region_png(
            {"name": "Cloud UX fixture", "x": 0, "y": 0, "width": width, "height": 844}))
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("mode,ux_mode,outcome", [("mutate", "advisory", "pass"), ("mutate", "strict", "fail"),
                                                 ("mutate", "off", "pass"), ("readonly", "advisory", "pass")])
def test_real_guarded_scenario_entry_preserves_advisory_and_strict_policy(page, site, mode, ux_mode, outcome):
    from qajev import runner, suite

    session, folder = page
    session.hosts = {urlsplit(site).netloc}
    parsed = suite.parse({"name": "UX entry fixture", "devices": ["phone"], "mode": mode, "ux": ux_mode,
                          "scenarios": [{"name": "Covered Save", "url": site + "/ux-covered.html",
                                         "about": "The local fixture page opens.", "expect": {"text": ["New show"]}}]})
    result = runner.run_scenario(session, parsed.scenarios[0], opts=runner.Options(shots=False, load_high=None),
                                 hosts=session.hosts, run_dir=folder)
    assert result["outcome"] == outcome, result
    if ux_mode == "off":
        assert "ux" not in result
    elif mode == "mutate":
        assert Counter(f["check"] for f in result["ux"]["findings"]) == {"UX-COVER": 1}, result
        if ux_mode == "strict":
            assert result["reason"] == "ux: 1 findings"
    else:
        assert result["guard_hidden"] > 0, result
        assert result["ux"]["status"] == "incomplete", result
        assert any(el["key"] == "#save" for skip in result["ux"]["skipped"] for el in skip.get("unchecked", []))


def test_actual_smoke_examine_uses_ux_without_a_model_and_marks_guard_coverage(page, site):
    from qajev import runner, smoke

    session, folder = page
    session.hosts = {urlsplit(site).netloc}
    session.arm("readonly")
    session.configure_ux({"ux": "advisory"}, folder, "Smoke covered Save")
    assert session.navigate(site + "/ux-covered.html") is None
    result = {"name": "Smoke covered Save", "url": site + "/ux-covered.html"}
    smoke._examine(session, result, "Smoke covered Save", PHONE, settle=0,
                   host=urlsplit(site).netloc, opts=runner.Options(shots=False), run_dir=folder)
    assert result["outcome"] == "pass", result
    assert result["ux"]["mode"] == "advisory" and result["ux"]["status"] == "incomplete", result
    assert any(el["key"] == "#save" for skip in result["ux"]["skipped"] for el in skip.get("unchecked", []))


@pytest.mark.parametrize("target", [{"selector": "#move", "x": 5, "y": 5}, {"js": "({x:105,y:105})"}])
def test_click_at_resolves_the_actual_target_before_excluding_its_own_shift(page, site, target):
    session, _ = page
    setup = """document.body.innerHTML = '<button ' +
      'style="position:absolute;left:0;top:0;width:44px;height:44px">Top</button>' +
      '<button id="move" style="position:absolute;left:100px;top:100px;width:100px;height:48px">Move</button>' +
      '<button style="position:absolute;left:100px;top:300px;width:100px;height:48px">Other</button>';
      document.querySelector('#move').onclick = function() { this.style.transform='translateY(100px)'; };"""
    observer = session.open_ux(site + "/ux-input-kind.html", ux_checks=["shift"], before_scan=setup)
    session.run_hook({"click_at": target}, site)
    assert counts(observer) == {}, observer.result


def test_clear_field_tap_that_opens_no_keyboard_is_not_forced_to_focus(page, site):
    session, _ = page
    setup = """document.body.innerHTML='<input id="name" style="width:300px;height:48px" aria-label="Name">';
      document.querySelector('#name').onmousedown = event => event.preventDefault();"""
    observer = session.open_ux(site + "/ux-input-kind.html", ux_checks=["keyboard"], before_scan=setup)
    field = observer.probe("element", "#name")
    observer.keyboard(field["id"], observer.screen())
    assert counts(observer) == {}, observer.result
    assert any("could not focus" in skip["reason"] for skip in observer.result["skipped"])
    assert session.evaluate("document.activeElement.id") != "name"


@pytest.mark.parametrize("ignore", ["all", "UX-KEYBOARD-DISMISS"])
def test_ignored_keyboard_checks_do_not_trigger_their_app_handlers(page, site, ignore):
    session, _ = page
    observer = inline(session, site, f'<input id="name" data-qa-ux-ignore="{ignore}" '
                       'style="width:300px;height:48px" aria-label="Name">', ["keyboard"])
    session.evaluate("window.proofFocus=0;window.proofOutside=0;"
                     "document.querySelector('#name').onfocus=()=>proofFocus++;"
                     "document.addEventListener('mousedown',e=>{if(!e.target.closest('#name'))proofOutside++;});")
    field = observer.probe("element", "#name")
    observer.keyboard(field["id"], observer.screen())
    assert session.evaluate("proofOutside") == 0
    if ignore == "all":
        assert session.evaluate("proofFocus") == 0
    assert counts(observer) == {}, observer.result
