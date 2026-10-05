"""UX notes for a page, measured (no model): contrast, keyboard reach and visible focus, 200% zoom, cut-off and
overlapping text, an open dialog's behaviour, and design consistency across pages.

José, 5 Oct: "I ideally want them all please... and then we add that as part of the report." Each note says what it
rests on (`basis`) and the rule or threshold it was measured against, so a measurement is never mistaken for a taste.
UX notes never change a run's PASS/FAIL. Everything here is free: no model calls.
"""

import json
from pathlib import Path

UX_FACTS = (Path(__file__).parent / "js" / "ux_facts.js").read_text()
MAX_TABS = 150

# The controls a keyboard user must be able to reach (WCAG 2.1.1), with the look of each before any focus (WCAG 2.4.7
# compares it with the look when focused). tabindex=-1 and disabled controls are out of the tab order on purpose.
CONTROLS_JS = """(() => {
  const visible = (e) => e.checkVisibility ? e.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) : true;
  const look = (e) => { const s = getComputedStyle(e); return [s.outlineStyle, s.outlineWidth, s.outlineColor,
    s.boxShadow, s.borderColor, s.backgroundColor, s.color, s.textDecorationLine].join('|'); };
  const path = (e) => { const self = e.tagName.toLowerCase() + (e.id ? '#' + CSS.escape(e.id)
      : [...e.classList].slice(0, 2).map((c) => '.' + CSS.escape(c)).join(''));
    const up = !e.id && e.parentElement && e.parentElement.closest('[id]');
    return up ? '#' + CSS.escape(up.id) + ' ' + self : self; };
  const said = (e) => (/^(INPUT|SELECT|TEXTAREA)$/.test(e.tagName)
    ? e.getAttribute('aria-label') || e.getAttribute('placeholder') || e.getAttribute('name') || e.type
    : e.getAttribute('aria-label') || e.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 40);
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  window.__qajevUx = new Map();
  const out = [];
  let n = 0;
  for (const e of document.querySelectorAll('a[href], button, input:not([type="hidden"]), select, textarea, ' +
      '[tabindex], [role="button"], [role="link"], [role="checkbox"], [role="tab"], [role="menuitem"], summary')) {
    if (!visible(e) || e.closest('[inert], [aria-hidden="true"]')) continue;
    if (e.matches(':disabled') || e.getAttribute('tabindex') === '-1') continue;
    const r = e.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const id = 'c' + (n++);
    e.dataset.qajevUx = id;
    window.__qajevUx.set(id, look(e));
    out.push({ id, path: path(e), said: said(e) });
  }
  return out;
})()"""

# Before the walk: keep each Tab keydown, so the page's own handling of it can be read once it is dispatched. A game
# binds Tab and cancels it (SideGame1, 5 Oct): then focus never moves, and that says nothing about keyboard reach.
TAB_WATCH_JS = """(() => {
  if (!window.__qajevTab) {
    window.__qajevTab = { last: null };
    addEventListener('keydown', (e) => { if (e.key === 'Tab') window.__qajevTab.last = e; }, true);
  }
  return true;
})()"""

# Whether the page is a game: a canvas covering half the viewport or more. A game may take Tab as one of its keys; an
# ordinary page that cancels Tab locks keyboard users out (Orchestrator, 5 Oct).
GAME_JS = """(() => [...document.querySelectorAll('canvas')].some((c) => {
  const r = c.getBoundingClientRect();
  return Math.max(0, Math.min(r.right, innerWidth) - Math.max(r.left, 0))
    * Math.max(0, Math.min(r.bottom, innerHeight) - Math.max(r.top, 0)) >= innerWidth * innerHeight / 2;
}))()"""

# After each Tab: which control has focus, whether it looks any different from before it had it, and whether the
# page cancelled that Tab.
FOCUSED_JS = """(() => {
  const t = window.__qajevTab, cancelled = !!(t && t.last && t.last.defaultPrevented);
  if (t) t.last = null;
  const e = document.activeElement;
  if (!e || e === document.body || e === document.documentElement) return { id: null, cancelled };
  const s = getComputedStyle(e);
  const now = [s.outlineStyle, s.outlineWidth, s.outlineColor, s.boxShadow, s.borderColor, s.backgroundColor, s.color,
    s.textDecorationLine].join('|');
  const id = e.dataset ? e.dataset.qajevUx || null : null;
  const r = e.getBoundingClientRect();
  return { id, changed: id ? now !== window.__qajevUx.get(id) : null, tag: e.tagName.toLowerCase(),
           onscreen: r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth, cancelled };
})()"""

