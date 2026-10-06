"""report.html: the same report as report.md, as one self-contained page (inline CSS, no scripts, screenshots linked
relative to the run folder). URLs lose their query strings, as in report.md: they carry tokens and codes."""

import base64
import time
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from . import plan as plan_mod
from .verdict import hidden_words

# The red banner of a run that showed destructive controls (allow_destructive; refused for a production host).
DESTRUCTIVE = ("Destructive controls were shown (allow_destructive): Jev could delete, remove, refund or cancel "
               "things in this run. Local dev hosts only; production never shows them.")

OBVIOUS = 0.8
OUTCOME_ORDER = ("pass", "fail", "stuck", "harness", "unverified", "skipped")
LEGEND = {
    "pass": "every expectation held",
    "fail": "the product is wrong (page or side effect)",
    "stuck": "Jev found no way forward; check by hand, often a UX finding",
    "harness": "the tool ran out of budget, went stale or errored, or Jev said DONE without trying; says nothing "
               "about the product",
    "unverified": "no expectations were given",
    "skipped": "not run (machine busy, or an earlier step failed)",
}

# QAJev's mark (the repo's site/favicon.svg, copied to qajev/assets), inline: a report loads nothing from elsewhere.
MARK = "data:image/svg+xml;base64," + base64.b64encode(
    (Path(__file__).with_name("assets") / "mark.svg").read_bytes()).decode()

CSS = """
:root { --bg:#f7f7f5; --card:#fff; --ink:#1c1c1a; --muted:#6b6b66; --line:#e3e3de; --code:#f0f0ec;
  --pass:#1f7a3a; --pass-bg:#e3f4e8; --fail:#b3261e; --fail-bg:#fde8e6; --stuck:#9a5b00; --stuck-bg:#fdf0d9;
  --harness:#5b4bb3; --harness-bg:#ecebfb; --other:#55554f; --other-bg:#ecece8; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#141413; --card:#1e1e1c; --ink:#ecece8; --muted:#9d9d96; --line:#33332f; --code:#262624;
  --pass:#7fd49a; --pass-bg:#1c3324; --fail:#ff9d94; --fail-bg:#3d1f1c; --stuck:#f3c173; --stuck-bg:#3a2d15;
  --harness:#b9afff; --harness-bg:#29254a; --other:#c4c4bd; --other-bg:#2c2c29; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { max-width:1100px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:22px; margin:0 0 4px; overflow-wrap:anywhere; display:flex; align-items:center; gap:10px; }
h1 .mark { flex:none; border-radius:6px; }
h2 { font-size:17px; margin:32px 0 10px; }
h3 { font-size:15px; margin:0; display:flex; gap:8px; align-items:center; flex-wrap:wrap;
  overflow-wrap:anywhere; }
.sub, .muted { color:var(--muted); }
.about { margin:6px 0 0; font-style:italic; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin:12px 0; }
.gate { display:inline-block; font-weight:700; font-size:18px; padding:4px 12px; border-radius:8px; margin:12px 0 8px; }
.pill { display:inline-block; font-size:12px; font-weight:600; padding:1px 8px; border-radius:999px;
  white-space:nowrap; }
.pass { color:var(--pass); background:var(--pass-bg); } .fail { color:var(--fail); background:var(--fail-bg); }
.stuck { color:var(--stuck); background:var(--stuck-bg); }
.harness { color:var(--harness); background:var(--harness-bg); }
.unverified, .skipped, .INCOMPLETE { color:var(--other); background:var(--other-bg); }
.PASS { color:var(--pass); background:var(--pass-bg); } .FAIL { color:var(--fail); background:var(--fail-bg); }
.counts { display:flex; gap:8px; flex-wrap:wrap; margin:8px 0; }
.scroll { overflow-x:auto; }
table { border-collapse:collapse; width:100%; min-width:640px; font-size:14px; }  /* narrow screens scroll it */
th, td { text-align:left; vertical-align:top; padding:6px 8px; border-bottom:1px solid var(--line); }
th { color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.03em; }
td { overflow-wrap:break-word; }
ul.checks { list-style:none; padding:0; margin:8px 0; }
ul.checks li { padding:2px 0; }
.ok { color:var(--pass); font-weight:700; } .no { color:var(--fail); font-weight:700; }
ol.plan { list-style:none; padding:0; margin:8px 0 0; } ol.plan > li { display:flex; gap:10px; padding:8px 0;
  border-top:1px solid var(--line); } ol.plan > li:first-child { border-top:0; }
ol.plan ul { list-style:none; padding:0; margin:4px 0 0; } ol.plan ul li { display:flex; gap:8px; padding:1px 0; }
.box { flex:none; display:inline-flex; align-items:center; justify-content:center; width:20px; height:20px;
  border-radius:5px; border:1.5px solid var(--line); font-size:13px; font-weight:700; }
.box.pass { color:var(--pass); background:var(--pass-bg); border-color:var(--pass); }
.box.fail { color:var(--fail); background:var(--fail-bg); border-color:var(--fail); }
.box.warn { color:var(--stuck); background:var(--stuck-bg); border-color:var(--stuck); }
.nd { color:var(--fail); font-weight:700; font-size:12px; letter-spacing:.04em; }
.shot img { max-width:100%; max-height:360px; border:1px solid var(--line); border-radius:6px; margin-top:8px; }
.clients { display:grid; grid-template-columns:repeat(auto-fill, minmax(170px, 1fr)); gap:10px; margin-top:8px; }
.clients figure { margin:0; } .clients figcaption { font-size:13px; color:var(--muted); }
details { margin-top:8px; } summary { cursor:pointer; color:var(--muted); }
code { background:var(--code); padding:1px 5px; border-radius:4px; font-size:13px; overflow-wrap:anywhere; }
a { color:inherit; }
dl { display:grid; grid-template-columns:max-content 1fr; gap:4px 16px; margin:0; }
dt { color:var(--muted); } dd { margin:0; overflow-wrap:anywhere; }
"""


