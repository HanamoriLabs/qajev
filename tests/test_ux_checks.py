"""The UX driver owns restoration, evidence and verdict policy; it never calls a model."""
from collections import Counter
from pathlib import Path
from unittest.mock import Mock

import pytest

from qajev.ux_checks import Observer, attach
from qajev.ux_options import resolve


def observer(tmp_path, **options):
    session = Mock()
    session.device = {"width": 390, "height": 844, "scale": 3, "mobile": True}
    return Observer(session, resolve(options), tmp_path, "test")


def finding(check="UX-TAP"):
    return {"check": check, "element": {"key": "#close", "desc": 'button "Close"', "rect": [1, 2, 24, 24]},
            "message": "Close is 24x24 px", "data": {}, "mark": {"elementId": "u1"}}


def test_static_dedupe_keeps_first_crop_and_counts_repeats(tmp_path):
    ux = observer(tmp_path)
    ux.capture = Mock(return_value="ux/first.png")
    ux.collect({"findings": [finding()]})
    changed = finding()
    changed["message"] = "Close is 25x24 px"
    ux.collect({"findings": [changed]})
    assert ux.result["findings"][0]["repeats"] == 2
    assert ux.result["findings"][0]["screenshot"] == "ux/first.png"
    assert "mark" not in ux.result["findings"][0]
    ux.capture.assert_called_once()


def test_advisory_never_changes_verdict_and_strict_fails(tmp_path):
    for mode, outcome, reason in (("advisory", "pass", "loaded"), ("strict", "fail", "ux: 1 findings")):
        ux = observer(tmp_path, ux=mode)
        ux.capture = Mock(return_value="ux/a.png")
        ux.collect({"findings": [finding()]})
        result = {"outcome": "pass", "reason": "loaded"}
        ux.apply(result)
        assert result["outcome"] == outcome
        assert result["reason"] == reason
        assert result["ux"]["findings"][0]["severity"] == ("fail" if mode == "strict" else "advisory")


def test_cap_is_per_id_and_reports_omitted_findings(tmp_path):
    ux = observer(tmp_path, ux_max_findings=1)
    ux.capture = Mock(return_value="ux/a.png")
    second = finding()
    second["element"]["key"] = "#other"
    ux.collect({"findings": [finding(), second, finding("UX-CLIP")]})
    assert Counter(f["check"] for f in ux.result["findings"]) == {"UX-TAP": 1, "UX-CLIP": 1}
    assert any("and 1 more" in note for note in ux.result["notes"])


def test_off_attaches_no_probe_and_writes_no_section():
    session = Mock()
    result = {"outcome": "pass"}
    assert attach(session, {"ux": "off"}, None, "test") is None
    session.call.assert_not_called()
    assert "ux" not in result


def test_crop_always_removes_overlay_on_failed_capture(tmp_path):
    ux = observer(tmp_path)
    ux.probe = Mock(return_value={"x": 0, "y": 0, "width": 160, "height": 120})
    ux.session.capture_region_png.side_effect = RuntimeError("capture refused")
    with pytest.raises(RuntimeError, match="capture refused"):
        ux.capture(finding())
    assert ("unmark",) in [c.args for c in ux.probe.call_args_list]


def test_keyboard_restores_metrics_focus_scroll_and_listeners_on_failure(tmp_path):
    ux = observer(tmp_path)
    saved = dict(ux.session.device)
    def probe(method, *args):
        if method == "snapshot":
            return "saved"
        if method == "keyboardInfo":
            return {"field": {"id": "u1", "key": "#name", "rect": [20, 40, 300, 48],
                              "visible_rect": [20, 40, 300, 48]}, "active_is_field": True}
        if method == "scrollField":
            raise RuntimeError("page changed")
        return {}
    ux.probe = Mock(side_effect=probe)
    ux.settle = Mock()
    with pytest.raises(RuntimeError, match="page changed"):
        ux.keyboard("u1", "screen")
    calls = ux.session.call.call_args_list
    assert calls[0].kwargs["height"] == 464
    assert calls[-1].kwargs == {"width": 390, "height": 844, "deviceScaleFactor": 3, "mobile": True}
    assert ux.session.device == saved
    assert ("restore", "saved") in [c.args for c in ux.probe.call_args_list]
    assert ("dismissEnd",) in [c.args for c in ux.probe.call_args_list]


def test_ux_source_is_packaged_beside_the_driver():
    assert (Path(__file__).parents[1] / "qajev" / "ux-probe.js").is_file()


