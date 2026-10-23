"""Behavior at the UX driver boundaries; model and browser construction stay out."""

import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_ux_dom_live import ENDPOINT, counts, inline
from test_ux_dom_live import page as ux_page
from test_ux_dom_live import site as ux_site

from qajev import runner, smoke, suite
from qajev.session import Session
from qajev.ux_checks import Observer, attach
from qajev.ux_options import resolve


class RecordingObserver:
    def __init__(self, events):
        self.events = events

    def safe(self, function, *args, **kwargs):
        return function(*args, **kwargs)

    def start(self):
        self.events.append("ux-start")

    def finish(self):
        self.events.append("ux-finish")

    def before(self, target=None, **kwargs):
        self.events.append(("ux-before", target, kwargs))
        return True

    def after(self):
        self.events.append("ux-after")

    def apply(self, result):
        self.events.append("ux-apply")
        result.update(outcome="fail", reason="ux: 1 findings", ux={"findings": [{"check": "UX-TAP"}]})


@pytest.mark.parametrize("signin_wall", [False, True])
def test_suite_runner_attaches_starts_finishes_and_applies_deterministic_observer(monkeypatch, tmp_path, signin_wall):
    events = []
    observer = RecordingObserver(events)

    class Page:
        def configure_ux(self, opts, run_dir, name, inherited):
            assert run_dir == tmp_path and inherited["ux"] == "strict"
            events.append("ux-attach")
            return observer

        def set_device(self, *_):
            pass

        def arm(self, *_):
            pass

        def check_host(self, *_):
            pass

        def navigate(self, *_):
            events.append("navigate")

        def probe(self, *_):
            return {"url": "http://127.0.0.1/", "status": 200, "text": [not signin_wall], "probe": {}}

    scenario = suite.parse(
        {
            "ux": "strict",
            "scenarios": [{"name": "loaded", "url": "http://127.0.0.1/", "settle": 0, "expect": {"text": "Ready"}}],
        }
    ).scenarios[0]
    monkeypatch.setattr(runner, "wait_for_quiet", lambda _: (True, 1))
    if signin_wall:
        monkeypatch.setattr(
            runner.verdict,
            "sign_in_wall",
            lambda *args, **kwargs: {"url": "http://127.0.0.1/signin", "reason": "behind sign-in"},
        )
    result = runner.run_scenario(
        Page(), scenario, opts=runner.Options(shots=False), hosts={"127.0.0.1"}, run_dir=tmp_path
    )
    assert events == ["ux-attach", "navigate", "ux-start", "ux-finish", "ux-apply"]
    assert result["outcome"] == "fail" and result["reason"] == "ux: 1 findings"


def test_smoke_page_starts_and_applies_deterministic_observer(monkeypatch, tmp_path):
    events = []
    session = Mock()
    session.ux_observer = RecordingObserver(events)
    session.evaluate.return_value = {"url": "http://127.0.0.1/", "status": 200}
    session.probe.return_value = {"probe": {}}
    monkeypatch.setattr(smoke, "lint", lambda *args, **kwargs: [])
    monkeypatch.setattr(smoke.ux, "measure", lambda *args: events.append("legacy-measure") or {})
    monkeypatch.setattr(smoke.ux, "notes", lambda *args: [])
    result = {"url": "http://127.0.0.1/"}
    smoke._examine(
        session,
        result,
        "home",
        {"mobile": True},
        settle=0,
        host="127.0.0.1",
        opts=SimpleNamespace(shots=False, ux="strict"),
        run_dir=tmp_path,
    )
    assert events == ["ux-start", "legacy-measure", "ux-finish", "ux-apply"]
    assert result["outcome"] == "fail" and result["ux"]["findings"][0]["check"] == "UX-TAP"


@pytest.mark.parametrize("kind,value", [("key", "Enter"), ("js", "void 0"), ("navigate", "/next"), ("reload", True)])
def test_session_hook_wraps_the_user_action_and_runs_after_even_on_failure(kind, value):
    events = []
    session = Session.__new__(Session)
    session.ux_observer = RecordingObserver(events)

    def action(*_):
        events.append("user-action")
        raise RuntimeError("test action failed")

    session._run_hook = action
    with pytest.raises(RuntimeError, match="test action failed"):
        session.run_hook({kind: value}, "http://127.0.0.1/")
    assert events[0][0] == "ux-before" and events[1:] == ["user-action", "ux-after"]
    assert events[0][1] is None