def _e(value):
    return escape(str(value if value is not None else ""))


def _where(url):
    if not url:
        return ""
    parts = urlsplit(url)
    return f"{parts.netloc}{parts.path}"


def _link(url):
    """A clickable http(s) link without its query string; anything else is shown as text."""
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return _e(url)
    href = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return f'<a href="{_e(href)}">{_e(_where(url))}</a>'


def _pill(outcome):
    return f'<span class="pill {_e(outcome)}">{_e(outcome)}</span>'


def _table(head, rows):
    if not rows:
        return ""
    th = "".join(f"<th>{h}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


BOX = {"pass": "✓", "fail": "✗", "warn": "!", "todo": ""}
NOT_DESCRIBED = '<span class="nd">NOT DESCRIBED</span>'


def _line(line):
    """One check in plain words, ticked by its result, the raw check folded away for agents."""
    state = "todo" if "ok" not in line else "pass" if line["ok"] else "fail"
    said = _e(line["words"]) if line["words"] else f"{NOT_DESCRIBED} <code>{_e(line['check'])}</code>"
    raw = (f'<details><summary>For agents</summary><code>{_e(line["check"])}</code></details>'
           if line["words"] and line.get("check") else "")
    reason = f' <span class="muted">— {_e(line["detail"])}</span>' if line.get("detail") else ""
    return f'<li><span class="box {state}">{BOX[state]}</span><div>{said}{reason}{raw}</div></li>'


def _plan(items, scenarios):
    """The test plan, at the top of the report: each test, what it proves and its checks, ticked as they ran."""
    if not items:
        return ""
    out = ['<section class="card" id="plan"><h2>Test plan</h2><p class="muted">What each test proves, ticked as it '
           "ran: ✓ passed, ✗ failed, ! stuck or harness, empty: not run.</p><ol class=\"plan\">"]
    for i, it in enumerate(items):
        about = _e(it["about"]) if it["about"] else f"{NOT_DESCRIBED}: this test has no about"
        details = {c.get("check"): c.get("detail") for c in (scenarios[i].get("checks") or [])} if i < len(
            scenarios) else {}
        lines = "".join(_line({**c, "detail": details.get(c["check"])}) for c in it["checks"])
        out.append(f'<li class="plan-item {it["state"]}"><span class="box {it["state"]}">{BOX[it["state"]]}</span>'
                   f'<div><a href="#s{i}"><strong>{it["n"]}. {_e(it["name"])}</strong></a><div>{about}</div>'
                   + (f"<ul>{lines}</ul>" if lines else "") + "</div></li>")
    missing = plan_mod.not_described(items)
    out.append("</ol>" + (f'<p class="nd">{len(missing)} test(s) NOT DESCRIBED: a pass would not say what it proved, '
                          "so the gate cannot be PASS.</p>" if missing else "") + "</section>")
    return "".join(out)


def _scenario(i, r, who="Jev"):
    jev = r.get("jev") or {}
    it = plan_mod.item(i + 1, r.get("name"), r.get("about"), r.get("checks") or [], r.get("outcome"))
    title = {"pass": "What it proved", "fail": "What went wrong"}.get(r["outcome"], "What happened")
    parts = [f'<section class="card" id="s{i}"><h3>{_pill(r["outcome"])} {_e(r["name"])}</h3>',
             f'<p class="about"><strong>{title}:</strong> '
             + (_e(r["about"]) if r.get("about") else f"{NOT_DESCRIBED}: this test has no about") + "</p>",
             f'<p>{_e(r.get("reason"))}</p>']
    r = {**r, "_lines": [{**line, "detail": c.get("detail")} for line, c in zip(it["checks"], r.get("checks") or [])]}
    facts = []
    if r.get("url"):
        facts.append(("Start", _link(r["url"])))
    if r.get("end_url"):
        facts.append(("Ended at", _link(r["end_url"])))
    if r.get("goal"):
        facts.append(("Goal", _e(r["goal"])))
    if r.get("mode"):
        facts.append(("Mode", _e(r["mode"])))
    device = r.get("device")
    if isinstance(device, dict) and device.get("width"):
        size = f"{device['width']}×{device['height']}" + (" mobile" if device.get("mobile") else "")
        facts.append(("Device", _e(size)))
    timing = [f"{r['seconds']} s" if r.get("seconds") is not None else "",
              f"{jev['actions']} Jev action(s)" if jev.get("actions") is not None else "",
              f"${r['cost_usd']:.4f}" if isinstance(r.get("cost_usd"), (int, float)) else ""]
    if any(timing):
        facts.append(("Took", _e(" · ".join(t for t in timing if t))))
    if facts:
        parts.append("<dl>" + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts) + "</dl>")

    checks = r.get("checks") or []
    notes = []
    if r.get("blocked_writes"):
        first = r["blocked_writes"][0]
        notes.append(f"Read-only guard blocked {len(r['blocked_writes'])} write request(s), e.g. "
                     f"{_e(first.get('method'))} {_e(_where(first.get('url')))}")
    for w in r.get("allowed_writes") or []:  # every write production let through (allow_requests) is listed
        notes.append(f"Allowed write: {_e(w.get('method'))} {_e(_where(w.get('url')))}")
    for d in r.get("dialogs") or []:
        notes.append(f"Dismissed {_e(d.get('kind'))}" + (f": {_e(d['message'])}" if d.get("message") else ""))
    blocked = [a for a in r.get("assists") or [] if a.get("after", "BLOCKED") == "BLOCKED"]
    if blocked:
        moved = sum(1 for a in blocked if a.get("scrolled"))
        notes.append(f"After Jev said BLOCKED, QAJev scrolled {len(blocked)} time(s); the page moved {moved} time(s)")
    for a in r.get("assists") or []:
        if a.get("after") == "visible":
            notes.append(f"Jev stopped with the expected text below the fold; QAJev scrolled {a['scrolled']} screen(s) "
                         + ("and brought it on screen" if a["found"] else "without bringing it on screen"))
    if r.get("guard_hidden"):
        held = r.get("guard_hidden_controls") or []
        notes.append(f"Guard hid or disabled {r['guard_hidden']} control(s) Jev must not use"
                     + (f": {_e(hidden_words(held, limit=8))}" if held else ""))
    if r.get("guard_hydration"):
        notes.append(f"React reported {r['guard_hydration']} hydration mismatch(es) on attributes the read-only guard "
                     "set (QAJev's doing, not the page's)" + "".join(
                         f'<pre class="muted">{_e(d)}</pre>' for d in r.get("guard_hydration_details") or []))
    if checks or notes:
        items = [_line(line) for line in r["_lines"]]
        items += [f'<li class="muted">{n}</li>' for n in notes]
        parts.append(f'<ul class="checks">{"".join(items)}</ul>')

    if r.get("findings"):
        parts.append(_table(["Severity", "Finding", "Detail", "Where"],
                            [[_e(f["severity"]), _e(f["kind"]), _e(f["detail"]), _link(f.get("url"))]
                             for f in r["findings"]]))
    if r.get("page_says") and r["outcome"] in ("fail", "stuck"):
        parts.append(f'<details><summary>What the page said</summary><p>{_e(r["page_says"])}</p></details>')
    screens = r.get("screens") or []
    if screens:
        rows = [[_e(s.get("step")), _e(s.get("next_step")), _e(s.get("p")), _e(s.get("runner_up")),
                 _e(s.get("runner_up_p")), "yes" if (s.get("p") or 0) >= OBVIOUS else "unclear"] for s in screens]
        parts.append(f"<details><summary>{who}'s view of each screen</summary>"
                     + _table(["Step", f"{who}'s next step", "p", "Runner-up", "p", "One obvious next step?"], rows)
                     + "</details>")
    st = r.get("stats")
    if st:
        parts.append(
            f'<p class="muted">Played {st["seconds"]:.0f} s · fps median {_e(st["fps_median"])}, 10th percentile '
            f'{_e(st["fps_p10"])}, min {_e(st["fps_min"])} · frame time p95 {_e(st["frame_ms_p95"])} ms · memory '
            f'{_e(st["memory_start_mb"])} → {_e(st["memory_end_mb"])} MB (peak {_e(st["memory_peak_mb"])})</p>')
    timeline = r.get("timeline") or []
    if timeline:
        keys = [k for k in timeline[0] if k != "screen"]
        every = max(1, len(timeline) // 15)
        rows = [[_e(round(row.get(k), 1) if isinstance(row.get(k), float) else row.get(k)) for k in keys]
                for row in timeline[::every]]
        parts.append("<details><summary>Timeline (sampled)</summary>" + _table(keys, rows) + "</details>")
    history = r.get("history") or []
    if history:
        rows = [[_e(h.get("step")), _e(h.get("kind")), _e(h.get("action")), _e(h.get("text")),
                 _e(_where(h.get("url")))] for h in history]
        parts.append("<details><summary>Steps Jev took</summary>"
                     + _table(["Step", "Kind", "Action", "Typed", "Page"], rows) + "</details>")
    frames = [e for e in r.get("react") or [] if e.get("path")]
    if frames:  # the frames a react hook's policy asked for: the cue as the player saw it
        parts.append('<div class="clients">' + "".join(
            f'<figure><a class="shot" href="{_e(e["path"])}"><img src="{_e(e["path"])}" loading="lazy" '
            f'alt="{_e(e["shot"])} at {_e(e["at_s"])} s"></a><figcaption>{_e(e["shot"])} at {_e(e["at_s"])} s'
            "</figcaption></figure>" for e in frames) + "</div>")
    notes = [f"Released {_e(e['released'])} at {_e(e['at_s'])} s: {_e(e['why'])}" if "released" in e
             else f"At {_e(e['at_s'])} s: {_e(e['note'])}"
             for e in r.get("react") or [] if "released" in e or "note" in e]
    if notes:
        parts.append("<p>" + "; ".join(notes) + "</p>")
    if r.get("clients"):
        parts.append(_clients_html(r))
    elif r.get("shot"):
        parts.append(f'<a class="shot" href="{_e(r["shot"])}"><img src="{_e(r["shot"])}" loading="lazy" '
                     f'alt="Screenshot at the end of {_e(r["name"])}"></a>')
    parts.append("</section>")
    return "".join(parts)


def _clients_html(r):
    """A multiplayer scenario (clients.py): each client's screen, the steps with when each client acted, and every
    snapshot with when each client got there (ms after the first)."""
    figures = "".join(
        f'<figure><a class="shot" href="{_e(c["shot"])}"><img src="{_e(c["shot"])}" loading="lazy" '
        f'alt="{_e(c["name"])} at the end"></a><figcaption>{_e(c["name"])}</figcaption></figure>' if c.get("shot")
        else f'<figure><figcaption>{_e(c["name"])} (no screenshot)</figcaption></figure>' for c in r["clients"])
    out = [f'<div class="clients">{figures}</div>']
    steps = [[_e(s.get("step")), _e(s.get("do") or f"snapshot {s.get('snapshot')}"),
              _e(", ".join(s.get("clients") or [])),
              _e(", ".join(str(x) for x in s["starts_ms"]) if s.get("starts_ms") else ""), _e(s.get("ms")),
              _e("; ".join([*(f"timed out: {n}" for n in s.get("timed_out") or []), *(s.get("errors") or [])]))]
             for s in r.get("steps") or []]
    if steps:
        out.append("<details><summary>Steps</summary>" + _table(
            ["Step", "Do", "Clients", "Started (ms)", "Took (ms)", "Trouble"], steps) + "</details>")
    rows = []
    for name, snap in (r.get("snapshots") or {}).items():
        first = min((x["at"] for x in snap if isinstance(x.get("at"), (int, float))), default=None)
        for x in snap:
            at = x.get("at")
            rows.append([_e(name), _e(x.get("name")),
                         _e(f"+{at - first:.0f}" if first is not None and isinstance(at, (int, float)) else
                            "timed out" if x.get("timed_out") else "—"),
                         _e((x.get("state") or x.get("error") or "")[:200])])
    if rows:
        out.append("<details><summary>Snapshots</summary>" + _table(["Snapshot", "Client", "At (ms)", "State"], rows)
                   + "</details>")
    return "".join(out)


def _changes(ch):
    if not ch:
        return ""
    since = ch.get("since") or {}
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(since["started_at"])) if since.get("started_at") else "?"
    items = []
    for label, key, cls in (("Newly failing", "newly_failing", "fail"), ("Fixed", "fixed", "pass"),
                            ("Changed", "other", "harness")):
        for it in ch.get(key) or []:
            why = f' <span class="muted">— {_e(it.get("reason"))}</span>' if key != "fixed" and it.get("reason") else ""
            items.append(f'<li><span class="pill {cls}">{label}</span> {_e(it["name"])}: {_e(it["was"])} → '
                         f'{_e(it["now"])}{why}</li>')
    for f in ch.get("new_findings") or []:
        items.append(f'<li><span class="pill stuck">New finding</span> {_e(f["severity"])} {_e(f["kind"])}: '
                     f'{_e(f["detail"])} {_link(f.get("url"))}</li>')
    for f in ch.get("gone_findings") or []:
        items.append(f'<li><span class="pill pass">Gone</span> {_e(f["kind"])} {_link(f.get("url"))}</li>')
    body = f'<ul class="checks">{"".join(items)}</ul>' if items else ""
    return (f'<h2>Changes since the previous run</h2><div class="card"><p><strong>{_e(ch["summary"])}</strong> '
            f'<span class="muted">(compared with {_e(when)}, {_e(since.get("gate"))})</span></p>{body}</div>')


