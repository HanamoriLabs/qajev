"""report.json (machine), report.md and report.html (people). Product findings and harness notes are kept apart."""

import json
import tempfile
import time
from html import escape
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

from . import __version__, config, plan, planfile, report_html, verdict
from .report_files import PUBLISH_LOCK, missing_images, publish, ux_crop, ux_details

OBVIOUS = 0.8
MOTION = {
    "reduce": "reduced (pages were told the visitor prefers reduced motion)",
    "full": "full (as-is)",
}  # Jev's probability at or above which the next step counts as "obvious"


def _atomic(path, text):
    publish(path.parent, [(path.name, text.encode())])


class Partial:
    """Keeps report.json current while a run is in progress, so a crash or Ctrl-C still leaves evidence."""

    def __init__(self, run_dir, suite, browser, started_at, cap):
        self.run_dir, self.suite, self.browser, self.started_at, self.cap = run_dir, suite, browser, started_at, cap
        self.results = []

    def add(self, result):
        with PUBLISH_LOCK:
            self.results = [r for r in self.results if r["name"] != result["name"]]
            self.results.append(result)
            try:
                if not json.loads((self.run_dir / "report.json").read_text()).get("partial"):
                    return
            except (OSError, ValueError):
                pass
            data = {"qajev": __version__, "qajev_commit": config.qajev_commit(),
                    "suite": self.suite.name, "partial": True,
                    "started_at": self.started_at,
                    "browser": self.browser, "scenarios": self.results}
            _atomic(self.run_dir / "report.json", json.dumps(data, indent=2, default=str))


def merge_ledgers(ledgers):
    total = {"usd": 0.0, "usd_typesafe_estimated": 0.0, "usd_text": 0.0, "calls": {"typesafe": 0, "text": 0},
             "tokens": {"typesafe": 0, "text": 0}, "errors": 0, "text_cost_reported": True, "cap_usd": None}
    for led in ledgers:
        for key in ("usd", "usd_typesafe_estimated", "usd_text"):
            total[key] = round(total[key] + led[key], 5)
        for key in ("calls", "tokens"):
            for kind in total[key]:
                total[key][kind] += led[key][kind]
        total["errors"] += led["errors"]
        total["text_cost_reported"] &= led["text_cost_reported"]
        total["cap_usd"] = led["cap_usd"]
    return total


def grade_planted(suite, results):
    """The planted faults (expect_fail): each keeps its own outcome and gains `expected`, `expected_check` (the check
    it names, in plain words) and `graded`, the outcome the gate counts, with `graded_reason` (verdict.planted)."""
    planted = {s.name: s.expect_fail for s in getattr(suite, "scenarios", None) or []
               if getattr(s, "expect_fail", None)}
    for r in results:
        named = planted.get(r.get("name"))
        if named:
            r["expected"], r["expected_check"] = "fail", named["words"]
            r["graded"], r["graded_reason"] = verdict.planted(r, named["check"])
    return sum(1 for r in results if r.get("expected") == "fail")


