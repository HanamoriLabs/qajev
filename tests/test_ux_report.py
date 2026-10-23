"""Deterministic UX findings stay beside the verdict, with their complete crop evidence."""

import copy
import json
import re
import time
from types import SimpleNamespace

import pytest

from qajev import report, report_files, report_html


def finding(**changes):
    return {
        "check": "UX-COVER",
        "severity": "advisory",
        "step": 2,
        "url": "http://fixture/covered",
        "element": {"desc": 'button "Save"', "key": "#save", "rect": [270, 768, 96, 52]},
        "message": 'button "Save" is covered by button "Settings" at 3 of 5 tap points.',
        "data": {"points_covered": 3, "points_total": 5},
        "repeats": 3,
        "screenshot": "ux/t1-UX-COVER-1.png",
        **changes,
    }


def document(folder, *, mode="advisory", findings=None, outcome="pass", notes=None, skipped=None):
    scenario = {
        "name": "New show",
        "about": "Save remains usable",
        "outcome": outcome,
        "reason": "expected content is visible" if outcome == "pass" else "ux: 1 findings",
        "checks": [],
        "findings": [],
        "screens": [],
        "ux": {
            "mode": mode,
            "findings": [finding()] if findings is None else findings,
            "skipped": skipped or [],
            "notes": notes or [],
        },
    }
    return report.build(
        SimpleNamespace(name="UX fixture"), [scenario], [], browser={}, started_at=time.time(),
        strict=False, interrupted=False, run_dir=folder,
    )


def save_crops(folder, data):
    for path in report_files.image_paths(data):
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fixture crop")


def test_advisory_report_keeps_pass_and_full_json_evidence_with_crop_below_verdict(tmp_path):
    data = document(tmp_path)
    original_ux = copy.deepcopy(data["scenarios"][0]["ux"])
    save_crops(tmp_path, data)
    report.write(tmp_path, data)
    saved = json.loads((tmp_path / "report.json").read_text())
    assert saved["gate"] == "PASS" and saved["exit_code"] == 0
    assert saved["scenarios"][0]["ux"] == original_ux
    md, html = ((tmp_path / name).read_text() for name in ("report.md", "report.html"))
    assert "| New show | **PASS · UX 1 advisory**" in md
    assert "### New show — PASS · UX 1 advisory" in md
    assert "<br>PASS · UX 1 advisory</td>" in html
    assert "| Check | Severity | Step | Message | Element key | Crop |" in md
    assert finding()["message"] in md and "#save" in md and "UX-COVER" in html
    assert "Repeated 3 times" in md and "Repeated 3 times" in html
    start = html.index('<section class="card" id="s0">')
    assert start < html.index('<h4>UX</h4>', start) < html.index('<th>Check</th>', start)
    assert html.index('<p><strong>PASS · UX 1 advisory</strong></p>', start) < html.index('<h4>UX</h4>', start)
    assert "<td>UX-COVER</td><td>advisory</td><td>2</td>" in html
    assert "<td><code>#save</code></td>" in html
    assert '<a class="shot"' in html and 'href="#shot-1"' in html
    assert '<img src="ux/t1-UX-COVER-1.png"' in html
    assert "[![UX-COVER crop](ux/t1-UX-COVER-1.png)](ux/t1-UX-COVER-1.png)" in md


def test_strict_report_retains_fail_and_reports_ux_count_without_advisory_label(tmp_path):
    data = document(tmp_path, mode="strict", outcome="fail", findings=[finding(severity="fail")])
    md, html = report.markdown(data), report_html.render(data)
    assert data["gate"] == "FAIL" and data["exit_code"] == 1
    assert "FAIL · UX 1" in md and "FAIL · UX 1" in html
    assert "UX 1 advisory" not in md + html
    assert "ux: 1 findings" in md + html
    assert "| UX-COVER | fail | 2 |" in md


@pytest.mark.parametrize("mode", ["advisory", "strict"])
def test_clean_report_has_a_zero_findings_ux_section(tmp_path, mode):
    data = document(tmp_path, mode=mode, findings=[])
    md, html = report.markdown(data), report_html.render(data)
    assert "PASS · UX 0" in md and "PASS · UX 0" in html
    assert "No UX findings." in md and "No UX findings." in html


@pytest.mark.parametrize("mode,outcome", [("advisory", "pass"), ("strict", "fail")])
def test_incomplete_scan_never_looks_clean_and_lists_each_unchecked_field_escaped(tmp_path, mode, outcome):
    fields = [{"key": "#venue", "desc": 'input[text] "Venue"'},
              {"key": '#fee<script> | [x]', "desc": '<img src=x onerror="bad"> Table fee'}]
    data = document(tmp_path, mode=mode, outcome=outcome, findings=[],
                    skipped=[{"check": "UX-INPUT-DATE", "reason": "scan incomplete: 200 ms budget reached",
                              "unchecked": fields}])
    ux = data["scenarios"][0]["ux"]
    ux["status"] = "incomplete"
    original_ux = copy.deepcopy(ux)
    report.write(tmp_path, data)
    saved = json.loads((tmp_path / "report.json").read_text())
    assert saved["scenarios"][0]["ux"] == original_ux
    assert saved["gate"] == outcome.upper()
    md, html = ((tmp_path / name).read_text() for name in ("report.md", "report.html"))
    summary = f"{outcome.upper()} · UX scan incomplete"
    assert f"| New show | **{summary}**" in md and f"### New show — {summary}" in md
    assert f"<br>{summary}</td>" in html and f"<p><strong>{summary}</strong></p>" in html
    for text in (md, html):
        assert "No UX findings." not in text and "UX 0" not in text
        assert "Scan incomplete." in text and "scan incomplete: 200 ms budget reached" in text
        assert "Venue" in text and "#venue" in text and "Table fee" in text
        assert "&lt;img" in text and "#fee&lt;script&gt;" in text
        assert fields[1]["key"] not in text and fields[1]["desc"] not in text
    assert '  - Not checked: input\\[text\\] "Venue" (#venue)' in md
    assert "#fee&lt;script&gt; \\| \\[x\\]" in md
    assert '<li>Not checked: input[text] &quot;Venue&quot; <code>#venue</code></li>' in html
    assert '<code>#fee&lt;script&gt; | [x]</code>' in html