def decider(data):
    """The decision model that ran: Jev, Clef or Clef-flash (older reports have no name: they were Jev)."""
    return (data.get("models") or {}).get("decider") or "Jev"


def game_profile(browser):
    """A game's save folder, in one line: kept (a test profile, game_profile.py) or a throwaway. Or None."""
    p = browser.get("game_profile")
    if not p:
        return None
    return f"{p.get('folder')} (kept for the next run)" if p.get("kept") else "a throwaway, deleted at close"


def browser_owner(browser):
    """Whose Chrome a run used, for both reports: attached, a throwaway one, or a named QAJev profile's."""
    if not browser.get("managed"):
        return "attached"
    if browser.get("ephemeral"):
        return "QAJev throwaway profile (deleted after the run)"
    return f"QAJev-managed profile {browser.get('profile')}"


def decisions_cost(data):
    """What the decisions cost: Jev on TypeSafe is a flat estimate per call; Clef's is its own token count."""
    usd = data["cost"]["usd_typesafe_estimated"]
    if decider(data) == "Jev":
        return f"TypeSafe ${usd:.4f} (estimated per call)"
    return f"{decider(data)} ${usd:.4f}"


def signed_in(s):
    """A stored test account's sign-in, in one line (both reports)."""
    if not s.get("ok"):
        return f"account {s['account']} failed: {s.get('reason')}"
    how = "already signed in" if s.get("already") else f"in {s.get('seconds')} s"
    test = s.get("test_signin")
    if test:  # a seeded test user signed in without a person: say so, and how (never the secret)
        via = "TOTP from seed: yes" if test.get("totp") else "session cookie from seed: yes"
        who = s.get("email") or s["account"]
        return f"as seeded test user {who} on {test.get('host')} (account {s['account']}), {how}; {via}"
    return f"as {s.get('email') or s['account']} (account {s['account']}), {how}"


