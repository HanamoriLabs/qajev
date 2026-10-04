"""report.json (machine), report.md and report.html (people). Product findings and harness notes are kept apart."""

import json
import os
import time
from urllib.parse import urlsplit

from . import __version__, report_html, verdict

OBVIOUS = 0.8
MOTION = {
    "reduce": "reduced (pages were told the visitor prefers reduced motion)",
    "full": "full (as-is)",
}  # Jev's probability at or above which the next step counts as "obvious"


def _atomic(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class Partial:
    """Keeps report.json current while a run is in progress, so a crash or Ctrl-C still leaves evidence."""

    def __init__(self, run_dir, suite, browser, started_at, cap):
        self.run_dir, self.suite, self.browser, self.started_at, self.cap = run_dir, suite, browser, started_at, cap
        self.results = []

    def add(self, result):
        self.results.append(result)
        data = {"qajev": __version__, "suite": self.suite.name, "partial": True, "started_at": self.started_at,
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


def build(suite, results, ledgers, *, browser, started_at, strict, interrupted, run_dir):
    outcomes = [r["outcome"] for r in results]
    counts = {o: outcomes.count(o) for o in verdict.OUTCOMES}
    findings = verdict.dedupe([f for r in results for f in r.get("findings", [])])
    gate = "INCOMPLETE" if interrupted else verdict.gate(outcomes, strict=strict)
    screens = [s for r in results for s in r.get("screens", []) if s.get("p") is not None]
    walls = [r["needs_sign_in"] for r in results if r.get("needs_sign_in")]
    abouts = {s.name: s.about for s in getattr(suite, "scenarios", None) or [] if getattr(s, "about", None)}
    for r in results:  # every test, skipped ones too, says what it would prove
        if not r.get("about") and abouts.get(r["name"]):
            r["about"] = abouts[r["name"]]
    about = getattr(suite, "about", None)
    return {
        **({"about": about} if about else {}),
        **({"needs_sign_in": verdict.sign_in_next_step(walls)} if walls else {}),
        "qajev": __version__,
        "suite": suite.name,
        "gate": gate,
        "exit_code": verdict.EXIT_CODES[gate],
        "interrupted": interrupted,
        "counts": counts,
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
    _atomic(run_dir / "report.json", json.dumps(data, indent=2, default=str))
    _atomic(run_dir / "report.md", markdown(data))
    write_html(run_dir, data)


def write_html(run_dir, data):
    path = run_dir / "report.html"
    _atomic(path, report_html.render(data))
    return path


def _where(url):
    if not url:
        return ""
    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}"  # no query strings: they carry tokens and codes


def _cell(text, limit=160):
    text = " ".join(str(text or "").split())
    return (text[: limit - 1] + "…" if len(text) > limit else text).replace("|", "\\|")


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
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(since["started_at"])) if since.get("started_at") else "?"
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
        f"**Gate: {data['gate']}**" + (" (interrupted)" if data.get("interrupted") else ""),
        "",
        f"{c['pass']} pass · {c['fail']} fail · {c['stuck']} stuck · {c['harness']} harness · "
        f"{c['unverified']} unverified · {c['skipped']} skipped · {data['seconds']:.0f} s · "
        f"${cost['usd']:.4f} of ${cost['cap_usd'] or 0:.2f} cap "
        f"({cost['calls']['typesafe']} {report_html.decider(data)} decisions, {cost['calls']['text']} text calls)",
        "",
        "Legend: **fail** = the product is wrong (page or side effect). **stuck** = Jev found no way forward "
        "(check by hand: often a UX finding). **harness** = the tool ran out of budget, went stale or errored; "
        "it says nothing about the product. **unverified** = no expectations were given.",
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
        lines.append(f"| {_cell(r['name'], 60)} | **{r['outcome']}** | {_cell(r.get('reason'))} | "
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

    lines += ["", "## Per scenario", ""]
    for r in data["scenarios"]:
        lines.append(f"### {r['name']} — {r['outcome']}")
        lines.append("")
        if r.get("about"):
            lines += [f"About: {r['about']}", ""]
        if r.get("goal"):
            lines.append(f"Goal: {_cell(r['goal'], 400)}")
            lines.append("")
        for check in r.get("checks", []):
            mark = "✅" if check["ok"] else "❌"
            lines.append(f"- {mark} {_cell(check['check'], 200)}" + (f" — {_cell(check['detail'])}"
                                                                       if check.get("detail") else ""))
        if r.get("blocked_writes"):
            first = r["blocked_writes"][0]
            lines.append(f"- Read-only guard blocked {len(r['blocked_writes'])} write request(s), e.g. "
                         f"{_cell(first.get('method'))} {_cell(_where(first.get('url')))}")
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
            lines.append(f"- Guard hid or disabled {r['guard_hidden']} control(s) Jev must not use")
        if r.get("guard_hydration"):
            lines.append(f"- React reported {r['guard_hydration']} hydration mismatch(es) on attributes the read-only "
                         "guard set (QAJev's doing, not the page's)")
        lines += _play_md(r)
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
    owner = f"QAJev-managed profile {browser.get('profile')}" if browser.get("managed") else "attached"
    where = (f"- Native: {browser.get('engine')} game {browser.get('project')} (adapter {browser.get('adapter')}"
             f"{', headless' if browser.get('headless') else ', windowed'})" if browser.get("surface") == "native"
             else f"- Browser: {browser.get('cdp_url')} ({owner}{', headless' if browser.get('headless') else ''})")
    lines += [
        "## Run",
        "",
        where,
        f"- Cost: {report_html.decisions_cost(data)}, text model "
        f"${cost['usd_text']:.4f}" + ("" if cost["text_cost_reported"] else " (provider did not report cost)"),
        *([f"- Models: {data['models'].get('decider') or 'Jev'} {data['models']['jev']}; text {data['models']['text']}"]
          if data.get("models") else []),
        *([f"- Motion: {MOTION[data['motion']]}"] if data.get("motion") in MOTION else []),
        *([f"- Sign-in: {report_html.signed_in(data['sign_in'])}"] if data.get("sign_in") else []),
        f"- QAJev {data['qajev']}",
        "",
    ]
    return "\n".join(lines)