def test_off_writes_no_ux_section_summary_or_evidence(tmp_path):
    data = document(tmp_path, mode="off")
    md, html = report.markdown(data), report_html.render(data)
    assert "## UX" not in md and "<h4>UX</h4>" not in html and "<h2>UX</h2>" not in html
    assert "· UX" not in md + html and "UX-COVER" not in md + html
    assert report_files.image_paths(data) == []


def test_skips_and_notes_preserve_legacy_note_dicts_and_plain_notes(tmp_path):
    legacy = {"basis": "struggle", "kind": "unclear choice", "detail": "Two possible next actions",
              "rule": "one obvious next step", "samples": ["Save or Cancel"]}
    data = document(tmp_path, findings=[], notes=["page did not settle", legacy],
                    skipped=[{"check": "UX-KEYBOARD", "reason": "viewport is not a phone (1280 px wide)"}])
    md, html = report.markdown(data), report_html.render(data)
    for text in (md, html):
        assert "page did not settle" in text and "viewport is not a phone (1280 px wide)" in text
        assert "unclear choice" in text and "struggle signal" in text and "one obvious next step" in text
        assert "Save or Cancel" in text


def test_legacy_ux_lists_and_consistency_still_render(tmp_path):
    data = document(tmp_path)
    data["scenarios"][0]["ux"] = [{"basis": "measured", "kind": "contrast", "detail": "Below AA"}]
    data["ux"] = {"consistency": {"phone": [{"kind": "heading size", "detail": "Sizes differ"}]}}
    md, html = report.markdown(data), report_html.render(data)
    assert "contrast" in md + html and "Below AA" in md + html
    assert "Design consistency (phone)" in md and "Design consistency (phone)" in html
    assert "· UX" not in md + html


def test_a_finding_without_a_safe_crop_says_not_saved_and_plain_dict_notes_remain_readable(tmp_path):
    data = document(tmp_path, findings=[finding(screenshot=None)], notes=[{"reason": "capture unavailable"}])
    md, html = report.markdown(data), report_html.render(data)
    assert "Not saved" in md and "Not saved" in html
    assert "capture unavailable" in md and "capture unavailable" in html
    assert 'class="shot"' not in html and report_files.image_paths(data) == []


def test_ux_fields_are_escaped_in_markdown_html_and_crop_attributes(tmp_path):
    hostile = '<img src=x onerror="alert(1)"> | [bad](javascript:alert(2))'
    crop = 'ux/crop" onerror="bad.png'
    item = finding(check="UX-COVER<script>", message=hostile,
                   element={"key": hostile, "desc": hostile}, screenshot=crop)
    data = document(tmp_path, findings=[item], notes=[hostile],
                    skipped=[{"check": hostile, "reason": hostile}])
    md, html = report.markdown(data), report_html.render(data)
    assert hostile not in md and hostile not in html
    assert "&lt;img" in md and "&lt;img" in html
    assert "\\|" in md and "\\[bad\\]" in md
    assert "UX-COVER&lt;script&gt;" in md and "UX-COVER&lt;script&gt;" in html
    assert crop not in html and 'onerror="bad.png' not in html
    assert 'src="ux/crop%22%20onerror%3D%22bad.png"' in html
    assert re.search(r'<td>[^<]*&lt;img', html)


def test_ux_crop_collection_keeps_all_safe_unique_paths_and_missing_evidence_fails_report(tmp_path):
    data = document(tmp_path, findings=[finding(), finding(screenshot="ux/second.png"), finding(),
                                      finding(screenshot="../outside.png"), finding(screenshot="/private.png")])
    assert report_files.image_paths(data) == ["ux/second.png", "ux/t1-UX-COVER-1.png"]
    report.write(tmp_path, data)
    assert data["gate"] == "INCOMPLETE"
    assert data["report_incomplete"]["missing"] == ["ux/second.png", "ux/t1-UX-COVER-1.png"]
    assert "../outside.png" not in report.markdown(data)
    assert 'src="/private.png"' not in report_html.render(data)


def test_malformed_ux_evidence_is_rejected_instead_of_silently_losing_crops(tmp_path):
    data = document(tmp_path)
    data["scenarios"][0]["ux"]["findings"] = ["not finding metadata"]
    with pytest.raises(ValueError, match="metadata"):
        report_files.image_paths(data)