def test_session_click_uses_the_resolved_hit_point_for_its_ux_boundary():
    events = []
    session = Session.__new__(Session)
    session.ux_observer = RecordingObserver(events)
    session.settled = lambda _: {"x": 105, "y": 205}
    session.trusted_click = lambda x, y: events.append(("click", x, y))
    session.observe = lambda: None
    session.run_hook({"click": "Save label"}, "http://127.0.0.1/")
    assert events[0][1] == {"x": 105, "y": 205}
    assert events[1:] == [("click", 105, 205), "ux-after"]


@pytest.mark.parametrize("target", [{"selector": "#save", "x": 5, "y": 5}, {"js": "({x:7,y:9})"}])
def test_session_click_at_uses_the_resolved_hit_point_for_selector_and_js(target):
    events = []
    session = Session.__new__(Session)
    session.ux_observer = RecordingObserver(events)
    evaluated = iter(
        ([{"value": {"x": 7, "y": 9}}] if "js" in target else []) + [{"x": 105, "y": 205}, {"x": 105, "y": 205}]
    )
    session.evaluate = lambda *args, **kwargs: next(evaluated)
    session.trusted_click = lambda x, y: events.append(("click", x, y))
    session.observe = lambda: None
    session.run_hook({"click_at": target}, "http://127.0.0.1/")
    assert events[0][1] == {"x": 105, "y": 205}
    assert events[1:] == [("click", 105, 205), "ux-after"]


def test_session_fill_probes_keyboard_before_relocating_and_typing():
    events = []
    session = Session.__new__(Session)
    session.ux_observer = RecordingObserver(events)
    session.find = lambda _: events.append("find") or {"secret": False, "x": 105, "y": 205}
    session.fill = lambda *args: events.append("fill")
    session.observe = lambda: None
    session.run_hook({"fill": {"#name": "A show"}}, "http://127.0.0.1/")
    assert events[0] == "find" and events[1][0] == "ux-before"
    assert events[1][1] == {"x": 105, "y": 205} and events[1][2]["typing"] is True
    assert events[2:] == ["find", "fill", "ux-after"]


@pytest.mark.parametrize("action_kind", ["fill", "done", "blocked"])
def test_jev_act_uses_chosen_node_and_typing_boundary_without_constructing_a_model(action_kind):
    events = []
    session = Session.__new__(Session)
    session.ux_observer = RecordingObserver(events)
    state = {
        "decisions": [],
        "decision": {"choice": "fill-name"},
        "page": {
            "fingerprint": "current",
            "actions": [{"id": "fill-name", "node": 17, "kind": action_kind, "label": "Show name"}],
        },
    }
    session.agent = SimpleNamespace(state=state, command=lambda name, *args: events.append(name))
    session.jev = SimpleNamespace(browser=SimpleNamespace(StalePage=type("Stale", (Exception,), {})))
    session.require_guard = lambda: None
    session.settle_scroll = lambda: None
    assert session.tick() is True
    if action_kind != "fill":
        assert events == ["predict", "act"]
        return
    assert events[0] == "predict" and events[1][0] == "ux-before"
    assert events[1][1] == {"jevNode": 17} and events[1][2]["typing"] is True
    assert events[2:] == ["act", "ux-after"]


def test_attach_installs_the_probe_before_new_documents_without_model_calls(tmp_path):
    session = Mock()
    session.ux_script_id = None
    session.call.return_value = {"identifier": "owned-probe"}
    observer = attach(session, {}, tmp_path, "screen")
    assert session.ux_observer is observer and session.ux_script_id == "owned-probe"
    session.call.assert_called_once()
    assert session.call.call_args.args == ("Page.addScriptToEvaluateOnNewDocument",)
    assert "window.__qajevUx" in session.call.call_args.kwargs["source"]


def test_post_action_waits_for_quiet_before_shift_and_static_measurements(tmp_path):
    observer = Observer(Mock(), resolve({}), tmp_path, "screen")
    events = []
    observer.in_action, observer.action = True, "tap Save"
    observer.settle = lambda *args: events.append("settle")
    observer.probe = lambda method, *args: events.append(method) or {}
    observer.scan = lambda: events.append("scan")
    observer.after()
    assert events == ["settle", "shiftAfter", "scan"]
    assert observer.in_action is False