def build(suite, results, ledgers, *, browser, started_at, strict, interrupted, run_dir):
    expected_fail = grade_planted(suite, results)
    outcomes = [r["outcome"] for r in results]
    counts = {o: outcomes.count(o) for o in verdict.OUTCOMES}
    outcomes = [r.get("graded", r["outcome"]) for r in results]  # a planted fault counts as its grade
    findings = verdict.dedupe([f for r in results for f in r.get("findings", [])])
    gate = "INCOMPLETE" if interrupted else verdict.gate(outcomes, strict=strict)
    screens = [s for r in results for s in r.get("screens", []) if s.get("p") is not None]
    walls = [r["needs_sign_in"] for r in results if r.get("needs_sign_in")]
    abouts = {s.name: s.about for s in getattr(suite, "scenarios", None) or [] if getattr(s, "about", None)}
    for r in results:  # every test, skipped ones too, says what it would prove
        if not r.get("about") and abouts.get(r["name"]):
            r["about"] = abouts[r["name"]]
    about = getattr(suite, "about", None)
    # The test plan (José, 6 Oct): each test's about and checks in plain words. A test or check without them is NOT
    # DESCRIBED, and a pass that cannot say what it proved is no PASS.
    the_plan = plan.from_results(results)
    missing = plan.not_described(the_plan)
    if gate == "PASS" and missing:
        gate = "INCOMPLETE"
    return {
        **({"about": about} if about else {}),
        "plan": the_plan,
        **({"not_described": missing} if missing else {}),
        **({"needs_sign_in": verdict.sign_in_next_step(walls)} if walls else {}),
        **({"allow_destructive": True} if (getattr(suite, "guard", None) or {}).get("allow_destructive") else {}),
        # the approved test plan this run followed (planfile.py; José, 8 Oct: no run without one)
        **({"test_plan": suite.test_plan} if getattr(suite, "test_plan", None) else {}),
        "qajev": __version__,
        "qajev_commit": config.qajev_commit(),  # which rules judged this run
        "suite": suite.name,
        "gate": gate,
        "exit_code": verdict.EXIT_CODES[gate],
        "interrupted": interrupted,
        "counts": counts,
        **({"expected_fail": expected_fail} if expected_fail else {}),  # how many tests are planted faults
        "started_at": started_at,
        "seconds": round(time.time() - started_at, 2),
        "run_dir": str(run_dir),
        "browser": browser,
        "cost": merge_ledgers(ledgers),
        "obvious_next_step": {"screens": len(screens), "obvious": sum(1 for s in screens if s["p"] >= OBVIOUS)},
        "findings": findings,
        "scenarios": results,
    }


def write(run_dir, data):
    missing = missing_images(run_dir, data)
    if missing:
        data["report_incomplete"] = {"reason": "Required report images are missing or empty.", "missing": missing}
        if data.get("gate") == "PASS":
            data["gate"], data["exit_code"] = "INCOMPLETE", verdict.EXIT_CODES["INCOMPLETE"]
    else:
        data.pop("report_incomplete", None)
    # Render all formats before any visible file changes; a render exception preserves the previous generation.
    files = [("report.md", markdown(data).encode()), ("report.html", report_html.render(data).encode()),
             ("report.json", json.dumps(data, indent=2, default=str).encode())]
    publish(run_dir, files)


def suite_error(out_dir, source, message, code, *, outcome="suite_error", runner=None):
    """A run rejected before any scenario ran. Keep the normal report schema and non-product INCOMPLETE gate."""
    source = config.redact(str(source)) if source else None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Independent attempts never overwrite one another, even in the same second.
    run_dir = Path(tempfile.mkdtemp(prefix="suite-error-", dir=out_dir))
    data = build(SimpleNamespace(name=Path(source).stem if source else "suite"), [], [], browser={},
                 started_at=time.time(), strict=False, interrupted=False, run_dir=run_dir)
    reason = " ".join(config.redact(str(message)).split())
    data.update(outcome=outcome, error=reason, reason=reason, gate="INCOMPLETE", exit_code=code)
    if runner:
        data["runner"] = runner
    if source:
        data["source"] = str(source)
        data["error"] = data["reason"] = f"{source}: {reason}"
    data = config.redact_tree(data)
    write(run_dir, data)
    return data


def write_html(run_dir, data):
    path = run_dir / "report.html"
    _atomic(path, report_html.render(data))
    return path


def _where(url):
    if not url:
        return ""
    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}"  # no query strings: they carry tokens and codes


def _cell(text, limit: int | None = 160):
    text = " ".join(str(text or "").split())
    return (text[: limit - 1] + "…" if limit is not None and len(text) > limit else text).replace("|", "\\|")