OVERFLOW_JS = ("({ wide: document.documentElement.scrollWidth > innerWidth + 1, "
               "width: document.documentElement.scrollWidth, viewport: innerWidth })")

DIALOG_OPEN_JS = """(() => [...document.querySelectorAll(
    'dialog[open], [role="dialog"], [role="alertdialog"], [aria-modal="true"]')]
  .some((d) => (d.checkVisibility ? d.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) : true)
               && d.getBoundingClientRect().width > 0))()"""


def keyboard(session, controls):
    """Tab through the page with real key presses: which controls a keyboard reaches, which show no change when
    focused, and whether focus gets stuck. -> {reached, unreachable, no_focus_look, stuck, tabs, tab_taken, game}:
    tab_taken when the page cancelled every Tab and focus never moved; game when a canvas fills the page."""
    order, looks, stuck, last, repeats, cancelled = [], {}, None, None, 0, 0
    session.evaluate(TAB_WATCH_JS)
    game = bool(session.evaluate(GAME_JS))
    for tabs in range(1, min(MAX_TABS, 2 * len(controls) + 5) + 1):
        session.press("Tab")
        f = session.evaluate(FOCUSED_JS) or {}
        cancelled += bool(f.get("cancelled"))
        cid = f.get("id")
        repeats = repeats + 1 if cid and cid == last else 0
        if repeats >= 3:
            stuck = cid
            break
        last = cid
        if cid and cid in looks:  # round the page and back to a control already reached: the cycle is complete
            if order and cid == order[0]:
                break
            continue
        if cid:
            order.append(cid)
            looks[cid] = f.get("changed")
    by_id = {c["id"]: c for c in controls}
    pick = lambda ids: [{"path": by_id[i]["path"], "said": by_id[i]["said"]} for i in ids if i in by_id]  # noqa: E731
    unreachable = [c["id"] for c in controls if c["id"] not in looks]
    return {"controls": len(controls), "reached": len(order), "tabs": tabs if controls else 0,
            "unreachable": pick(unreachable)[:10], "unreachable_count": len(unreachable),
            "no_focus_look": pick([i for i in order if looks.get(i) is False])[:10],
            "no_focus_look_count": sum(1 for i in order if looks.get(i) is False),
            "stuck": pick([stuck])[0] if stuck else None,
            "tab_taken": bool(controls) and not order and cancelled == tabs, "game": game}