def test_keyboard_failure_clears_override_when_no_original_metrics_exist(tmp_path):
    session = Mock()
    session.device = None
    session.evaluate.return_value = {"width": 390, "height": 844, "scale": 3}
    observer = Observer(session, resolve({}), tmp_path, "screen")

    def probe(method, *args):
        if method == "snapshot":
            return "saved"
        if method == "keyboardInfo":
            return {"field": {"id": "u1", "key": "#name", "rect": [10, 10, 300, 48]}, "active_is_field": True}
        if method == "scrollField":
            raise RuntimeError("page changed")
        return {}

    observer.probe, observer.settle = Mock(side_effect=probe), Mock()
    with pytest.raises(RuntimeError, match="page changed"):
        observer.keyboard("u1", "screen")
    assert any(call.args == ("Emulation.clearDeviceMetricsOverride",) for call in session.call.call_args_list)
    assert ("restore", "saved") in [call.args for call in observer.probe.call_args_list]


def test_nested_user_action_does_not_start_a_second_probe(tmp_path):
    observer = Observer(Mock(), resolve({}), tmp_path, "screen")
    observer.in_action = True
    observer.probe = Mock()
    assert observer.before("#save", typing=True) is False
    observer.probe.assert_not_called()


def test_static_observer_does_not_request_a_model(tmp_path):
    session = Mock()
    session.guard_cfg = None
    session.evaluate.return_value = 390
    session.agent.command.side_effect = AssertionError("UX checks must not request a model")
    observer = Observer(session, resolve({}), tmp_path, "screen")
    observer.probe = Mock(return_value={"findings": []})
    observer.scan()
    session.agent.command.assert_not_called()


@pytest.mark.parametrize("input_path", ["tap", "focus", "return", "outside"])
def test_keyboard_rechecks_the_current_guard_before_each_input_path(tmp_path, monkeypatch, input_path):
    from qajev import ux_checks

    session = Mock()
    session.guard_cfg = {"mode": "readonly"}
    session.device = {"width": 390, "height": 844, "scale": 3, "mobile": True}
    session.evaluate.return_value = dict(session.device)
    inputs = []
    stop_at = 3 if input_path in {"return", "outside"} else 2
    guard_calls = 0

    def require_guard():
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == stop_at:
            raise RuntimeError("current guard was lost")

    session.require_guard.side_effect = require_guard
    session.trusted_click.side_effect = lambda *args: inputs.append("tap")
    session.call.side_effect = (
        lambda method, **kwargs: inputs.append("return") if method == "Input.dispatchKeyEvent" else {}
    )
    observer = Observer(session, resolve({"ux_keyboard_press_return": True}), tmp_path, "screen")
    field = {"id": "u1", "desc": "Name", "rect": [20, 40, 300, 48], "center_clear": input_path != "focus"}

    def probe(method, *args):
        if method == "snapshot":
            return "saved"
        if method == "keyboardInfo":
            return {
                "field": field,
                "active_is_field": input_path != "focus",
                "active_is_text_field": True,
                "return_eligible": input_path == "return",
            }
        if method == "neutralPoint" and input_path == "outside":
            return {"x": 10, "y": 10}
        if method == "focusField":
            inputs.append("focus")
        return None

    observer.probe, observer.settle = Mock(side_effect=probe), Mock()
    monkeypatch.setattr(ux_checks.time, "sleep", lambda _: None)
    observer.safe(observer.keyboard, "u1", "screen")
    assert guard_calls == stop_at
    assert inputs == (["tap"] if input_path in {"return", "outside"} else [])
    assert observer.result["status"] == "incomplete"


# The rendered additions share the fixture harness. Importing it starts no
# browser; these tests run only on an explicit box endpoint.
page, site = ux_page, ux_site

browser_only = pytest.mark.skipif(
    not ENDPOINT or os.uname().sysname == "Darwin",
    reason="approved isolated box endpoint required; Mac browsers forbidden",
)


@browser_only
def test_rendered_action_target_is_excluded_from_shift(page, site):
    session, _ = page
    observer = inline(
        session, site, '<button id="target" style="position:fixed;top:100px;height:48px">Take photo</button>', ["shift"]
    )
    observer.probe("shiftBefore", "#target")
    session.evaluate("document.querySelector('#target').style.top='200px'")
    assert observer.probe("shiftAfter", observer.config)["findings"] == []