def _clients_md(r):
    """A multiplayer scenario (clients.py): each client's screenshot, and every snapshot with when each client got
    there (ms after the first)."""
    if not r.get("clients"):
        return []
    lines = ["- Clients: " + ", ".join(f"[{c['name']}]({c['shot']})" if c.get("shot") else c["name"]
                                       for c in r["clients"])]
    for name, snap in (r.get("snapshots") or {}).items():
        first = min((x["at"] for x in snap if isinstance(x.get("at"), (int, float))), default=None)
        when = [f"{x['name']} " + (f"+{x['at'] - first:.0f} ms" if first is not None and isinstance(x.get("at"), (
            int, float)) else "timed out" if x.get("timed_out") else "—") for x in snap]
        lines.append(f"- Snapshot {_cell(name, 40)}: " + ", ".join(when))
    return lines


def _react_md(r):
    """What react hooks saw: the frames their policies asked for (a cue on screen), and any key held too long."""
    frames = [e for e in r.get("react") or [] if "shot" in e]
    out = ["- Frames: " + ", ".join(f"[{_cell(e['shot'], 40)}]({e['path']}) at {e['at_s']} s" if e.get("path")
                                    else f"{_cell(e['shot'], 40)} at {e['at_s']} s (not saved)" for e in frames)] \
        if frames else []
    out += [f"- Released {e['released']} at {e['at_s']} s: {e['why']}" for e in r.get("react") or [] if "released" in e]
    out += [f"- At {e['at_s']} s: {e['note']}" for e in r.get("react") or [] if "note" in e]
    return out