def test_incomplete_scan_is_explicit_and_strict_refuses_even_without_findings(tmp_path):
    for mode in ("advisory", "strict"):
        ux = observer(tmp_path, ux=mode)
        skipped = {"check": "UX-INPUT-NUMBER", "reason": "scan incomplete: 200 ms budget exhausted",
                   "unchecked": [{"key": "#fee", "desc": 'input[text] "Table fee (RM)"'}]}
        ux.collect({"status": "incomplete", "skipped": [skipped]})
        ux.collect({"findings": []})  # a later successful scan does not erase a missed step's coverage
        result = {"outcome": "pass", "reason": "checked"}
        ux.apply(result)
        assert result["ux"]["status"] == "incomplete"
        assert result["ux"]["skipped"][0]["unchecked"][0]["key"] == "#fee"
        assert result["outcome"] == ("fail" if mode == "strict" else "pass")
        if mode == "strict":
            assert result["reason"] == "ux: scan incomplete"


def test_guard_changed_controls_are_unchecked_without_weakening_the_guard(tmp_path):
    ux = observer(tmp_path)
    ux.session.guard_cfg = {"mode": "readonly"}
    ux.probe = Mock(return_value={"findings": []})
    hidden = [{"key": "#save", "desc": 'button "Save"'}]
    ux.session.evaluate.side_effect = lambda js: hidden if "data-qajev-guard" in js else 390
    ux.scan()
    assert ux.result["status"] == "incomplete"
    assert ux.result["skipped"][0]["unchecked"] == hidden
    assert "guard" in ux.result["skipped"][0]["reason"]
    ux.session.call.assert_not_called()


@pytest.mark.parametrize("missing_folder", [False, True])
def test_missing_crop_is_incomplete_and_strict_refuses_missing_evidence(tmp_path, missing_folder):
    ux = observer(tmp_path, ux="strict")
    if missing_folder:
        ux.run_dir = None
    else:
        ux.capture = Mock(side_effect=RuntimeError("capture refused"))
    ux.collect({"findings": [finding()]})
    result = {"outcome": "pass", "reason": "loaded"}
    ux.apply(result)
    assert result["ux"]["status"] == "incomplete"
    assert result["ux"]["findings"][0]["screenshot"] is None
    assert result["outcome"] == "fail" and result["reason"] == "ux: scan incomplete"


def test_keyboard_scope_all_requires_the_guard_before_any_probe_input(tmp_path):
    ux = observer(tmp_path, ux_keyboard_scope="all")
    ux.session.guard_cfg = {"mode": "readonly"}
    ux.session.require_guard.side_effect = RuntimeError("guard unavailable")
    ux.probe = Mock(return_value={"field": {"id": "u1", "desc": "Name", "rect": [20, 40, 300, 48]},
                                  "active_is_field": True, "done": {"desc": "Done"}})
    ux.settle = Mock()
    ux.safe(ux.keyboard, "u1", "screen")
    ux.session.require_guard.assert_called()
    ux.session.trusted_click.assert_not_called()
    assert ux.result["status"] == "incomplete"


def test_a_clear_tap_that_does_not_focus_is_skipped_without_forcing_focus(tmp_path):
    ux = observer(tmp_path)
    ux.session.guard_cfg = None
    field = {"id": "u1", "desc": "Name", "rect": [20, 40, 300, 48], "center_clear": True}
    ux.probe = Mock(side_effect=lambda method, *args:
                    {"field": field, "active_is_field": False} if method == "keyboardInfo" else None)
    ux.settle = Mock()
    ux.keyboard("u1", "screen")
    assert ("focusField", "u1") not in [call.args for call in ux.probe.call_args_list]
    assert "focused by script" not in ux.result["notes"]
    assert any("could not focus" in skip["reason"] for skip in ux.result["skipped"])


def test_keyboard_dedupe_merges_three_field_labels_once_and_keeps_first_crop(tmp_path):
    ux = observer(tmp_path)
    ux.capture = Mock(return_value="ux/first.png")
    for label in ("Show name", "Venue", "Fee"):
        item = finding("UX-KEYBOARD-MAIN")
        item["data"] = {"field": {"label": label}}
        item["message"] = f"With the keyboard open on {label}, the main button Save is hidden."
        ux.collect({"findings": [item]}, screen="same screen")
    result = ux.result["findings"][0]
    assert result["message"] == "With the keyboard open on Show name, Venue, Fee, the main button Save is hidden."
    assert result["data"]["fields"] == ["Show name", "Venue", "Fee"] and result["repeats"] == 3
    ux.capture.assert_called_once()