def zoomed(session, dev):
    """The page at 200% zoom (WCAG 1.4.4 and 1.4.10): half the CSS width and height at twice the scale, as a browser
    zoomed to 200% lays it out. -> {wide, width, viewport, clipped}. The device is put back after."""
    w, h = int(dev.get("width", 1280)), int(dev.get("height", 800))
    session.set_device({**dev, "width": w // 2, "height": h // 2, "scale": float(dev.get("scale", 1)) * 2})
    try:
        overflow = session.evaluate(OVERFLOW_JS) or {}
        facts = session.evaluate(UX_FACTS) or {}
    finally:
        session.set_device(dev)
    return {**overflow, "clipped": facts.get("clipped", [])[:10], "clipped_count": facts.get("clipped_count", 0)}


def measure(session, dev):
    """Every measured UX fact for the loaded page. The keyboard walk and the zoom run on a desktop layout only (a
    phone has no Tab key; its zoom is the phone's own layout). The Escape test runs last: it may close a dialog."""
    facts = session.evaluate(UX_FACTS) or {}
    out = {"facts": facts}
    if not dev.get("mobile"):
        controls = session.evaluate(CONTROLS_JS) or []
        out["keyboard"] = keyboard(session, controls)
        out["zoom"] = zoomed(session, dev)
    if facts.get("dialog"):
        session.press("Escape")
        out["dialog_escape_closes"] = not session.evaluate(DIALOG_OPEN_JS)
    return out


def _note(kind, detail, rule, samples=None):
    return {"basis": "measured", "kind": kind, "detail": detail, "rule": rule,
            **({"samples": samples} if samples else {})}


def notes(m):
    """The UX notes for one page's measurements (measure()): only what falls short of its rule."""
    facts, out = m.get("facts") or {}, []
    c = facts.get("contrast") or {}
    if c.get("low_count"):
        out.append(_note("low text contrast", f"{c['low_count']} of {c.get('measured', 0)} text(s) below WCAG AA",
                         "WCAG 2.2 1.4.3: 4.5:1, or 3:1 for text from 24px (18.66px bold)",
                         [f"'{x['text']}' {x['ratio']}:1 (needs {x['need']}:1; {x['fg']} on {x['bg']}, {x['size']}px; "
                          f"{x['path']})" for x in c.get("low", [])[:10]]))
    not_measured = {k: v for k, v in (c.get("skipped") or {}).items() if v and k != "disabled"}
    if not_measured:
        out.append({"basis": "measured", "kind": "contrast not measured", "rule": "WCAG 2.2 1.4.3",
                    "detail": ", ".join(f"{v} text(s) {k.replace('_', ' ')}" for k, v in not_measured.items())
                    + " (check those by eye)"})
    if facts.get("clipped_count"):
        out.append(_note("text cut off", f"{facts['clipped_count']} text(s) cut off by their own box",
                         "the box hides part of its own text (overflow hidden, no ellipsis)",
                         [f"'{x['text']}' ({x['cut']}: box {x['box']}, text {x['content']}; {x['path']})"
                          for x in facts.get("clipped", [])[:10]]))
    if facts.get("overlaps"):
        out.append(_note("text overlapping", f"{len(facts['overlaps'])} pair(s) of text boxes overlap",
                         "two text boxes, neither inside the other, overlap by 4 px or more each way",
                         [f"'{x['a']}' and '{x['b']}' ({x['area']} px; {x['aPath']} / {x['bPath']})"
                          for x in facts["overlaps"][:10]]))
    k = m.get("keyboard")
    if k and k.get("tab_taken") and k.get("game"):
        out.append(_note("keyboard not measurable", f"Tab is taken by the game: all {k['tabs']} Tab presses were "
                         "cancelled and focus never moved",
                         "WCAG 2.2 2.1.1, 2.4.7 and 2.1.2 not measured: the game handles Tab itself; check its own "
                         "keys by hand"))
    elif k and k.get("tab_taken"):
        out.append(_note("keyboard blocked", f"the page cancels Tab: all {k['tabs']} Tab presses were cancelled and "
                         f"focus never moved, so a keyboard user cannot reach its {k['controls']} control(s)",
                         "WCAG 2.2 2.1.1: everything works from a keyboard",
                         [f"'{x['said']}' ({x['path']})" for x in k["unreachable"]]))
    elif k:
        if k["unreachable_count"]:
            out.append(_note("not reachable by keyboard", f"{k['unreachable_count']} of {k['controls']} control(s) "
                             f"never got focus in {k['tabs']} Tab presses",
                             "WCAG 2.2 2.1.1: everything works from a keyboard",
                             [f"'{x['said']}' ({x['path']})" for x in k["unreachable"]]))
        if k["no_focus_look_count"]:
            out.append(_note("no visible focus", f"{k['no_focus_look_count']} control(s) look the same with and "
                             "without keyboard focus", "WCAG 2.2 2.4.7: focus is visible (outline, shadow, border, "
                             "background, colour or underline changes)",
                             [f"'{x['said']}' ({x['path']})" for x in k["no_focus_look"]]))
        if k["stuck"]:
            out.append(_note("keyboard trap", f"focus stayed on '{k['stuck']['said']}' ({k['stuck']['path']}) for 3 "
                             "Tab presses", "WCAG 2.2 2.1.2: no keyboard trap"))
    z = m.get("zoom")
    if z:
        if z.get("wide"):
            out.append(_note("sideways scroll at 200% zoom",
                             f"the page is {z['width']} px wide in a {z['viewport']} px viewport",
                             "WCAG 2.2 1.4.10: content reflows without scrolling sideways"))
        if z.get("clipped_count"):
            out.append(_note("text cut off at 200% zoom", f"{z['clipped_count']} text(s) cut off by their own box",
                             "WCAG 2.2 1.4.4: text resizes to 200% without losing content",
                             [f"'{x['text']}' ({x['path']})" for x in z["clipped"]]))
    d = facts.get("dialog")
    if d:
        issues = [why for ok, why in ((d["named"], "has no accessible name"), (d["modal"], "is not marked modal"),
                                      (d["focus_inside"], "does not hold focus"),
                                      (m.get("dialog_escape_closes", True), "does not close on Escape")) if not ok]
        if issues:
            out.append(_note("dialog", f"the open dialog '{d['text']}' ({d['path']}) " + ", ".join(issues),
                             "WAI-ARIA dialog pattern: named, aria-modal, focus moves in, Escape closes"))
    return out


def consistency(pages):
    """Design consistency across pages: for each kind of element (h1, body text, button...), the style most pages
    use, and every page whose style differs, naming each differing property with both values.
    `pages`: [(page name, its facts' styles)] for one device. -> notes."""
    out = []
    kinds = sorted({k for _, styles in pages for k in styles})
    for kind in kinds:
        usage = {}  # style (JSON) -> pages using it as their main style for this kind
        for name, styles in pages:
            main = (styles.get(kind) or [None])[0]
            if main:
                usage.setdefault(json.dumps(main["style"], sort_keys=True), []).append((name, main))
        if len(usage) < 2:
            continue
        ranked = sorted(usage.items(), key=lambda kv: -len(kv[1]))
        canon = json.loads(ranked[0][0])
        for style_json, users in ranked[1:]:
            style = json.loads(style_json)
            diff = [f"{f} {style.get(f)} (vs {canon.get(f)})" for f in canon if style.get(f) != canon.get(f)]
            where = ", ".join(n for n, _ in users[:5]) + (f" and {len(users) - 5} more" if len(users) > 5 else "")
            out.append({"basis": "measured", "kind": f"{kind} style differs",
                        "detail": f"{where}: " + "; ".join(diff) + f"; {len(ranked[0][1])} page(s) use the other",
                        "rule": "the same kind of element looks the same on every page (computed styles compared)",
                        "samples": [f"'{u['example']}' ({u['path']})" for _, u in users[:3]]})
    return out


UNSURE_P, UNSURE_GAP, SCROLLS = 0.8, 0.25, 4


def _struggle(kind, detail, rule, samples=None):
    return {"basis": "struggle", "kind": kind, "detail": detail, "rule": rule,
            **({"samples": samples} if samples else {})}


def struggle(history, screens, outcome, assists=()):
    """What Jev's own run says about how findable the goal was (free: from the run already made). A struggle signal is
    evidence, not a verdict: a person may find a page Jev hesitated on, and the reverse.
    history: Jev's executed actions (kind, action, url); screens: verdict.screens() per decision; assists: QAJev's
    scrolls after Jev stopped. -> notes."""
    if not history:
        return []
    pages = []
    for h in history:
        url = (h.get("url") or "").split("#", 1)[0]
        if url and (not pages or pages[-1] != url):
            pages.append(url)
    backs = [pages[i] for i in range(2, len(pages)) if pages[i] in pages[:i - 1]]  # back to a page left earlier
    scrolls = sum(1 for h in history if h.get("kind") == "scroll")
    unsure = [s for s in screens if s.get("p") is not None and s.get("runner_up_p") is not None
              and s["p"] < UNSURE_P and s["p"] - s["runner_up_p"] <= UNSURE_GAP]
    below = sum(a.get("scrolled", 0) for a in assists if a.get("after") == "visible")
    reached = "reached" if outcome == "pass" else f"not reached ({outcome})"
    parts = [f"{len(history)} action(s) over {len(pages)} page(s)", f"{len(backs)} backtrack(s)",
             f"{scrolls} scroll(s)", f"{len(unsure)} unsure step(s)"]
    if below:
        parts.append(f"QAJev scrolled {below} more screen(s) to bring it on screen")
    out = [_struggle("findability", f"{reached} in " + ", ".join(parts),
                     "Jev's own run: its actions, the pages it went through, and how sure each choice was")]
    if unsure:
        out.append(_struggle("unclear choice", f"{len(unsure)} step(s) where two options looked almost equally right",
                             f"Jev's top choice under {UNSURE_P} with the runner-up within {UNSURE_GAP}",
                             [f"step {s['step']}: '{s['next_step']}' ({s['p']}) vs '{s['runner_up']}' "
                              f"({s['runner_up_p']})" for s in unsure[:5]]))
    if backs:
        out.append(_struggle("backtracked", f"went back to {len(backs)} page(s) it had already left",
                             "a page visited again after another page", backs[:5]))
    if scrolls >= SCROLLS:
        out.append(_struggle("searched by scrolling", f"{scrolls} scrolls before the run ended",
                             f"{SCROLLS} or more scrolls: what Jev needed was not near the top"))
    if below:
        out.append(_struggle("below the fold", f"the expected text was {below} screen(s) below where Jev stopped",
                             "QAJev scrolled for Jev, as a person reading on would"))
    return out