@browser_only
def test_rendered_hidden_container_cannot_make_the_main_button_finger_reachable(page, site):
    session, _ = page
    observer = inline(
        session,
        site,
        '<div style="position:fixed;inset:0;overflow:hidden">'
        '<input id="name" aria-label="Name" style="position:absolute;top:100px;width:300px;height:48px">'
        '<button data-qa-main style="position:absolute;top:768px;width:300px;height:52px">Save</button></div>',
        ["keyboard"],
    )
    session.run_hook({"fill": {"#name": "A show"}}, site)
    assert counts(observer)["UX-KEYBOARD-MAIN"] == 1, observer.result


@browser_only
def test_rendered_ignored_main_check_never_scrolls_the_form(page, site):
    session, _ = page
    observer = inline(
        session,
        site,
        '<div id="form" style="position:fixed;inset:0;overflow:auto">'
        '<input id="name" data-qa-ux-ignore="UX-KEYBOARD-MAIN" aria-label="Name" '
        'style="position:absolute;top:100px;width:300px;height:48px">'
        '<button data-qa-main style="position:absolute;top:768px;width:300px;height:52px">Save</button></div>',
        ["keyboard"],
    )
    session.evaluate(
        "window.proofScrollWrites=0;const scrollProp=Object.getOwnPropertyDescriptor(Element.prototype,'scrollTop');"
        "Object.defineProperty(document.querySelector('#form'),'scrollTop',{"
        "get(){return scrollProp.get.call(this)},set(value){proofScrollWrites++;scrollProp.set.call(this,value)}})"
    )
    field = observer.probe("element", "#name")
    observer.probe("keyboardInfo", field["id"], {"resetMain": True})
    try:
        session.call("Emulation.setDeviceMetricsOverride", width=390, height=464, deviceScaleFactor=3, mobile=True)
        observer.probe("keyboardInfo", field["id"], {"phase": "open"})
        assert session.evaluate("proofScrollWrites") == 0
    finally:
        session.set_device({"width": 390, "height": 844, "scale": 3, "mobile": True})


@browser_only
def test_rendered_hidden_document_root_is_never_programmatically_scrolled(page, site):
    session, _ = page
    observer = inline(
        session,
        site,
        '<input id="name" aria-label="Name" style="position:absolute;top:100px">'
        '<button data-qa-main style="position:absolute;top:768px;width:100px;height:52px">Save</button>',
        ["keyboard"],
    )
    session.evaluate(
        "document.documentElement.style.overflow='hidden';document.body.style.minHeight='900px';"
        "window.proofRootScroll=0;const prop=Object.getOwnPropertyDescriptor(Element.prototype,'scrollTop');"
        "Object.defineProperty(document.scrollingElement,'scrollTop',{get(){return prop.get.call(this)},"
        "set(value){proofRootScroll++;prop.set.call(this,value)}})"
    )
    field = observer.probe("element", "#name")
    observer.probe("keyboardInfo", field["id"], {"resetMain": True})
    try:
        session.call("Emulation.setDeviceMetricsOverride", width=390, height=464, deviceScaleFactor=3, mobile=True)
        assert session.evaluate("document.scrollingElement.scrollHeight>document.scrollingElement.clientHeight+1")
        observer.probe("keyboardInfo", field["id"], {"phase": "open"})
        assert session.evaluate("proofRootScroll") == 0
    finally:
        session.set_device({"width": 390, "height": 844, "scale": 3, "mobile": True})


@browser_only
@pytest.mark.parametrize("dismiss_control", ["<button>Done</button>", "<button data-qa-dismiss-keyboard>Hide</button>"])
def test_rendered_done_control_or_explicit_dismiss_marker_is_a_non_destructive_pass(page, site, dismiss_control):
    session, _ = page
    observer = inline(
        session, site, '<textarea id="name" aria-label="Notes"></textarea>' + dismiss_control, ["keyboard"]
    )
    session.evaluate("window.proofDoneClicks=0;document.querySelector('button').onclick=()=>proofDoneClicks++")
    session.run_hook({"fill": {"#name": "A note"}}, site)
    assert counts(observer) == {}, observer.result
    assert session.evaluate("proofDoneClicks") == 0