def _ux(data):
    """The UX section (report.py's _ux_md): notes by page with their basis and rule, then design consistency."""
    from .report import BASIS

    ux = data.get("ux") or {}
    pages = [(r["name"], r["ux"]) for r in data["scenarios"] if r.get("ux")]
    if not pages and not ux.get("consistency"):
        return ""
    out = ["<h2>UX</h2>", '<p class="muted">Notes on how easy the pages are to use. They never change the gate; each '
           "says what it rests on and the rule it was measured against.</p>"]
    for name, notes in pages:
        items = "".join(
            f"<li><strong>{_e(n['kind'])}</strong> ({_e(BASIS.get(n['basis'], n['basis']))}): {_e(n['detail'])}"
            + (f'<br><span class="muted">Rule: {_e(n["rule"])}</span>' if n.get("rule") else "")
            + ("<ul>" + "".join(f"<li>{_e(s)}</li>" for s in n.get("samples") or []) + "</ul>" if n.get("samples")
               else "") + "</li>" for n in notes)
        out.append(f'<div class="card"><h3>{_e(name)}</h3><ul>{items}</ul></div>')
    for device, notes in (ux.get("consistency") or {}).items():
        items = "".join(f"<li><strong>{_e(n['kind'])}</strong>: {_e(n['detail'])}</li>" for n in notes)
        none = '<p class="muted">No differences between pages.</p>'
        out.append(f'<div class="card"><h3>Design consistency ({_e(device)})</h3>'
                   + (f"<ul>{items}</ul>" if items else none) + "</div>")
    return "".join(out)