def _play_md(r):
    """A real-time play step (Native): its numbers, the decisions made during play, and a sampled timeline."""
    st = r.get("stats")
    if not st:
        return []
    out = [f"- Played {st['seconds']:.0f} s: fps median {st['fps_median']}, 10th percentile {st['fps_p10']}, min "
           f"{st['fps_min']}; frame time p95 {st['frame_ms_p95']} ms; memory {st['memory_start_mb']} -> "
           f"{st['memory_end_mb']} MB (peak {st['memory_peak_mb']})"]
    ends = ", ".join(f"{k[4:]} {v}" for k, v in st.items() if k.startswith("end_"))
    if ends:
        out.append(f"- At the end: {ends}")
    for h in (r.get("history") or [])[:20]:
        if "t" in h:
            out.append(f"- t={h['t']:.0f}s picked: {_cell(h['action'], 110)}"
                       + (f" (p {h['probability']:.2f})" if isinstance(h.get("probability"), (int, float)) else ""))
    timeline = r.get("timeline") or []
    if timeline:
        keys = [k for k in timeline[0] if k not in ("screen",)]
        every = max(1, len(timeline) // 12)
        out += ["", "| " + " | ".join(keys) + " |", "|" + "---|" * len(keys)]
        for row in timeline[::every]:
            out.append("| " + " | ".join(_cell(round(row.get(k), 1) if isinstance(row.get(k), float) else row.get(k),
                                             20) for k in keys) + " |")
        out.append("")
    return out


def _changes_md(ch):
    if not ch:
        return []
    since = ch.get("since") or {}
    when = report_html.format_time(since["started_at"]) if since.get("started_at") else "?"
    out = ["", f"## Changes since the previous run ({when}, {since.get('gate')})", "", f"**{ch['summary']}**"]
    for label, key in (("Newly failing", "newly_failing"), ("Fixed", "fixed"), ("Other outcome changes", "other")):
        for item in ch.get(key) or []:
            out.append(f"- {label}: **{_cell(item['name'], 80)}** {item['was']} → {item['now']}"
                       + (f" ({_cell(item.get('reason'), 120)})" if key != "fixed" and item.get("reason") else ""))
    for f in ch.get("new_findings") or []:
        out.append(f"- New finding: {f['severity']} {_cell(f['kind'], 40)}: {_cell(f['detail'], 100)} "
                   f"({_cell(_where(f.get('url')), 60)})")
    for f in ch.get("gone_findings") or []:
        out.append(f"- Gone: {_cell(f['kind'], 40)} ({_cell(_where(f.get('url')), 60)})")
    return out


BASIS = {"measured": "measured", "struggle": "struggle signal", "opinion": "model opinion"}


def _ux_md(data):
    """The UX section: each note with what it rests on (measured, a struggle signal, a model's opinion) and its rule.
    It never changes the gate."""
    ux = data.get("ux") or {}
    pages = [(r["name"], r["ux"]) for r in data["scenarios"] if isinstance(r.get("ux"), list) and r["ux"]]
    if not pages and not ux.get("consistency"):
        return []
    out = ["", "## UX", "", "Notes on how easy the pages are to use. They never change the gate; each says what it "
           "rests on and the rule it was measured against.", ""]
    for name, notes in pages:
        out.append(f"### {_cell(name, 80)}")
        for n in notes:
            out.append(f"- **{n['kind']}** ({BASIS.get(n['basis'], n['basis'])}): {_cell(n['detail'], 300)}"
                       + (f". Rule: {_cell(n['rule'], 200)}" if n.get("rule") else ""))
            out += [f"  - {_cell(s, 200)}" for s in n.get("samples") or []]
        out.append("")
    for device, notes in (ux.get("consistency") or {}).items():
        out += [f"### Design consistency ({device})", ""]
        out += [f"- **{n['kind']}**: {_cell(n['detail'], 400)}" for n in notes] or ["No differences between pages."]
        out.append("")
    return out


def _ux_cell(text):
    """Keep DOM text literal inside a Markdown table, with the complete diagnostic message."""
    text = escape(" ".join(str(text if text is not None else "").split()), quote=False).replace("\\", "\\\\")
    for char in "|`*_[]":
        text = text.replace(char, "\\" + char)
    return text


def _ux_note_md(note):
    if not isinstance(note, dict):
        return ["- " + _ux_cell(note)]
    if "kind" not in note:
        return ["- " + _ux_cell(json.dumps(note, ensure_ascii=False, sort_keys=True))]
    basis = note.get("basis")
    out = [f"- **{_ux_cell(note['kind'])}**"
           + (f" ({_ux_cell(BASIS.get(basis, basis))})" if basis else "")
           + f": {_ux_cell(note.get('detail'))}"
           + (f". Rule: {_ux_cell(note['rule'])}" if note.get("rule") else "")]
    return out + ["  - " + _ux_cell(sample) for sample in note.get("samples") or []]


def _scenario_ux_md(scenario):
    ux = ux_details(scenario)
    if ux is None:
        return []
    findings = ux.get("findings") or []
    out = ["#### UX", ""]
    if ux.get("status") == "incomplete":
        out += ["Scan incomplete.", ""]
    if findings:
        out += ["| Check | Severity | Step | Message | Element key | Crop |", "|---|---|---|---|---|---|"]
        for finding in findings:
            path = ux_crop(finding)
            href = quote(path, safe="/") if path else None
            check = _ux_cell(finding.get("check"))
            crop = f"[![{check} crop]({href})]({href})" if href else "Not saved"
            message = _ux_cell(finding.get("message"))
            if (finding.get("repeats") or 1) > 1:
                message += f" Repeated {finding['repeats']} times."
            out.append(f"| {check} | {_ux_cell(finding.get('severity'))} | {_ux_cell(finding.get('step'))} | "
                       f"{message} | {_ux_cell((finding.get('element') or {}).get('key'))} | {crop} |")
    elif ux.get("status") != "incomplete":
        out.append("No UX findings.")
    out.append("")
    for skipped in ux.get("skipped") or []:
        out.append(f"- Skipped {_ux_cell(skipped.get('check'))}: {_ux_cell(skipped.get('reason'))}")
        out += [f"  - Not checked: {_ux_cell(field.get('desc'))} ({_ux_cell(field.get('key'))})"
                for field in skipped.get("unchecked") or []]
    for note in ux.get("notes") or []:
        out += _ux_note_md(note)
    return out + [""]


def _needs_sign_in_md(wall):
    if not wall:
        return []
    pages = ", ".join(_where(u) for u in wall["pages"][:3])
    return [f"**Needs sign-in:** {len(wall['pages'])} page(s) asked to sign in ({pages}). {wall['next_step']}", ""]


def markdown(data):
    c = data["counts"]
    cost = data["cost"]
    lines = [
        f"# QAJev report: {data['suite']}",
        "",
        *([data["about"], ""] if data.get("about") else []),
        *([f"Test plan: {data['test_plan']['path'] or 'none'}, {planfile.words(data['test_plan'])}", ""]
          if data.get("test_plan") else []),
        f"**Gate: {data['gate']}**" + (" (interrupted)" if data.get("interrupted") else ""),
        "",
        *([f"Started: {report_html.format_time(data['started_at'])}", ""] if data.get("started_at") else []),
        *(["## Report incomplete", "", data["report_incomplete"]["reason"], "",
           *["- " + name for name in data["report_incomplete"]["missing"]], ""]
          if data.get("report_incomplete") else []),
        *(["## Run did not start", "", "No scenarios were judged. Correct the error before running again.", "",
           # An error can contain Markdown or backticks from a malformed input. Keep it literal, in full.
           "    " + data["error"].replace("\n", "\n    "), ""] if data.get("error") else []),
        *([f"> **{report_html.DESTRUCTIVE}**", ""] if data.get("allow_destructive") else []),
        f"{c['pass']} pass · {c['fail']} fail · {c['stuck']} stuck · {c['harness']} harness · "
        f"{c['unverified']} unverified · {c['skipped']} skipped · {data['seconds']:.0f} s · "
        f"${cost['usd']:.4f} of ${cost['cap_usd'] or 0:.2f} cap "
        f"({cost['calls']['typesafe']} {report_html.decider(data)} decisions, {cost['calls']['text']} text calls)",
        "",
        *([report_html.planted_count(data["expected_fail"]), ""] if data.get("expected_fail") else []),
        "Legend: **fail** = the product is wrong (page or side effect). **stuck** = Jev found no way forward "
        "(check by hand: often a UX finding). **harness** = the tool ran out of budget, went stale or errored, or "
        "Jev said DONE without trying; it says nothing about the product. **unverified** = no expectations were given.",
        "",
        *_needs_sign_in_md(data.get("needs_sign_in")),
        "## Scenarios",
        "",
        "| Scenario | Outcome | Why | Actions | Seconds | Ended at | Shot |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in data["scenarios"]:
        jev = r.get("jev") or {}
        shot = f"[shot]({r['shot']})" if r.get("shot") else ""
        ended = _cell(_where(r.get("end_url")), 80)
        planted = f" ({report_html.planted_tag(r)})" if r.get("expected") else ""
        lines.append(f"| {_cell(r['name'], 60)} | **{report_html.ux_summary(r)}**{_cell(planted, 120)} | "
                     f"{_cell(r.get('graded_reason') or r.get('reason'))} | "
                     f"{jev.get('actions', '')} | {r.get('seconds', '')} | {ended} | {shot} |")

    product = data["findings"]
    lines += _changes_md(data.get("changes"))
    lines += ["", "## Findings", ""]
    if product:
        lines += ["| Severity | Scenario | Finding | Detail | Where |", "|---|---|---|---|---|"]
        for f in product:
            lines.append(f"| {f['severity']} | {_cell(f['scenario'], 50)} | {_cell(f['kind'], 40)} | "
                         f"{_cell(f['detail'])} | {_cell(_where(f.get('url')), 80)} |")
    else:
        lines.append("No product findings.")

    known = data.get("known_findings") or []
    if known:
        lines += ["", "### Known (not raised again)", "", "| Finding | Where | Owner's note |", "|---|---|---|"]
        for f in known:
            lines.append(f"| {_cell(f['kind'], 40)}: {_cell(f['detail'], 60)} | {_cell(_where(f.get('url')), 60)} | "
                         f"{_cell(f['known'], 120)} |")

    lines += _ux_md(data)
    lines += ["", "## Per scenario", ""]
    for r in data["scenarios"]:
        lines.append(f"### {r['name']} — {report_html.ux_summary(r)}")
        lines.append("")
        lines += _scenario_ux_md(r)
        if r.get("about"):
            lines += [f"About: {r['about']}", ""]
        effect = r.get("effect")
        if effect:
            lines += ["Visible effect: " + _cell(effect["name"], 200), "",
                      "Region: " + json.dumps(effect["region"], ensure_ascii=False), "",
                      "Pixels: " + json.dumps(effect["metrics"], ensure_ascii=False, sort_keys=True), "",
                      " · ".join(f"[{key}]({effect[key]})" for key in ("off", "on", "difference")), ""]
        if r.get("expected"):
            tag = report_html.planted_tag(r)
            lines += [f"{tag[:1].upper()}{tag[1:]}. Gate: {r['graded_reason']}", ""]
        if r.get("goal"):
            lines.append(f"Goal: {_cell(r['goal'], 400)}")
            lines.append("")
        for check in r.get("checks", []):
            mark = "✅" if check["ok"] else "❌"
            lines.append(f"- {mark} {_cell(check['check'], 200)}" + (f" — {_ux_cell(check['detail'])}"
                                                                       if check.get("detail") else ""))
            evidence = check.get("evidence")
            if check.get("ok") is False and check.get("value") is not None:
                value = check["value"]
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
                lines += ["", "Returned value:", "", "    " + text.replace("\n", "\n    "), ""]
            if isinstance(evidence, dict):
                text = json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2)
                lines += ["", "Claim evidence:", "", "    " + text.replace("\n", "\n    "), ""]
                if check.get("evidence_type") != "js":
                    lines += [" · ".join(f"[{key}]({evidence[key]})" for key in
                                         ("crop", "reference", "panel", "difference") if evidence.get(key)), ""]
        if r.get("page_console") or r.get("page_console_omitted"):
            lines += ["", "Page console:", ""]
            for entry in r.get("page_console") or []:
                text = f"{entry.get('level')}: {entry.get('text')}" + (" [truncated]" if entry.get("truncated") else "")
                lines += ["    " + text.replace("\n", "\n    ")]
            if r.get("page_console_omitted"):
                lines += ["", f"{r['page_console_omitted']} console lines omitted"]
            lines += [""]
        diagnostics = r.get("diagnostics")
        if isinstance(diagnostics, dict):
            state = "complete" if diagnostics.get("complete") else "incomplete"
            lines += ["", f"Native output: {state}", ""]
            if diagnostics.get("reason"):
                lines += [str(diagnostics["reason"]), ""]
            for error in diagnostics.get("errors") or []:
                lines += ["    " + str(error).replace("\n", "\n    "), ""]
        if r.get("blocked_writes"):
            first = r["blocked_writes"][0]
            lines.append(f"- Read-only guard blocked {len(r['blocked_writes'])} write request(s), e.g. "
                         f"{_cell(first.get('method'))} {_cell(_where(first.get('url')))}")
        for w in r.get("allowed_writes") or []:  # every write production let through (allow_requests) is listed
            lines.append(f"- Allowed write: {_cell(w.get('method'))} {_cell(_where(w.get('url')))}")
        for url in r.get("frames_not_observed") or []:  # its writes and requests are in no list: never "none"
            lines.append("- " + _cell(report_html.NOT_OBSERVED.format(where=_where(url) or verdict.NO_ADDRESS)))
        for f in r.get("frames_accepted") or []:
            lines.append("- " + _cell(report_html.ACCEPTED.format(where=_where(f.get("url")), why=f.get("why", ""))))
        lines += ["- " + _cell(line, 400) for line in report_html.frame_lines(r)]
        listed, shown = r.get("block_list_requests") or [], report_html.BLOCK_LIST_SHOWN
        for q in listed[:shown]:  # what the page tried to reach on block_urls
            lines.append(f"- Block list: {_cell(q.get('method'))} {_cell(_where(q.get('url')))}, "
                         + ("blocked" if q.get("blocked") else "not blocked") + _cell(report_html.from_frame(q)))
        if len(listed) > shown:
            lines.append(f"- Block list: {len(listed) - shown} more in report.json")
        for d in r.get("dialogs") or []:
            said = f": {_cell(d['message'], 120)}" if d.get("message") else ""
            lines.append(f"- Dismissed {_cell(d.get('kind'))}{said}")
        blocked = [a for a in r.get("assists") or [] if a.get("after", "BLOCKED") == "BLOCKED"]
        if blocked:
            moved = sum(1 for a in blocked if a["scrolled"])
            lines.append(f"- After Jev said BLOCKED, QAJev scrolled {len(blocked)} time(s); the page moved "
                         f"{moved} time(s)")
        for a in r.get("assists") or []:
            if a.get("after") == "visible":
                found = "and brought it on screen" if a["found"] else "without bringing it on screen"
                lines.append(f"- Jev stopped with the expected text below the fold; QAJev scrolled {a['scrolled']} "
                             f"screen(s) {found}")
        if r.get("guard_hidden"):
            held = r.get("guard_hidden_controls") or []
            lines.append(f"- Guard hid or disabled {len(held) or r['guard_hidden']} control(s) Jev must not use"
                         + (". A picture marks each hidden one with a dashed box, \"hidden by guard\":"
                            if held else ""))
            lines += [f"  - {_cell(line, 200)}" for line in verdict.hidden_list(held)]
        if r.get("guard_hydration"):
            lines.append(f"- React reported {r['guard_hydration']} hydration mismatch(es) on attributes the read-only "
                         "guard set (QAJev's doing, not the page's)")
            lines += [f"  - {_cell(d, 500)}" for d in r.get("guard_hydration_details") or []]
        lines += _play_md(r)
        lines += _clients_md(r)
        lines += _react_md(r)
        screens = r.get("screens") or []
        if screens:
            lines += ["", f"| Step | {report_html.decider(data)}'s next step | p | Runner-up | p "
                          "| One obvious next step? |",
                      "|---|---|---|---|---|---|"]
            for s in screens:
                obvious = "yes" if (s.get("p") or 0) >= OBVIOUS else "unclear"
                lines.append(f"| {s['step']} | {_cell(s['next_step'], 70)} | {s.get('p')} | "
                             f"{_cell(s.get('runner_up'), 70)} | {s.get('runner_up_p')} | {obvious} |")
        lines.append("")

    browser = data["browser"]
    owner = report_html.browser_owner(browser)
    where = (f"- Native: {browser.get('engine')} game {browser.get('project')} (adapter {browser.get('adapter')}"
             f"{', headless' if browser.get('headless') else ', windowed'})" if browser.get("surface") == "native"
             else f"- Browser: {browser.get('cdp_url')} ({owner}{', headless' if browser.get('headless') else ''})")
    lines += [
        "## Run",
        "",
        where,
        *([f"- Game profile: {report_html.game_profile(browser)}"] if report_html.game_profile(browser) else []),
        f"- Cost: {report_html.decisions_cost(data)}, text model "
        f"${cost['usd_text']:.4f}" + ("" if cost["text_cost_reported"] else " (provider did not report cost)"),
        *([f"- Models: {data['models'].get('decider') or 'Jev'} {data['models']['jev']}; text {data['models']['text']}"]
          if data.get("models") else []),
        *([f"- Keys: {'; '.join(data['models']['keys'])}"] if (data.get("models") or {}).get("keys") else []),
        *([f"- Motion: {MOTION[data['motion']]}"] if data.get("motion") in MOTION else []),
        *([f"- CPU: {data['cpu_throttle']}× slower (Chrome's CPU throttling)"]
          if (data.get("cpu_throttle") or 1) > 1 else []),
        *([f"- Watched live: {data['watched_live_s']:.0f} s in the dashboard"] if data.get("watched_live_s") else []),
        *([f"- Sign-in: {report_html.signed_in(data['sign_in'])}"] if data.get("sign_in") else []),
        f"- QAJev {data['qajev']}" + (f" ({data['qajev_commit']})" if data.get("qajev_commit") else ""),
        "",
    ]
    return "\n".join(lines)