@browser_only
@pytest.mark.parametrize("main_mode,expected", [("reachable", 0), ("visible", 1)])
def test_rendered_main_button_policy_distinguishes_reachable_from_visible(page, site, main_mode, expected):
    session, _ = page
    html = (
        '<div style="position:fixed;inset:0;overflow:auto">'
        '<input id="name" enterkeyhint="done" aria-label="Name" '
        'style="position:absolute;top:100px;width:300px;height:48px">'
        '<button data-qa-main style="position:absolute;top:768px;width:300px;height:52px">Save</button></div>'
    )
    observer = session.open_ux(
        site + "/ux-input-kind.html",
        ux_checks=["keyboard"],
        ux_keyboard_main=main_mode,
        before_scan="document.body.innerHTML=" + json.dumps(html),
    )
    session.run_hook({"fill": {"#name": "A show"}}, site)
    assert counts(observer)["UX-KEYBOARD-MAIN"] == expected, observer.result
    assert session.evaluate("document.querySelector('div').scrollTop") == 0


@browser_only
@pytest.mark.parametrize("blur_on_submit,expected", [(True, 0), (False, 1)])
def test_rendered_return_requires_actual_blur_and_prevents_form_submission(page, site, blur_on_submit, expected):
    session, _ = page
    html = (
        '<form action="/must-not-submit"><input id="name" aria-label="Name" enterkeyhint="done">'
        '<button type="submit">Submit</button></form>'
    )
    setup = (
        "document.body.innerHTML=" + json.dumps(html) + ";window.proofSubmits=0;"
        "document.querySelector('form').onsubmit=()=>{proofSubmits++;"
        + ("document.querySelector('#name').blur();" if blur_on_submit else "")
        + "};"
    )
    observer = session.open_ux(
        site + "/ux-input-kind.html", ux_checks=["keyboard"], ux_keyboard_press_return=True, before_scan=setup
    )
    initial_url = session.evaluate("location.href")
    field = observer.probe("element", "#name")
    observer.keyboard(field["id"], observer.screen())
    assert session.evaluate("location.href") == initial_url
    assert session.evaluate("proofSubmits") == 1
    assert counts(observer)["UX-KEYBOARD-DISMISS"] == expected, observer.result


@browser_only
def test_rendered_cover_scroll_retest_avoids_a_control_under_a_fixed_header(page, site):
    session, _ = page
    observer = inline(
        session,
        site,
        '<div style="height:2000px"><button id="target" '
        'style="position:absolute;top:250px;width:100px;height:48px">Save</button></div>'
        '<header style="position:fixed;left:0;top:0;width:390px;height:100px;z-index:10">Header</header>',
        ["cover"],
    )
    session.evaluate("scrollTo(0,200)")
    assert observer.probe("scanStatic", observer.config)["findings"] == []
    assert session.evaluate("scrollY") == 200


@browser_only
def test_rendered_scan_cap_names_every_remaining_control_and_field(page, site):
    session, _ = page
    html = "".join(f'<button id="control-{i}" style="width:50px;height:50px">{i}</button>' for i in range(301))
    html += '<input id="pending-fee" aria-label="Fee" value="12.50">'
    observer = inline(session, site, html, ["input-kind", "tap"])
    output = observer.probe("scanStatic", observer.config)
    assert output["status"] == "incomplete"
    for check in ("UX-TAP", "UX-INPUT-DATE", "UX-INPUT-NUMBER"):
        assert any(
            row["check"] == check and any(el["key"] == "#pending-fee" for el in row["unchecked"])
            for row in output["skipped"]
        )


@browser_only
def test_rendered_shift_cap_and_budget_name_unchecked_controls(page, site):
    session, _ = page
    observer = inline(
        session,
        site,
        "".join(f'<button id="control-{i}" style="width:50px;height:50px">{i}</button>' for i in range(302)),
        ["shift"],
    )
    before = observer.probe("shiftBefore", None)
    assert before["status"] == "incomplete"
    assert any(el["key"] == "#control-301" for row in before["skipped"] for el in row["unchecked"])
    after = observer.probe("shiftAfter", observer.config)
    assert after["status"] == "incomplete"
    assert any(el["key"] == "#control-301" for row in after["skipped"] for el in row["unchecked"])