def render(data):
    c = data["counts"]
    cost = data["cost"]
    title = f"QAJev: {data['suite']}"
    interrupted = " (interrupted)" if data.get("interrupted") else ""
    counts = "".join(f'<span class="pill {o}">{c.get(o, 0)} {o}</span>' for o in OUTCOME_ORDER if c.get(o))
    summary = (f"{data['seconds']:.0f} s · ${cost['usd']:.4f} of ${cost['cap_usd'] or 0:.2f} cap · "
               f"{cost['calls']['typesafe']} {decider(data)} decisions, {cost['calls']['text']} text calls")
    project = data.get("project") or {}
    sub = [f"{project['name']} · {project['env']}"] if project else []
    if data.get("started_at"):
        sub.append(time.strftime("%Y-%m-%d %H:%M", time.localtime(data["started_at"])))
    out = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<title>{_e(title)}</title><link rel="icon" href="{MARK}"><style>{CSS}</style></head><body><main>',
        f'<h1><img class="mark" src="{MARK}" alt="" width="28" height="28">{_e(title)}</h1>',
        f'<p class="about">{_e(data["about"])}</p>' if data.get("about") else "",
        f'<div class="sub">{_e(" · ".join(sub))}</div>' if sub else "",
        f'<div class="gate {_e(data["gate"])}">Gate: {_e(data["gate"])}{interrupted}</div>',
        f'<div class="counts">{counts}</div><div class="muted">{_e(summary)}</div>',
    ]
    if data.get("allow_destructive"):
        out.append(f'<div class="box fail" role="alert"><strong>{_e(DESTRUCTIVE)}</strong></div>')
    wall = data.get("needs_sign_in")
    if wall:
        out.append(f'<div class="card"><p><strong>Needs sign-in:</strong> {len(wall["pages"])} page(s) asked to '
                   f'sign in ({", ".join(_link(u) for u in wall["pages"][:3])}).</p><p>{_e(wall["next_step"])}</p>'
                   "</div>")

    out.append(_plan(data.get("plan") or plan_mod.from_results(data["scenarios"]), data["scenarios"]))
    rows = [[f'<a href="#s{i}">{_e(r["name"])}</a>', _pill(r["outcome"]), _e(r.get("reason")),
             _link(r.get("end_url") or r.get("url"))] for i, r in enumerate(data["scenarios"])]
    out += ["<h2>Scenarios</h2>", _table(["Scenario", "Outcome", "Why", "Ended at"], rows) or
            '<p class="muted">No scenarios ran.</p>']

    out.append(_changes(data.get("changes")))
    out.append("<h2>Findings</h2>")
    findings = data["findings"]
    out.append(_table(["Severity", "Scenario", "Finding", "Detail", "Where"],
                      [[_e(f["severity"]), _e(f["scenario"]), _e(f["kind"]), _e(f["detail"]), _link(f.get("url"))]
                       for f in findings]) if findings else '<p class="muted">No product findings.</p>')
    known = data.get("known_findings") or []
    if known:
        out += ["<h2>Known (not raised again)</h2>",
                _table(["Finding", "Where", "Owner's note"],
                       [[_e(f"{f['kind']}: {f['detail']}"), _link(f.get("url")), _e(f["known"])] for f in known])]

    out.append(_ux(data))
    smoke = data.get("smoke")
    if smoke:
        facts = [("Start", _link(smoke.get("start_url"))), ("Pages", _e(smoke.get("pages"))),
                 ("Links discovered", _e(smoke.get("discovered_links"))),
                 ("robots.txt", "respected" if smoke.get("robots_txt") else "none found"),
                 ("Delay", _e(f"{smoke.get('delay_s')} s between pages"))]
        out += ["<h2>Crawl</h2>", '<div class="card"><dl>',
                "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in facts), "</dl>"]
        if smoke.get("robots_disallowed"):
            out.append("<details><summary>Links robots.txt asked us not to visit</summary><ul>"
                       + "".join(f"<li>{_link(u)}</li>" for u in smoke["robots_disallowed"]) + "</ul></details>")
        out.append("</div>")

    out.append("<h2>Per scenario</h2>")
    out += [_scenario(i, r, decider(data)) for i, r in enumerate(data["scenarios"])]

    browser = data.get("browser") or {}
    owner = browser_owner(browser)
    run = [("Native", _e(f"{browser.get('engine')} game {browser.get('project')} (adapter {browser.get('adapter')}"
                         f"{', headless' if browser.get('headless') else ', windowed'})"))
           if browser.get("surface") == "native" else
           ("Browser", _e(f"{browser.get('cdp_url')} ({owner}{', headless' if browser.get('headless') else ''})")),
           *([("Game profile", _e(game_profile(browser)))] if game_profile(browser) else []),
           ("Cost", _e(f"{decisions_cost(data)}, text model "
                       f"${cost['usd_text']:.4f}" + ("" if cost["text_cost_reported"]
                                                     else " (provider did not report cost)")))]
    if data.get("models"):
        run.append(("Models", _e(f"{data['models'].get('decider') or 'Jev'} {data['models']['jev']}; "
                                 f"text {data['models']['text']}")))
        if data["models"].get("keys"):  # where QAJev's own keys came from: names only
            run.append(("Keys", _e("; ".join(data["models"]["keys"]))))
    if data.get("motion") in ("reduce", "full"):
        run.append(("Motion", "reduced (pages were told the visitor prefers reduced motion)"
                    if data["motion"] == "reduce" else "full (as-is)"))
    if (data.get("cpu_throttle") or 1) > 1:
        run.append(("CPU", _e(f"{data['cpu_throttle']}× slower (Chrome's CPU throttling)")))
    if data.get("watched_live_s"):
        run.append(("Watched live", f"{data['watched_live_s']:.0f} s in the dashboard"))
    if data.get("sign_in"):
        run.append(("Sign-in", _e(signed_in(data["sign_in"]))))
    run += [("Legend", "<br>".join(f"{_pill(o)} {_e(t)}" for o, t in LEGEND.items())),
            ("Files", '<a href="report.md">report.md</a> · <a href="report.json">report.json</a>'),
            ("QAJev", _e(data.get("qajev")) + (f" ({_e(data['qajev_commit'])})" if data.get("qajev_commit") else ""))]
    out += ["<h2>Run</h2>", '<div class="card"><dl>', "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in run),
            "</dl></div>", "</main></body></html>"]
    return "\n".join(o for o in out if o)
