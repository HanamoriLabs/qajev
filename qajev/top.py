"""`qajev top`: a live dashboard of every QAJev run on this machine. Who has the browser, what is queued, each
job's progress and finished scenarios, QAJev's Chromes, and recent reports (open one with `o`).

snapshot() gathers the state, render() turns it into styled lines; the curses loop only draws them, so
`qajev top --once` and `--json` show the same thing without a terminal UI.
"""

import json
import os
import textwrap
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

REFRESH_S = 1.0
KEYS = "q quit · ↑↓/jk select · enter details · o open report · s stop job · r refresh"
BAR = 12


def _ago(seconds):
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def _bar(done, total):
    if not total:
        return "·" * BAR
    filled = round(BAR * min(done / total, 1))
    return "█" * filled + "░" * (BAR - filled)


def _when(stamp):
    """ "2026-09-30T18:02:21[+0800]" (local time, as jobs and the project index write it) -> epoch seconds."""
    try:
        return time.mktime(time.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0


def snapshot(limit=30):
    """Everything the dashboard shows, as plain data."""
    from . import chrome, jobs
    from . import project as project_mod

    job_rows = jobs.listing(limit)
    runs = {}
    for j in job_rows:
        final = jobs.result(j["id"]) if j["state"] in ("done", "stopped") else None
        if final and final.get("run_dir"):
            j["cost_usd"] = (final.get("cost") or {}).get("usd")
            runs[final["run_dir"]] = {
                "when": _when(j["started_at"]),
                "title": j["title"],
                "gate": final.get("gate"),
                "cost_usd": j["cost_usd"],
                "html": str(Path(final["run_dir"]) / "report.html"),
                "outcomes": final.get("counts") or {},
                "changes": final.get("changes"),
                "job": j["id"],
            }
    try:
        index = project_mod.index_rows(None, limit)
    except (OSError, ValueError):
        index = []
    for r in index:
        folder = str(Path(r["report"]).parent)
        outcomes = {}
        for o in r.get("objectives") or []:
            outcomes[o["outcome"]] = outcomes.get(o["outcome"], 0) + 1
        kind = " smoke" if "-smoke-" in Path(folder).name else ""
        entry = runs.setdefault(
            folder,
            {
                "when": _when(r["when"]),
                "title": f"{r['project']} {r['env']}{kind}",
                "gate": r["gate"],
                "cost_usd": r.get("cost_usd"),
                "html": r.get("html") or str(Path(folder) / "report.html"),
                "outcomes": outcomes,
                "changes": r.get("changes"),
            },
        )
        if not entry.get("changes"):  # a run seen first as a job: the index row carries its comparison too
            entry["changes"] = r.get("changes")
    recent = sorted(runs.values(), key=lambda x: x["when"], reverse=True)[:limit]
    today = time.strftime("%Y-%m-%d")
    todays = [r for r in recent if time.strftime("%Y-%m-%d", time.localtime(r["when"])) == today]
    try:
        browsers = [
            {
                "name": b.get("state_key") or b.get("profile"),
                "port": b.get("port"),
                "pid": b.get("pid"),
                "headless": b.get("headless"),
                "tabs": b.get("tabs"),
                "alive": b.get("alive"),
                "age_s": round(time.time() - b["started_at"]) if b.get("started_at") else None,
            }
            for b in chrome.status()
        ]
    except OSError:
        browsers = []
    return {
        "at": time.time(),
        "load": os.getloadavg()[0],
        "holder": jobs.holder(),
        "jobs": job_rows,
        "browsers": browsers,
        "native": _native(),
        "recent": recent,
        "today": {
            "runs": len(todays),
            "cost_usd": round(sum(r.get("cost_usd") or 0 for r in todays), 4),
            "gates": {g: sum(1 for r in todays if r["gate"] == g) for g in ("PASS", "FAIL", "INCOMPLETE")},
        },
    }


_reports = {}  # report.json path -> (mtime, data): the detail view re-renders every second


def _report(folder):
    path = Path(folder) / "report.json"
    try:
        mtime = path.stat().st_mtime
        if _reports.get(path, (None,))[0] != mtime:
            _reports[path] = (mtime, json.loads(path.read_text()))
        return _reports[path][1]
    except (OSError, json.JSONDecodeError):
        return None


def _where(url):
    parts = urlsplit(url or "")
    return f"{parts.netloc}{parts.path}" if parts.netloc else ""


def _clip(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _why(reason):
    """The verdict part of a reason, without the page-text excerpt it quotes (the page text is shown once)."""
    reason = " ".join(str(reason).split())
    cut = reason.find(" (")
    return _clip(reason[:cut] if cut > 40 else reason, 220)


def explain(scenarios, add, width, indent=4):
    """Why each scenario ended as it did: failed checks, findings, what the page said, where Jev ended up."""
    pad = " " * indent

    def say(text, style=None, extra=2):
        for n, line in enumerate(textwrap.wrap(" ".join(str(text).split()), max(width - indent - extra, 20))):
            add((pad + " " * extra + ("" if n == 0 else "  ") + line, style))

    for r in scenarios:
        checks = r.get("checks") or []
        failed = [c for c in checks if not c.get("ok")]
        findings = r.get("findings") or []
        jev = r.get("jev") or {}
        facts = [f"{r['seconds']:.1f} s" if isinstance(r.get("seconds"), (int, float)) else "",
                 f"{jev['actions']} Jev action(s)" if jev.get("actions") is not None else "",
                 f"${r['cost_usd']:.4f}" if r.get("cost_usd") else ""]
        facts = " · ".join(f for f in facts if f)
        if r["outcome"] == "pass" and not findings:
            add((f"{pad}{'pass':<10} {r['name']}  ({len(checks)} check(s) passed{', ' + facts if facts else ''})"
                 [:width], "pass"))
            continue
        add((f"{pad}{r['outcome']:<10} {r['name']}"[:width], r["outcome"]))
        if r.get("reason"):
            say(f"why: {_why(r['reason'])}")
        for c in failed:
            say(f"✗ {_clip(c['check'], 160)}" + (f" — found: {_clip(c['detail'], 120)}" if c.get("detail") else ""),
                "fail")
        if checks and not failed:
            say(f"✓ {len(checks)} check(s) passed", "dim")
        groups = {}  # the same finding seen again and again (e.g. a spinner on every look) is one line
        for f in findings:
            key = (f.get("severity"), f.get("kind"), _where(f.get("url")))
            groups.setdefault(key, []).append(f)
        for n, ((sev, kind, where), same) in enumerate(groups.items()):
            if n == 5:
                say(f"… and {len(groups) - 5} more kind(s) of finding", "dim")
                break
            times = f" ×{len(same)}" if len(same) > 1 else ""
            say(f"{sev} {kind}{times}: {_clip(same[-1].get('detail'), 140)}" + (f" ({where})" if where else ""),
                "stuck")
        if r["outcome"] in ("fail", "stuck", "harness"):
            if r.get("end_url"):
                say(f"ended at: {_where(r['end_url'])}", "dim")
            if r.get("page_says"):
                say(f"page said: {_clip(r['page_says'], 240)}", "dim")
        if facts:
            say(facts, "dim")
        if r.get("shot"):
            say(f"screenshot: {r['shot']}", "dim")


def _native():
    from . import native

    try:
        return [{"name": Path(r["project"]).name if Path(r["project"]).name != "godot"
                 else Path(r["project"]).parent.name, "engine": r.get("engine"), "adapter": r.get("adapter"),
                 "pid": r["pid"], "port": r.get("port"), "headless": r.get("headless"),
                 "age_s": round(time.time() - r["started_at"]) if r.get("started_at") else None}
                for r in native.running() if r["alive"]]
    except OSError:
        return []


def _job_style(j):
    if j["state"] in ("running", "queued"):
        return j["state"]
    return {"PASS": "pass", "FAIL": "fail", "INCOMPLETE": "harness"}.get(j.get("gate") or "", "dim")


def selectable(snap):
    """The rows the cursor moves over: jobs first, then recent reports."""
    return [("job", j) for j in snap["jobs"]] + [("run", r) for r in snap["recent"]]


def render(snap, *, width=120, selected=0, detail=None, message=None):
    """-> [(text, style)] lines; style is one of head, dim, sel, pass, fail, stuck, harness, running, queued, live
    (a Chrome or game that is up right now)."""
    lines = []
    add = lines.append
    h = snap["holder"]
    queued = sum(1 for j in snap["jobs"] if j["state"] == "queued")
    busy = (
        f"browser: {h.get('what') or 'a run'} ({_ago(snap['at'] - h['since'])})"
        if h and h.get("since")
        else "browser: in use"
        if h
        else "browser: free"
    )
    t = snap["today"]
    live = sum(j.get("cost_usd") or 0 for j in snap["jobs"] if j["state"] == "running")
    add((f"QAJev top · {busy} · {queued} queued · load {snap['load']:.1f} · {time.strftime('%H:%M:%S')}", "head"))
    add(
        (
            f"today: {t['runs']} run(s) · {t['gates']['PASS']} PASS · {t['gates']['FAIL']} FAIL · "
            f"{t['gates']['INCOMPLETE']} INCOMPLETE · ${t['cost_usd']:.4f}"
            + (f" (+${live:.4f} running now)" if live else ""),
            "dim",
        )
    )
    add((KEYS, "dim"))
    rows = selectable(snap)
    selected = min(selected, len(rows) - 1) if rows else -1

    add(("", None))
    add(("Jobs " + "─" * (width - 6), "head"))
    if not snap["jobs"]:
        add(("  no jobs yet: runs from MCP or with --background show up here", "dim"))
    for i, j in enumerate(snap["jobs"]):
        p = j["progress"]
        count = f"{p['done']}/{p['total']}" if p.get("total") else f"{p['done']}"
        tail = (
            j.get("waiting")
            if j["state"] == "queued"
            else f"▸ {j['current']}"
            if j.get("current")
            else j.get("gate") or j.get("error") or ""
        )
        cost = f"${j['cost_usd']:.4f}" if isinstance(j.get("cost_usd"), (int, float)) else ""
        lead = (f"{'›' if i == selected else ' '} {j['state']:<8} {_bar(p['done'], p.get('total'))} {count:>5}  "
                f"{_ago(j['seconds']):>7}  ")
        tw = max(44, min(80, width - len(lead) - 30))  # a wide terminal widens the title; cost and gate keep 30
        text = f"{lead}{j['title'][:tw]:<{tw}} {cost:>8}  {tail}"
        add((text[:width], "sel" if i == selected else _job_style(j)))
        now = j.get("now") if j["state"] == "running" else None
        if now and now.get("doing"):
            p_txt = f" (p {now['p']:.2f})" if isinstance(now.get("p"), (int, float)) else ""
            step = f" · step {now['n']}" if now.get("n") else ""
            ago = f" · {_ago(snap['at'] - now['at'])} ago" if isinstance(now.get("at"), (int, float)) else ""
            add((f"      Jev ▸ {now['doing']}{p_txt}{step}{ago}"[:width], "running"))
        if detail == ("job", j["id"]):
            from . import jobs

            full = jobs.status(j["id"], detail=True)
            add((f"    job {j['id']} · pid {j['pid']} · started {j['started_at']}", "dim"))
            if full.get("run_dir"):
                add((f"    run folder {full['run_dir']}", "dim"))
            explain(full["scenarios"], add, width)
            if full.get("error"):
                add((f"    error: {full['error']}"[:width], "fail"))
            if full.get("current"):
                add((f"    running    {full['current']}", "running"))

    add(("", None))
    add(("Chrome " + "─" * (width - 8), "head"))
    if not snap["browsers"]:
        add(("  none running", "dim"))
    for b in snap["browsers"]:
        mode = "headless" if b["headless"] else "headed"
        age = _ago(b["age_s"]) if b.get("age_s") is not None else "?"
        add(
            (
                f"  {'●' if b.get('alive') else ' '} {b['name'][:40]:<40} port {b['port']}  {mode:<8} "
                f"{b['tabs'] if b['tabs'] is not None else '?'} tab(s)  pid {b['pid']}  up {age}"[:width],
                "live" if b.get("alive") else "fail",
            )
        )

    add(("", None))
    add(("Native " + "─" * (width - 8), "head"))
    if not snap.get("native"):
        add(("  no game running (qajev play)", "dim"))
    for g in snap.get("native") or []:
        mode = "headless" if g["headless"] else "windowed"
        age = _ago(g["age_s"]) if g.get("age_s") is not None else "?"
        add((f"  ● {g['name'][:24]:<24} {g['engine'] or '?':<8} adapter {g['adapter'] or '-':<10} {mode:<9} "
             f"port {g['port']}  pid {g['pid']}  up {age}"[:width], "live"))

    add(("", None))
    add(("Recent reports " + "─" * (width - 16), "head"))
    if not snap["recent"]:
        add(("  none yet", "dim"))
    offset = len(snap["jobs"])
    for k, r in enumerate(snap["recent"]):
        i = offset + k
        outcomes = " ".join(f"{n} {o}" for o, n in r["outcomes"].items() if n)
        cost = f"${r['cost_usd']:.4f}" if isinstance(r.get("cost_usd"), (int, float)) else ""
        when = time.strftime("%m-%d %H:%M", time.localtime(r["when"])) if r["when"] else "?"
        text = (
            f"{'›' if i == selected else ' '} {when}  {(r['gate'] or '?'):<10} {r['title'][:36]:<36} "
            f"{cost:>8}  {outcomes}"
        )
        ch = r.get("changes") or {}
        if ch.get("changed"):
            text += f"  Δ {ch['summary']}"
        style = "sel" if i == selected else {"PASS": "pass", "FAIL": "fail"}.get(r["gate"], "harness")
        add((text[:width], style))
        if detail == ("run", r["html"]):
            data = _report(Path(r["html"]).parent)
            if data:
                sev = {}
                for f in data.get("findings") or []:
                    sev[f.get("severity")] = sev.get(f.get("severity"), 0) + 1
                known = len(data.get("known_findings") or [])
                summary = ", ".join(f"{n} {k}" for k, n in sorted(sev.items())) or "no findings"
                add((f"    {data.get('gate')} · {summary}" + (f" · {known} known" if known else "")
                     + f" · {data.get('seconds', 0):.0f} s", "dim"))
                ch = data.get("changes")
                if ch:
                    add((f"    since the previous run: {ch['summary']}"[:width], "stuck" if ch["changed"] else "dim"))
                    for key, label in (("newly_failing", "newly failing"), ("fixed", "fixed"), ("other", "changed")):
                        for it in ch.get(key) or []:
                            add((f"      {label}: {it['name']} ({it['was']} -> {it['now']})"[:width],
                                 {"newly_failing": "fail", "fixed": "pass"}.get(key, "harness")))
                    for f in ch.get("new_findings") or []:
                        add((f"      new finding: {f.get('severity')} {f.get('kind')}: {_clip(f.get('detail'), 90)}"
                             [:width], "stuck"))
                explain(data.get("scenarios") or [], add, width)
            else:
                add(("    (no report.json in this run folder)", "dim"))
            add((f"    report: {r['html']}  (o opens it)"[:width], "dim"))
    if message:
        add(("", None))
        add((message[:width], "stuck"))
    return lines


def plain(snap, width=120):
    return "\n".join(text for text, _ in render(snap, width=width, selected=-1))


# ---- the terminal UI ----


def _open(path):
    if Path(path).exists():
        webbrowser.open(Path(path).resolve().as_uri())
        return f"opened {path}"
    return f"no report yet at {path}"


def run_ui():
    import curses

    def loop(screen):
        from . import jobs

        curses.curs_set(0)
        curses.use_default_colors()
        palette = {
            "pass": curses.COLOR_GREEN,
            "fail": curses.COLOR_RED,
            "stuck": curses.COLOR_YELLOW,
            "queued": curses.COLOR_YELLOW,
            "harness": curses.COLOR_MAGENTA,
            "running": curses.COLOR_CYAN,
            "head": curses.COLOR_CYAN,
            "live": curses.COLOR_GREEN,
        }
        styles = {None: curses.A_NORMAL, "dim": curses.A_DIM, "sel": curses.A_REVERSE}
        for n, (name, color) in enumerate(palette.items(), start=1):
            curses.init_pair(n, color, -1)
            styles[name] = curses.color_pair(n) | (curses.A_BOLD if name in ("head", "fail", "live") else 0)
        screen.timeout(int(REFRESH_S * 1000))
        selected, detail, message, confirm, top = 0, None, None, None, 0
        snap = snapshot()
        while True:
            height, width = screen.getmaxyx()
            lines = render(snap, width=width - 1, selected=selected, detail=detail, message=message)
            cursor = next((n for n, (_, s) in enumerate(lines) if s == "sel"), 0)
            if cursor < top + 3:
                top = max(cursor - 3, 0)
            elif cursor >= top + height - 1:
                top = cursor - height + 2
            screen.erase()
            for y, (text, style) in enumerate(lines[top : top + height]):
                try:
                    screen.addnstr(y, 0, text, width - 1, styles.get(style, curses.A_NORMAL))
                except curses.error:
                    pass
            screen.refresh()
            key = screen.getch()
            rows = selectable(snap)
            pick = rows[selected] if 0 <= selected < len(rows) else None
            if confirm:
                if key in (ord("y"), ord("Y")):
                    st = jobs.stop(confirm)
                    message = f"job {confirm}: {st['state']}"
                elif key != -1:
                    message = "not stopped"
                if key != -1:
                    confirm = None
            elif key in (ord("q"), 27):
                return
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = min(selected + 1, len(rows) - 1)
            elif key in (curses.KEY_UP, ord("k")):
                selected = max(selected - 1, 0)
            elif key in (curses.KEY_NPAGE, ord(" ")):
                selected = min(selected + 10, len(rows) - 1)
            elif key == curses.KEY_PPAGE:
                selected = max(selected - 10, 0)
            elif key in (10, 13, curses.KEY_ENTER) and pick:
                ident = ("job", pick[1]["id"]) if pick[0] == "job" else ("run", pick[1]["html"])
                detail = None if detail == ident else ident
            elif key == ord("o") and pick:
                if pick[0] == "run":
                    message = _open(pick[1]["html"])
                else:
                    folder = jobs.status(pick[1]["id"]).get("run_dir")
                    message = _open(Path(folder) / "report.html") if folder else "this job has no run folder yet"
            elif key == ord("s") and pick and pick[0] == "job":
                if pick[1]["state"] in ("running", "queued"):
                    confirm = pick[1]["id"]
                    message = f"stop job {confirm} ({pick[1]['title']})? y/n"
                else:
                    message = f"job {pick[1]['id']} is {pick[1]['state']}"
            elif key == ord("r"):
                message = None
            snap = snapshot()

    try:
        curses.wrapper(loop)
    except KeyboardInterrupt:
        pass
    return 0
