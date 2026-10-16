"""One guarded browser tab driven by Jev.

Order matters and every step here was learned from an incident or a measured miss:
  1. BU_CDP_URL / BU_NAME are set before jev_ultrafast is imported (browser_harness reads them at import).
  2. The tab opens on about:blank, so no site byte loads before the guard is armed.
  3. Page.enable BEFORE Page.addScriptToEvaluateOnNewDocument, or later documents run unguarded.
  4. The guard is re-proven right before every action Jev executes (fail closed).
"""

import contextlib
import functools
import json
import os
import re
import subprocess
import threading
import time
import uuid
from string import Template
from types import SimpleNamespace
from typing import Any

from . import __version__, chrome, keys, live, netlog, providers
from . import guard as guard_mod
from . import pad as pad_mod
from .config import is_loopback

_jev = None


class GuardMissing(RuntimeError):
    pass


class HookFailed(RuntimeError):
    pass


class LeftSite(RuntimeError):
    pass


def configure_env(cdp_url, name=None):
    """One daemon name per process: browser_harness binds BU_NAME when it is imported, so once jev is loaded
    a pooled worker that runs a second chain must keep the name it started with."""
    if _jev is not None:
        if os.environ.get("BU_CDP_URL") != cdp_url:
            raise RuntimeError("this process is already bound to another Chrome; use a new process")
        return os.environ["BU_NAME"]
    os.environ["BU_CDP_URL"] = cdp_url
    os.environ["BU_NAME"] = name or f"qajev-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    os.environ.setdefault("BH_UPDATE_CHECK", "0")
    return os.environ["BU_NAME"]


DAEMON_LINE = 65536  # browser_harness's daemon reads each command as one line of at most 64 KiB (asyncio's default)


def too_long(method, params, error):
    """A command over the daemon's line limit, said plainly with its size, or None for any other error. The daemon
    answers a command a little over it with "Separator is found, but chunk is longer than limit", and closes the
    socket on a far larger one while it is still being sent (BrokenPipeError: 1.8 MB, 5 Oct)."""
    size = len(json.dumps(params, default=str))
    if size <= DAEMON_LINE and "than limit" not in str(error) and "exceed the limit" not in str(error):
        return None
    return (f"{method}: a {size / 1024:.0f} KB command is over the browser daemon's {DAEMON_LINE // 1024} KB limit "
            f"per command ({error})")


def load(ledger):
    """Import jev once per process and apply QAJev's patches. Returns a namespace of the modules."""
    global _jev
    if _jev is not None:
        _jev.model.post_json = providers.route(providers.metered(
            _jev.model.CLIENT, ledger, verified_bounds=_jev.providers.get("verified_model_bounds")), _jev.providers)
        return _jev
    if "BU_NAME" not in os.environ:
        raise RuntimeError("configure_env() must run before load()")
    from browser_harness import admin
    from jev_ultrafast import agent as jev_agent
    from jev_ultrafast import browser as jev_browser
    from jev_ultrafast import model as jev_model

    timeout = float(os.environ.get("QAJEV_CDP_TIMEOUT", "30"))
    raw_cdp = jev_browser.cdp

    def patient_cdp(method, session_id=None, **params):
        # The 5 s default is bound as a default argument; under load it failed ~1 call in 8.
        params.pop("_response_timeout", None)
        try:
            try:
                return raw_cdp(method, session_id=session_id, _response_timeout=timeout, **params)
            except TimeoutError:
                return raw_cdp(method, session_id=session_id, _response_timeout=timeout, **params)
        except (RuntimeError, OSError) as e:
            said = too_long(method, params, e)
            if said:
                raise RuntimeError(said) from e
            raise

    jev_browser.cdp = patient_cdp

    raw_init = jev_browser.Browser.__init__

    def init_or_close(self, url):
        # Browser() creates its tab before the first setup call; a failed start must not leak it.
        try:
            raw_init(self, url)
        except Exception:
            if getattr(self, "target", None):
                chrome.close_target(os.environ["BU_CDP_URL"], self.target)
            raise

    jev_browser.Browser.__init__ = init_or_close

    raw_field_text = jev_model.field_text

    def field_text_retry(context):
        # The text model sometimes wraps its JSON in a code fence; a retry almost always fixes it.
        for attempt in range(3):
            try:
                return raw_field_text(context)
            except ValueError as e:
                if "no valid field value" not in str(e) or attempt == 2:
                    raise

    jev_model.field_text = field_text_retry
    jev_agent.field_text = field_text_retry  # agent.py imported it by name

    resolved = providers.apply()
    raw_post_json = jev_model.post_json
    jev_model.post_json = providers.route(providers.metered(
        jev_model.CLIENT, ledger, verified_bounds=resolved.get("verified_model_bounds")), resolved)
    _jev = SimpleNamespace(admin=admin, agent=jev_agent, browser=jev_browser, model=jev_model,
                           raw_post_json=raw_post_json, cdp=patient_cdp, providers=resolved)
    return _jev


JS_WAIT_MS = 5000
# The guard's record that nobody has read yet (blocked and allowed writes, dialogs), taken from one document
DRAIN_JS = "window.__qajev && window.__qajev.drain ? window.__qajev.drain() : null"
# A frame's record: the same, plus what its console said and its errors (frame_console)
FRAME_DRAIN_JS = ("window.__qajev && window.__qajev.drain ? Object.assign(window.__qajev.drain(), {frame_console: "
                  "window.__qajev.drainConsole ? window.__qajev.drainConsole() : []}) : null")


def awaited(expression, timeout_ms=JS_WAIT_MS):
    """JS that judges `expression` by what it settles to: a Promise is awaited (never truthy just for being pending),
    and a throw, a rejection or no answer within `timeout_ms` becomes {error}. -> {value} | {error}, as JS source."""
    return (f"(async () => {{ try {{ const v = await Promise.race([(async () => ({expression}))(), "
            f"new Promise((_, no) => setTimeout(() => no(new Error('no answer within {timeout_ms} ms')), "
            f"{timeout_ms}))]); return {{ value: v === undefined ? null : v }}; }} "
            "catch (e) { return { error: String(e && e.message || e) }; } })()")


# One tick of a react hook: done when `until` holds, else the policy's key actions. A throw is the policy's error.
REACT_JS = Template("""(async () => {
  try {
    if (await (async () => ($until))()) return { done: true };
    return { actions: await (async () => ($js))() };
  } catch (e) { return { error: String(e && e.message || e) }; }
})()""")


PROBE_JS = Template("""(async () => {
  const q = window.__qajev;
  const probe = q ? q.probe() : null;
  // Visible text as a person reads it: CSS text-transform applies, and runs of whitespace (line breaks from <br>,
  // wrapped markup) count as one space on both sides. ignore_case for labels styled in capitals.
  const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
  const body = norm(document.body && document.body.innerText);
  const spec = $spec;
  const fold = (s) => spec.ci ? s.toLowerCase() : s;
  const page = fold(body);
  const has = (t) => page.includes(fold(norm(t)));
  // On screen, as a person sees it (FlockTab1, 6 Oct): the smallest elements holding the text, visible, inside the
  // viewport, drawn on top (no overlay over its lines) and whole (not clipped by an overflow box, as an ellipsis
  // does). -> {ok, why}: why says what a person sees instead.
  const name = (e) => e.tagName.toLowerCase() + (e.id ? '#' + e.id : e.classList.length ? '.' + e.classList[0] : '');
  const inView = (r) => r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < innerHeight
    && r.left < innerWidth;
  // The range of the wanted words inside e (whitespace as a person reads it), or null when it cannot be placed.
  const wordsIn = (e, want) => {
    const nodes = [], walk = document.createTreeWalker(e, NodeFilter.SHOW_TEXT);
    let raw = '';
    for (let n = walk.nextNode(); n; n = walk.nextNode()) { nodes.push([n, raw.length]); raw += n.data; }
    const pattern = want.split(' ').map(w => w.replace(/[.*+?^$${}()|[\\]\\\\]/g, '\\\\$$&')).join('\\\\s+');
    const m = new RegExp(pattern, spec.ci ? 'i' : '').exec(raw);
    if (!m) return null;
    const at = (i) => {
      let k = nodes.length - 1;
      while (k > 0 && nodes[k][1] > i) k--;
      return [nodes[k][0], i - nodes[k][1]];
    };
    const range = document.createRange();
    range.setStart(...at(m.index)); range.setEnd(...at(m.index + m[0].length));
    return range;
  };
  // What is drawn over e's words instead of them, or null when they are on top somewhere.
  // SHORTCUT: elementFromPoint skips `pointer-events: none`, so an OPAQUE cover that lets clicks through is missed
  // (a see-through badge rightly is). Upgrade to elementsFromPoint plus each layer's paint (opacity, background) when
  // an agent reports such a cover.
  const coveredBy = (e, rects) => {
    let over = null;
    for (const r of rects) {
      if (!inView(r)) continue;
      const hit = document.elementFromPoint(Math.min(r.left + r.width / 2, innerWidth - 1),
                                            Math.min(r.top + r.height / 2, innerHeight - 1));
      if (!hit || hit === e || e.contains(hit) || hit.contains(e)) return null;
      over = hit;
    }
    return over;
  };
  // The overflow box that clips the words, when they run past its edge.
  const clippedBy = (e, rects) => {
    for (let a = e; a && a !== document.body; a = a.parentElement) {
      const s = getComputedStyle(a);
      if (s.overflowX === 'visible' && s.overflowY === 'visible') continue;
      const box = a.getBoundingClientRect();
      if (rects.some(r => r.right > box.right + 1 || r.bottom > box.bottom + 1 || r.left < box.left - 1)) return a;
    }
    return null;
  };
  const onScreen = (t) => {
    const want = fold(norm(t));
    const holders = [...document.querySelectorAll('body *')].filter(e => fold(norm(e.textContent)).includes(want));
    const smallest = holders.filter(e => ![...e.children].some(c => fold(norm(c.textContent)).includes(want)));
    let why = 'not in the page';
    for (const e of smallest) {
      if (!(e.checkVisibility ? e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) : true)
          || !fold(norm(e.innerText)).includes(want)) {
        // Hidden by QAJev itself: the result must say so (verse-workshop #302).
        const g = e.closest('[data-qajev-guard]');
        why = g && g.dataset.qajevGuard !== 'field'
          ? "hidden by QAJev's guard (" + g.dataset.qajevGuard.slice(0, 80) + "); "
            + 'if it is safe, allow it in the suite: guard: {allow: [...]}'
          : 'in the page but hidden';
        continue;
      }
      if (!inView(e.getBoundingClientRect())) { why = 'in the page but not in the viewport'; continue; }
      const range = wordsIn(e, norm(t));
      const rects = range ? [...range.getClientRects()].filter(r => r.width > 0 && r.height > 0) : [];
      const lines = rects.length ? rects : [e.getBoundingClientRect()];
      const over = coveredBy(e, lines);
      if (over) { why = 'covered by ' + name(over) + ': "' + norm(over.innerText).slice(0, 60) + '"'; continue; }
      const clip = range && clippedBy(e, lines);
      if (clip) {
        why = 'cut short: ' + name(clip) + ' shows "' + norm(clip.innerText).slice(0, 60) + '" (clipped)';
        continue;
      }
      return { ok: true, why: null };
    }
    return { ok: false, why };
  };
  // A missing text's closest match: the longest start of it (4 characters at least) that the page has, with the
  // words around it, so a failed check shows "overlapFrames=5" next to the "overlapFrames=0" it wanted.
  const nearest = (t) => {
    const want = fold(norm(t));
    for (let n = want.length - 1; n >= 4; n--) {
      const at = page.indexOf(want.slice(0, n));
      if (at >= 0) {
        let from = Math.max(0, at - 40);
        const space = body.indexOf(' ', from);
        if (from > 0 && space >= 0 && space < at) from = space + 1;  // start on a word
        return body.slice(from, at + n + 80).trim();
      }
    }
    return null;
  };
  // A missing text the guard hid (data-qajev-guard): the result names the guard, not the page.
  const guarded = spec.text.map(t => {
    if (has(t)) return null;
    const want = fold(norm(t));
    const el = [...document.querySelectorAll('[data-qajev-guard]')]
      .find(e => e.dataset.qajevGuard !== 'field' && fold(norm(e.textContent)).includes(want));
    return el ? el.dataset.qajevGuard.slice(0, 80) : null;
  });
  let js = null;
  if (spec.js) {
    const r = await $js;
    // What came back, as is: verdict.js_result passes only exactly true. A value that is not plain data (an element,
    // a function) is sent as its type, since it cannot cross to Python.
    const v = r.value, plain = v === null || ['boolean', 'string', 'number'].includes(typeof v);
    let data = plain;
    if (!plain && typeof v === 'object' && !(v instanceof Node)) {
      try { JSON.stringify(v); data = true; } catch (e) {}
    }
    const kind = v instanceof Element ? 'element' : v instanceof Node ? 'node' : typeof v;
    js = 'error' in r ? { error: r.error } : data ? { value: v } : { type: kind, shown: String(v).slice(0, 200) };
  }
  let status = null;
  try { status = performance.getEntriesByType('navigation')[0].responseStatus || null; } catch (e) {}
  const seen = spec.visible.map(onScreen);
  return { probe, url: location.href, title: document.title, status,
    text: spec.text.map(t => has(t)), guarded, absent: spec.absent.map(t => has(t)), visible: seen.map(s => s.ok),
    visible_why: seen.map(s => s.why),
    near: spec.text.map(t => has(t) ? null : nearest(t)), js, says: body.slice(0, 400) };
})()""")

FIND_JS = Template("""(() => {
  const all = document.querySelectorAll($selector);
  const el = all[0];
  if (!el) return { error: 'no element matches ' + $selector };
  // A broad selector whose first match is guarded says so (dash-wholesale-links, 13 Oct: Save matched Sign out first).
  // A later match is never clicked instead: the suite says which control it means.
  if (el.dataset.qajevGuard) return { error: 'refused: the guard hid this control (' + el.dataset.qajevGuard + ')' +
    (all.length > 1 ? '; your selector matches ' + all.length + ' elements and this one comes first: make it match ' +
      'only the control you mean' : '') };
  const secret = el.matches('input[type=password],input[autocomplete^="cc-"],input[name*=card i],input[name*=cvc i],' +
    'input[name*=cvv i],input[autocomplete=one-time-code],input[autocomplete$$=-password]');
  el.scrollIntoView({ block: 'center', inline: 'center' });
  const r = el.getBoundingClientRect();
  const x = r.x + r.width / 2, y = r.y + r.height / 2;
  const hit = document.elementFromPoint(x, y);
  const covered = !(hit && (el === hit || el.contains(hit)));
  const over = covered && hit ? hit.tagName.toLowerCase() + (hit.id ? '#' + hit.id : hit.classList.length ?
    '.' + hit.classList[0] : '') : null;
  return { x, y, secret, covered, over, tag: el.tagName };
})()""")

# What a fill left in its field (null: no such field, or one with no value).
VALUE_JS = Template("""(() => { const el = document.querySelector($selector);
  return el && typeof el.value === 'string' ? el.value : null; })()""")
# A field Chrome edits through a picker ignores typed text (dash-wholesale-links, 12 Oct: fill "2027-06-30" left a date
# field on 06/30/2025). Its value is set as a person's edit sets it: the native setter, then input and change events.
# -> the value it holds after, or null for any other field (typed as usual).
PICKER_TYPES = ["date", "time", "datetime-local", "month", "week"]
SET_PICKER_JS = Template("""(() => {
  const el = document.querySelector($selector);
  if (!el || el.tagName !== 'INPUT' || !$types.includes(el.type)) return null;
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, $text);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return el.value;
})()""")

# Chrome drops a click into an iframe sent too soon after the page loads: bare clicks at once landed 0 of 12, and 4 of
# 4 after 0.5 s (measured on macOS, Chrome 153, 12 Oct 2026). The frame's load event had already fired, and two
# animation frames did not help, so there is no event to wait on yet.
# SHORTCUT: a fixed wait. Revisit if a click into a frame is lost after it, or when Chrome exposes input readiness.
FRAME_INPUT_S = 0.75

# click_at: the point $x, $y (CSS pixels) from the top-left corner of $selector's element (or of the viewport when it
# is null), in view, and what the page has at that point. A frame element is the hit for any point inside its frame.
AT_JS = Template("""(() => {
  let x = $x, y = $y, el = null;
  if ($selector !== null) {
    el = document.querySelector($selector);
    if (!el) return { error: 'no element matches ' + $selector };
    if (el.dataset.qajevGuard) return { error: 'refused: the guard hid this control (' + el.dataset.qajevGuard + ')' };
    el.scrollIntoView({ block: 'center', inline: 'center' });
    const r = el.getBoundingClientRect();
    if (x > r.width || y > r.height)
      return { error: 'the point ' + x + ', ' + y + ' is outside ' + $selector + ' (' + Math.round(r.width) + ' x ' +
               Math.round(r.height) + ')' };
    x += r.x; y += r.y;
  }
  if (x < 0 || y < 0 || x > innerWidth || y > innerHeight)
    return { error: 'the point ' + Math.round(x) + ', ' + Math.round(y) + ' is outside the viewport' };
  const hit = document.elementFromPoint(x, y);
  const covered = el !== null && !(hit && (el === hit || el.contains(hit)));
  const over = hit ? hit.tagName.toLowerCase() + (hit.id ? '#' + hit.id : hit.classList.length ?
    '.' + hit.classList[0] : '') : 'nothing';
  return { x, y, covered, over };
})()""")

# Why a decision went stale, read before Jev looks again: Jev's freshness keys now (its pageKey and the target's
# guard, as Browser.fresh compares them) and the first of Browser.act's target checks that fails.
STALE_JS = Template("""(() => {
  const c = window.__jevFast;
  if (!c) return { target: null, key: null, guard: null };
  const e = $node === null ? null : c.nodes.get($node);
  const named = (el) => {
    if (!el) return 'nothing';
    const cls = typeof el.className === 'string' ? el.className.trim().split(/\\s+/).slice(0, 2).join('.') : '';
    const words = String(el.innerText || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim().slice(0, 40);
    return el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (cls ? '.' + cls : '') +
      (words ? ' "' + words + '"' : '');
  };
  let target = null;
  if ($node !== null) {
    if (!e || !e.isConnected) target = 'gone from the page';
    else if (e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]')) target = 'disabled';
    else if (!e.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) target = 'hidden';
    else {
      const r = e.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;
      if (!r.width || !r.height || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) target = 'off the screen';
      else {
        const hit = document.elementFromPoint(x, y);
        if (!e.contains(hit)) target = 'covered by ' + named(hit);
      }
    }
  }
  return { target, key: c.pageKey(), guard: e && e.isConnected ? c.guard(e) : null };
})()""")
# Jev's pageKey and guard arrays, item by item (jev_ultrafast/snapshot.js)
PAGE_KEY = ("page load", "address", "scroll x", "scroll y", "width", "height", "form fields")
GUARD = ("element", "role", "name", "value", "checked", "selected option", "read-only", "disabled", "aria-disabled",
         "aria-expanded", "aria-checked", "aria-selected", "link", "text around it")


def _first_difference(before, after):
    """The first lines that differ between two texts: ("Score 41", "Score 42")."""
    import difflib

    a, b = str(before or "").splitlines(), str(after or "").splitlines()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            return " / ".join(a[i1:i2])[:60], " / ".join(b[j1:j2])[:60]
    return None


def _items(names, before, after):
    """What changed between two of Jev's freshness arrays, named."""
    out = []
    for name, a, b in zip(names, before or [], after or [], strict=False):
        if a == b:
            continue
        if name == "text around it":
            diff = _first_difference(a, b)
            out.append(f"text around it: {diff[0]!r} → {diff[1]!r}" if diff else "text around it")
        elif name in {"page load", "form fields"}:
            out.append("a new page load" if name == "page load" else "a form field's value")
        else:
            out.append(f"{name}: {str(a)[:40]!r} → {str(b)[:40]!r}")
    return out


# A clock or a countdown as a person reads it: "0:03", "188:26", "1:05:09", "59 s", "30 sec", "5 min".
_TICKING = re.compile(r"\b\d+:\d\d(?::\d\d)?\b|\b\d+\s*(?:s|secs?|seconds?|mins?|minutes?)\b")


def _ticked(value):
    """Words as a person reads them, with each clock time or countdown as one mark: a clock, a countdown or a video
    timer that ticks reads the same. Other digits stay exact: a step, a count, a price or a label that changes is the
    page moving on."""
    return _TICKING.sub("#", value) if isinstance(value, str) else value


def _marker_read(marker):
    """Jev's page marker [timeOrigin, href, scrollX, scrollY, innerWidth, innerHeight, title, text, actions, inputs]
    with its words ticked (title, text, the actions' labels); the rest stays exact."""
    if not isinstance(marker, list) or len(marker) != 10:
        return marker
    actions = [{k: _ticked(v) if k in ("label", "current_value") else v for k, v in a.items()}
               if isinstance(a, dict) else a for a in marker[8] or []]
    return [*marker[:6], _ticked(marker[6]), _ticked(marker[7]), actions, marker[9]]


def _guard_read(guard):
    """Jev's target guard with its name and the text of its card ticked; its state and href stay exact."""
    if not isinstance(guard, list) or len(guard) < 3:
        return guard
    return [*guard[:2], _ticked(guard[2]), *guard[3:-1], _ticked(guard[-1])]


def fresh_past_ticks(browser, marker_js, page, action=None):
    """Jev's Browser.fresh, but digits ticking in what a person reads are not a change (verse2, 6 Oct: a video timer
    in the HUD, "0:03 / 188:26", made every move stale before it acted). New words, the address, a reload, the scroll,
    an input's value and a link's href still are: the decision is then made again."""
    if action is not None and action["kind"] in {"click", "select"}:
        node = action["node"]
        if type(node) is not int:
            return False
        now = browser.evaluate("(() => { const c=window.__jevFast; "
                               f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()")
        if not now:
            return False
        return now[0] == page["page_key"] and _guard_read(now[1]) == _guard_read(page["guards"].get(str(node)))
    return _marker_read(browser.evaluate(marker_js)) == _marker_read(page["marker"])


def page_changes(before, after, now=None, node=None):
    """What changed between the page Jev decided on (`before`) and the page now (`after`, a new observation; `now`,
    Jev's freshness keys read before it): words, at most six."""
    out = []
    if now:
        out += _items(PAGE_KEY, before.get("page_key"), now.get("key"))
        if node is not None:
            guard = (before.get("guards") or {}).get(str(node))
            if now.get("guard") != guard:
                out += [f"target's {x}" for x in _items(GUARD, guard, now.get("guard"))] or ["the target"]
    for name, a, b in (("address", before.get("url"), after.get("url")), ("title", before.get("title"),
                                                                           after.get("title"))):
        if a != b and not any(x.startswith(f"{name}:") for x in out):
            out.append(f"{name}: {str(a)[:60]!r} → {str(b)[:60]!r}")
    a, b = (before.get("scroll") or {}).get("y"), (after.get("scroll") or {}).get("y")
    if a != b and not any(x.startswith("scroll") for x in out):
        out.append(f"scroll y: {a} → {b}")
    diff = _first_difference(before.get("text"), after.get("text"))
    if diff:
        out.append(f"text: {diff[0]!r} → {diff[1]!r}")
    def labels(page):
        return {a.get("label") for a in page.get("actions") or [] if a.get("kind") not in {"scroll", "wait"}}

    gone, came = sorted(labels(before) - labels(after))[:3], sorted(labels(after) - labels(before))[:3]
    if gone or came:
        out.append("controls: " + ", ".join([f"-{x!r}" for x in gone] + [f"+{x!r}" for x in came]))
    return out[:6]


# The browser's own user-agent client hints, in CDP's UserAgentMetadata shape: an override without them drops them.
UA_HINTS_JS = """(async () => {
  const d = navigator.userAgentData;
  if (!d) return null;
  const v = await d.getHighEntropyValues(["architecture", "bitness", "model", "platformVersion", "fullVersionList",
                                          "wow64", "formFactors"]);
  const out = {brands: d.brands, mobile: d.mobile, platform: d.platform};
  for (const k of ["fullVersionList", "platformVersion", "architecture", "model", "bitness", "wow64", "formFactors"])
    if (v[k] !== undefined) out[k] = v[k];
  return out;
})()"""


_HINTS = {}  # each Chrome's client hints (by its CDP address), read once it has read them


def client_hints(cdp, cdp_url):
    """The browser's own user-agent client hints, or None. navigator.userAgentData exists only in a secure context,
    which QAJev's about:blank tab is not. Chrome's own debugging page on 127.0.0.1 is one, and loads nothing from a
    site, so a throwaway tab reads them there once per Chrome."""
    if cdp_url in _HINTS:
        return _HINTS[cdp_url]
    if not is_loopback(cdp_url):
        return None
    hints, target = None, None
    try:
        target = cdp("Target.createTarget", url=f"{cdp_url}/json/version", background=True)["targetId"]
        tab = cdp("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            got = cdp("Runtime.evaluate", session_id=tab, expression=f"isSecureContext && {UA_HINTS_JS}",
                      returnByValue=True, awaitPromise=True)
            hints = (got.get("result") or {}).get("value")
            if isinstance(hints, dict):
                break
            time.sleep(0.1)  # still the new tab's first about:blank
    except (RuntimeError, TimeoutError, KeyError):
        pass
    finally:
        if target:
            with contextlib.suppress(RuntimeError, TimeoutError):
                cdp("Target.closeTarget", targetId=target)
    if not isinstance(hints, dict):
        return None  # not kept: the next session tries again, so one failed read does not cost every later page
    _HINTS[cdp_url] = hints
    return hints


def with_qajev(user_agent):
    """A user agent with QAJev's name on the end, once: "... Chrome/141.0.0.0 Safari/537.36 QAJev/<version>"."""
    return user_agent if "QAJev/" in user_agent else f"{user_agent} QAJev/{__version__}"


def as_chrome(user_agent):
    """Headless Chrome's user agent as a normal Chrome's: "HeadlessChrome/155" says "Chrome/155". Some sites refuse a
    headless browser, and QAJev tests what a person gets (QAJev Cloud's desktop Chrome, 14 Oct 2026)."""
    return user_agent.replace("HeadlessChrome/", "Chrome/")


def as_chrome_hints(hints):
    """Headless Chrome's client hints as a normal Chrome's: its "HeadlessChrome" brand is "Google Chrome" again. A copy,
    so the hints kept for this Chrome do not change."""
    if not hints:
        return hints
    out = dict(hints)
    for key in ("brands", "fullVersionList"):
        if isinstance(out.get(key), list):
            out[key] = [{**b, "brand": "Google Chrome"} if isinstance(b, dict) and b.get("brand") == "HeadlessChrome"
                        else b for b in out[key]]
    return out


class Tab:
    """What a Session uses of Jev's Browser (its tab, and calls into it), for a tab in a browser context of its own:
    a multiplayer client's own cookies, storage, cache and service workers (clients.py). No Jev on it."""

    def __init__(self, cdp, ensure_daemon):
        self.cdp = cdp
        # Jev's Browser starts the browser daemon itself; a Tab must too, or a run whose only scenarios have clients
        # has no daemon to talk to (FileNotFoundError on its socket: the first live run of clients.py)
        ensure_daemon()
        self.context = cdp("Target.createBrowserContext")["browserContextId"]
        self.target = self.session = None
        try:
            # QAJev's rules hold per browser context: the default context's settings do not reach a new one
            cdp("Browser.setDownloadBehavior", behavior="deny", browserContextId=self.context)
            for name in ("microphone", "camera"):
                cdp("Browser.setPermission", permission={"name": name}, setting="denied",
                    browserContextId=self.context)
            self.target = cdp("Target.createTarget", url="about:blank", browserContextId=self.context,
                              background=True)["targetId"]
            self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
            self.call("Emulation.setFocusEmulationEnabled", enabled=True)  # renders and runs rAF like a focused tab
        except Exception:
            self.close()
            raise

    def call(self, method, **params):
        return self.cdp(method, session_id=self.session, **params)

    def close(self):
        if self.target:
            with contextlib.suppress(RuntimeError, TimeoutError):
                self.cdp("Target.closeTarget", targetId=self.target)
            self.target = None
        if self.context:
            with contextlib.suppress(RuntimeError, TimeoutError):  # its cookies and storage go with it
                self.cdp("Target.disposeBrowserContext", browserContextId=self.context)
            self.context = None


class Session:
    def __init__(self, ledger, *, headless=False, hosts=(), guard_opts=None, allow_commands=False, cwd=None,
                 motion="reduce", isolated=False, cpu_throttle=1):
        """isolated: a multiplayer client (clients.py): its own browser context and tab, and no Jev agent."""
        self.ledger = ledger
        self.motion = motion
        self.color_scheme = None
        self.jev = load(ledger)
        self.headless = headless
        self.hosts = set(hosts)
        self.guard_opts = guard_opts or {}
        self.allow_commands = allow_commands
        self.cwd = cwd
        self.script_id = None
        self.guard_cfg = None
        self.device = None
        self.user_agent = None  # the browser's own, with QAJev's name on the end, and its client hints
        self.assists = []
        self.last_stale = None
        self.net = None
        self.carried = {}  # what a page's guard recorded before the tab left it, for the next probe
        self.loaded_at = 0.0  # when the last navigation or reload ended (a click into a frame waits FRAME_INPUT_S)
        self.last_test_page = None  # the page the last test left this tab on (begin_test): its frames are named
        # Jev's agent and its Browser; a multiplayer client has a Tab and no agent (only Jev's own paths use one)
        self.agent: Any
        self.browser: Any
        if isolated:
            self.agent = None
            self.browser = Tab(self.jev.cdp, self.jev.admin.ensure_daemon)
        else:
            Agent = self.jev.agent.Agent
            self.agent = Agent("about:blank", "Wait for instructions.")
            self.browser = self.agent.browser
            # every freshness check Jev makes (before choosing, before typing, before input) goes through this
            self.browser.fresh = functools.partial(fresh_past_ticks, self.browser, self.jev.browser.MARKER)
        try:
            self.call("Page.enable")  # must precede addScriptToEvaluateOnNewDocument
            if motion == "reduce":  # before the first navigation, so the first paint already honours it
                self.call("Emulation.setEmulatedMedia",
                          features=[{"name": "prefers-reduced-motion", "value": "reduce"}])
            if cpu_throttle > 1:  # a phone-like CPU, also before the first navigation (its load is measured too)
                self.call("Emulation.setCPUThrottlingRate", rate=cpu_throttle)
            blocked = list(self.guard_opts.get("block_urls") or [])
            if blocked:
                self.call("Network.enable")
                self.call("Network.setBlockedURLs", urls=blocked)
            try:
                version = self.jev.cdp("Browser.getVersion") or {}
            except (RuntimeError, TimeoutError, AttributeError):
                version = {}
            if not self.headless:  # attached over --cdp-url: ask the browser itself
                self.headless = "Headless" in version.get("product", "")
            self._own_user_agent(version.get("userAgent", ""))
            self._keep_safe()
            for name in ("microphone", "camera"):
                try:
                    self.jev.cdp("Browser.setPermission", permission={"name": name}, setting="denied")
                except (RuntimeError, TimeoutError):
                    pass  # the in-page stub still refuses capture
            self.minimize()
            # still on about:blank: it hears the site's first request
            self.net = netlog.start(self.page_socket(), self.guard_opts.get("block_urls") or ())
        except Exception:
            self.close()
            raise

    # ---- plumbing ----
    def call(self, method, **params):
        return self.browser.call(method, **params)

    def evaluate(self, expression, timeout_ms=15000):
        r = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True,
                      timeout=timeout_ms)
        if r.get("exceptionDetails"):
            detail = r["exceptionDetails"].get("exception", {}).get("description") or r["exceptionDetails"].get("text")
            raise RuntimeError(f"page script failed: {detail}")
        return r.get("result", {}).get("value")

    def js_holds(self, expression):
        """A condition's settled value is truthy (a throw, a rejection or no answer counts as not holding)."""
        r = self.evaluate(awaited(expression)) or {}
        return "error" not in r and bool(r.get("value"))

    def minimize(self):
        """Keep a visible run's window out of the way. The page still draws: Chrome's flags here
        (--disable-backgrounding-occluded-windows, --disable-renderer-backgrounding) keep a minimized page visible,
        with its animation frames running (measured on macOS, 12 Oct 2026)."""
        if self.headless or self.agent is None:  # a client's window stays up
            return
        try:
            window = self.jev.cdp("Browser.getWindowForTarget", targetId=self.browser.target)["windowId"]
            self.jev.cdp("Browser.setWindowBounds", windowId=window, bounds={"windowState": "minimized"})
        except (RuntimeError, KeyError, TimeoutError):
            pass

    def _keep_safe(self):
        """Re-asserted whenever the guard is armed: another CDP session in the same browser can undo either."""
        # No downloads, ever: a "Download for Mac" click must not pull installers onto this machine. (On Linux the
        # deny did not survive other sessions in the same browser, and Debian Chromium's default is to save.)
        # a multiplayer client's own browser context, if it has one (a stub Session may have no browser at all)
        context = getattr(getattr(self, "browser", None), "context", None)
        try:
            self.jev.cdp("Browser.setDownloadBehavior", behavior="deny",
                         **({"browserContextId": context} if context else {}))
        except (RuntimeError, TimeoutError):
            pass
        if self.headless:  # headless Linux Chromium gives a background tab no frames: every screenshot waited ~31 s
            try:
                self.call("Page.bringToFront")
            except (RuntimeError, TimeoutError):
                pass

    # ---- guard ----
    def arm(self, mode, speech=None):
        self._keep_safe()
        cfg = guard_mod.build_config(
            mode=mode, hosts=self.hosts, speech=speech,
            deny=self.guard_opts.get("deny") or (), allow=self.guard_opts.get("allow") or (),
            allow_requests=self.guard_opts.get("allow_requests") or (),
            redact_emails=self.guard_opts.get("redact_emails", False),
            allow_secret_fields=self.guard_opts.get("allow_secret_fields", False),
            allow_destructive=self.guard_opts.get("allow_destructive", False),
        )
        if self.guard_cfg and cfg["v"] == self.guard_cfg["v"]:
            return
        source = guard_mod.script(cfg)
        if self.script_id:
            self.call("Page.removeScriptToEvaluateOnNewDocument", identifier=self.script_id)
        self.script_id = self.call("Page.addScriptToEvaluateOnNewDocument", source=source)["identifier"]
        self.guard_cfg = cfg
        net = getattr(self, "net", None)
        if net is not None:  # frames on another site run apart: netlog's link guards each before it runs
            with contextlib.suppress(RuntimeError, TimeoutError):
                net.guard_frames(source)
        try:
            self.evaluate(source)  # the current document too (re-arms in place if already guarded)
        except RuntimeError:
            pass

    def guard_state(self):
        return self.evaluate("window.__qajev ? {v: window.__qajev.v, deaf: window.__qajev.deaf, "
                             "mode: window.__qajev.mode, "
                             "pending: window.__qajev.pending ? window.__qajev.pending() : 0} : null")

    GUARD_WAIT = 12.0  # the guard's longest wait: the page's load (5 s after it is parsed) plus React's hydration (5 s)

    def guard_settled(self, timeout=None):
        """Wait until the guard has judged every control on the page: it holds back until React has hydrated them
        (its writes would read as the page's hydration errors). -> the guard's state, pending or not."""
        deadline = time.monotonic() + (self.GUARD_WAIT if timeout is None else timeout)
        while True:
            state = self.guard_state()
            if not state or not state.get("pending") or time.monotonic() >= deadline:
                return state
            time.sleep(0.1)

    def all_judged(self):
        """The guard's state once it has judged every control. Fail closed: GuardMissing while it is still waiting,
        as a control it has not judged may be one nobody must use. Before every Jev action and every hook click."""
        state = self.guard_settled()
        if state and state.get("pending"):
            raise GuardMissing(f"guard still waiting on {state['pending']} control(s) after {self.GUARD_WAIT:.0f} s")
        return state

    def require_guard(self):
        if self.guard_cfg is None:
            raise GuardMissing("guard not armed for this tab")
        state = self.all_judged()
        if not state or state.get("v") != self.guard_cfg["v"] or state.get("deaf") is not True:
            url = self.evaluate("location.href")
            raise GuardMissing(f"guard {'absent' if not state else 'stale or not deaf'} on {url}")
        return state

    def set_color_scheme(self, scheme):
        """light | dark: the page's prefers-color-scheme answers so, as on such a system (foleyapp, 12 Oct: "dark mode
        follows the system setting" had no way to be tested). None: the browser's own. The reduced-motion preference
        stays: Emulation.setEmulatedMedia replaces every feature at once."""
        if scheme == self.color_scheme:
            return
        features = [{"name": "prefers-color-scheme", "value": scheme or ""}]
        if self.motion == "reduce":
            features.append({"name": "prefers-reduced-motion", "value": "reduce"})
        self.call("Emulation.setEmulatedMedia", features=features)
        self.color_scheme = scheme

    def set_device(self, device):
        if device == self.device:
            return
        self.call("Emulation.setDeviceMetricsOverride", width=device["width"], height=device["height"],
                  deviceScaleFactor=device.get("scale", 1), mobile=bool(device.get("mobile")))
        self.call("Emulation.setTouchEmulationEnabled", enabled=bool(device.get("mobile")))
        # A phone also says it is one (sites that serve a mobile page by user agent). Either way it names QAJev.
        if device.get("ua"):
            self.call("Emulation.setUserAgentOverride", userAgent=with_qajev(device["ua"]),
                      platform=device.get("platform", ""))
        elif self.user_agent:
            self._set_user_agent(**self.user_agent)
        else:
            self.call("Emulation.setUserAgentOverride", userAgent="")
        self.device = device

    def _own_user_agent(self, user_agent):
        """Every page says QAJev drove it: the browser's own user agent with "QAJev/<version>" on the end, so a site,
        or a person's page that counts their visits, can tell QAJev from a person (FlockTab1, 14 Oct 2026). Set before
        the first navigation, so the first request says it. The client hints keep the browser's own values. When they
        cannot be read, the mark still goes on, and that tab sends no client hints (as a phone tab does). Headless
        Chrome says it is a normal Chrome (as_chrome)."""
        if not user_agent:
            return
        hints = as_chrome_hints(client_hints(self.jev.cdp, os.environ.get("BU_CDP_URL", "")))
        self.user_agent = {"user_agent": with_qajev(as_chrome(user_agent)), "hints": hints}
        self._set_user_agent(**self.user_agent)

    def _set_user_agent(self, user_agent, hints):
        self.call("Emulation.setUserAgentOverride", userAgent=user_agent,
                  **({"userAgentMetadata": hints} if hints else {}))

    # ---- navigation and page reads ----
    def carry(self):
        """Take what the page's guard recorded (blocked and allowed writes, dialogs) before the tab leaves it."""
        with contextlib.suppress(RuntimeError, TimeoutError):
            got = self.evaluate(DRAIN_JS, timeout_ms=3000) or {}
            for key, items in got.items():
                self.carried.setdefault(key, []).extend(items or [])
        leaving_last_test = self.last_test_page is not None and self._document() == self.last_test_page
        for key, items in self.frames_drained().items():
            if leaving_last_test and key in ("frames_not_observed", "frames_accepted", "frame_console",
                                             "frame_requests"):
                continue  # the last test's page: that test named its frames
            self.carried.setdefault(key, []).extend(items)

    def frames_drained(self):
        """What the guard recorded inside the page's frames, each item with its frame's address, and under
        "frames_not_observed" the address of each frame QAJev could not read or guard. A same-site frame is read in
        its own page world (the top page cannot read another origin); a frame on another site runs as a target of its
        own, which netlog's link attached to and guarded before it ran, and is read through that link.
        A frame on another site that link did not guard (attaching failed, or the frame started before the guard) is
        named instead, unless guard.unguarded_frames accepts its host."""
        if self.net is None:
            return {}
        try:
            tree = self.call("Page.getFrameTree")["frameTree"]
        except (RuntimeError, TimeoutError, KeyError):
            return {}
        urls, todo = {}, list(tree.get("childFrames") or [])
        while todo:
            node = todo.pop()
            urls[node["frame"]["id"]] = node["frame"].get("url", "")
            todo.extend(node.get("childFrames") or [])
        got, unread = {}, []

        def keep(items, where):
            for key, values in (items or {}).items():
                for value in values or []:
                    if isinstance(value, dict):
                        value.setdefault("frame", where[:300])
                    got.setdefault(key, []).append(value)

        if urls:
            try:
                drained, missed = self.net.drain_frames(set(urls), FRAME_DRAIN_JS)
            except (RuntimeError, TimeoutError):
                drained, missed = [], set(urls)
            unread += [(f, urls[f]) for f in missed]
            for frame, items in drained:
                keep(items, urls.get(frame, ""))
        apart = self.frame_targets({tree["frame"]["id"], *urls})
        if apart:
            guarded = self.net.guarded_targets()
            sessions = {guarded[t]: url for t, url in apart if t in guarded}
            ids = {guarded[t]: t for t, _ in apart if t in guarded}
            unread += [(t, url) for t, url in apart if t not in guarded]
            try:
                drained, missed = self.net.drain_sessions(set(sessions), FRAME_DRAIN_JS)
            except (RuntimeError, TimeoutError):
                drained, missed = [], set(sessions)
            unread += [(ids[c], sessions[c]) for c in missed]
            for child, items in drained:
                keep(items, sessions.get(child, ""))
        docs = set(urls.values()) | {url for _, url in apart}
        asked = self.net.frame_requests(docs) if docs else []
        if asked:  # what the frames asked for that failed, or still waits
            got["frame_requests"] = asked
        page = tree["frame"].get("url", "")
        unread = [self.frame_address(frame, url, page) for frame, url in unread]
        accepted = {f["host"]: f["why"] for f in self.guard_opts.get("unguarded_frames") or ()}
        if accepted:  # reviewed exceptions: listed as accepted, and they never make a test harness
            from urllib.parse import urlsplit

            ok = [u for u in unread if (urlsplit(u).hostname or "") in accepted]
            if ok:
                got["frames_accepted"] = [{"url": u[:300], "why": accepted[urlsplit(u).hostname or ""]} for u in ok]
            unread = [u for u in unread if u not in ok]
        if unread:
            got["frames_not_observed"] = [u[:300] for u in unread]
        return got

    def frame_address(self, frame_id, url, page_url):
        """A frame's address. While it has none (its document has not loaded yet, as a video starting), the web address
        its iframe asks for: the iframe's src attribute, read from the DOM (not through the page's script), resolved
        against the page's address. "" when that is not a web address either: such a frame stays unproven, and the
        harness reason calls it a frame with no address yet (Verse check 4, 14 Oct 2026).
        The src is what the page last asked for, not proof of where the frame goes: page script can set it, or send a
        named frame elsewhere while the attribute still names another host (redteam on #152). So an unguarded_frames
        host that matches a src accepts what the page says it loads."""
        if url:
            return url
        from urllib.parse import urljoin, urlsplit

        try:
            node = self.call("DOM.getFrameOwner", frameId=frame_id)["backendNodeId"]
            attributes = self.call("DOM.describeNode", backendNodeId=node)["node"].get("attributes") or []
        except (RuntimeError, TimeoutError, KeyError, TypeError):
            return ""
        src = dict(zip(attributes[::2], attributes[1::2])).get("src", "").strip()
        address = urljoin(page_url, src) if src else ""
        return address if urlsplit(address).scheme in ("http", "https") else ""

    def frame_targets(self, ours):
        """[(target id, address)] of this page's frames that run as targets of their own (a frame on another site,
        under site isolation), found through each iframe target's parent frame, nested ones too."""
        try:
            targets = self.jev.cdp("Target.getTargets").get("targetInfos") or []
        except (RuntimeError, TimeoutError, AttributeError):
            return []
        frames = [t for t in targets if t.get("type") == "iframe"]
        known, found, grew = set(ours), [], True
        while grew:  # a frame inside such a frame names that frame as its parent
            grew = False
            for t in frames:
                if t.get("targetId") not in known and t.get("parentFrameId") in known:
                    known.add(t["targetId"])
                    found.append((t["targetId"], t.get("url") or ""))
                    grew = True
        return found

    def navigate(self, url, timeout=30.0):
        self.carry()
        result = self.call("Page.navigate", url=url)
        if result.get("errorText"):
            return result["errorText"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.evaluate("document.readyState", timeout_ms=3000) == "complete":
                    break
            except RuntimeError:
                pass
            time.sleep(0.05)
        if self.guard_cfg is not None:
            with contextlib.suppress(RuntimeError):  # navigating on: the next page's guard is judged in its turn
                self.guard_settled()
        self.minimize()
        self.loaded_at = time.monotonic()
        return None

    def reload(self, timeout=30.0):
        self.carry()
        self.call("Page.reload")
        time.sleep(0.1)  # the old document may still say "complete" for a moment
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with contextlib.suppress(RuntimeError):
                if self.evaluate("document.readyState", timeout_ms=3000) == "complete":
                    break
            time.sleep(0.05)
        if self.guard_cfg is not None:
            with contextlib.suppress(RuntimeError):
                self.guard_settled()
        self.loaded_at = time.monotonic()

    def begin_test(self):
        """A new test starts on this tab: the last test's page is marked, so leaving it does not name its frames
        under this test (that test's own looks named them). Its blocked writes still carry on."""
        self.carried.pop("frames_not_observed", None)
        self.carried.pop("frames_accepted", None)
        self.last_test_page = self._document()

    def _document(self):
        """The top document's loader id: the same while the tab shows the same page load."""
        try:
            return self.call("Page.getFrameTree")["frameTree"]["frame"].get("loaderId")
        except (RuntimeError, TimeoutError, KeyError):
            return None

    def observe(self):
        if self.agent is None:  # a multiplayer client: no Jev looks at it
            return
        self.agent.state["page"] = self.browser.observe(screenshot=False)

    def probe(self, expect):
        spec = {"text": expect.get("text", []), "absent": expect.get("absent", []), "js": bool(expect.get("js")),
                "ci": bool(expect.get("ignore_case")), "visible": expect.get("visible", [])}
        expression = PROBE_JS.substitute(spec=json.dumps(spec), js=awaited(expect.get("js") or "null"))
        out = self.evaluate(expression) or {}
        if isinstance(out.get("probe"), dict):
            for key, items in self.frames_drained().items():  # the frames' own records, after the page's
                out["probe"][key] = (out["probe"].get(key) or []) + items
            if self.carried:  # the pages the tab has left, first
                carried, self.carried = self.carried, {}
                for key, items in carried.items():
                    out["probe"][key] = items + (out["probe"].get(key) or [])
            if self.net is not None and self.guard_opts.get("block_urls"):
                out["probe"]["block_list"] = self.net.take_block_list()
            if self.net is not None:  # the workers' own console: a page's probe never sees it
                out["probe"]["errors"] = (out["probe"].get("errors") or []) + [
                    {"kind": "worker", **entry} for entry in self.net.take_worker_logs()]
        return out

    def check_host(self, url):
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            return
        if parts.netloc not in self.hosts:
            raise LeftSite(f"now on {parts.netloc}, allowed: {sorted(self.hosts)}")

    def screenshot(self, path, marked=True):
        """The viewport as a JPEG at path. marked: where the read-only guard hid a control, the picture shows a dashed
        box labelled "hidden by guard" (guard.js mark), so a missing button never reads as the page's bug (13 Oct)."""
        mark = "window.__qajev && window.__qajev.mark ? window.__qajev.mark(%s) : 0"
        try:
            if marked:
                with contextlib.suppress(RuntimeError, KeyError, TimeoutError):
                    self.evaluate(mark % "true", timeout_ms=2000)
            data = self.call("Page.captureScreenshot", format="jpeg", quality=70)["data"]
        except (RuntimeError, KeyError, TimeoutError):
            return None
        finally:
            if marked:
                with contextlib.suppress(RuntimeError, KeyError, TimeoutError):
                    self.evaluate(mark % "false", timeout_ms=2000)
        import base64

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data))
        return path

    def page_socket(self):
        """This tab's own debugger WebSocket, for a second client (live.py's frames), or None."""
        from . import live

        if not hasattr(self, "_page_socket"):
            self._page_socket = live.page_socket(os.environ.get("BU_CDP_URL", ""), self.browser.target)
        return self._page_socket

    def why_failed(self, url):
        """Chrome's reason a resource failed to load (or the requests that failed around it), or None."""
        return self.net.explain(url) if self.net else None

    def screen_image(self):
        """The viewport as a JPEG data URL, for Clef (vision.py): what a visitor sees right now."""
        data = self.call("Page.captureScreenshot", format="jpeg", quality=70)["data"]
        return f"data:image/jpeg;base64,{data}"

    def fetch(self, probe):
        method = probe.get("method", "GET").upper()
        expression = f"""(async () => {{
          try {{
            const r = await fetch({json.dumps(probe['url'])},
              {{ method: {json.dumps(method)}, credentials: 'same-origin', redirect: 'manual' }});
            const body = await r.text();
            return {{ status: r.status, body: body.slice(0, 2000) }};
          }} catch (e) {{ return {{ status: null, error: String(e) }}; }}
        }})()"""
        return self.evaluate(expression)

    # ---- Jev ----
    def reset_agent(self, goal):
        """Point the same tab at a new goal; resets Jev's own 60-action / 120-call budgets."""
        self.agent.pending_text = None
        self.agent.state.update(goal=goal, plan=[goal], plan_index=0, history=[], decisions=[], text_calls=[],
                                status="ready", decision=None, started_at=None, elapsed_ms=0)
        self.observe()

    def tick(self):
        """predict -> prove the guard on the live document -> act. Returns False when the page went stale; the
        decision then carries why (`stale`: Jev's reason, what was in the way, what changed)."""
        stale = self.jev.browser.StalePage
        state = self.agent.state
        made = len(state["decisions"])
        try:
            self.agent.command("predict")
            self.require_guard()
            self.agent.command("act", {"fingerprint": state["page"]["fingerprint"]})
            self.settle_scroll()
            return True
        except stale as e:
            decision = state["decisions"][-1] if len(state["decisions"]) > made else None
            before = state["page"]
            why, now, node = self.why_stale(str(e), before, decision)
            state["decision"] = None
            state["status"] = "ready"
            self.observe()
            why["changed"] = page_changes(before, state["page"], now, node)
            self.last_stale = why
            if decision is not None:
                decision["stale"] = why
            return False

    def why_stale(self, reason, page, decision):
        """Before Jev looks again: its reason, and what is wrong with the target it chose. -> (why, keys now, node)."""
        why = {"reason": reason}
        action = next((a for a in page.get("actions") or []
                       if decision and a.get("id") == decision.get("choice")), None)
        node = action.get("node") if action and type(action.get("node")) is int else None
        try:
            now = self.evaluate(STALE_JS.substitute(node=json.dumps(node)), timeout_ms=3000) or {}
        except (RuntimeError, TimeoutError):
            now = {}
        if action is not None and node is not None:
            why["target"] = f"{action.get('kind')} {action.get('label')!r}: {now.get('target') or 'still clickable'}"
        return why, now, node

    # ---- direct steps (hooks): for forms Jev would re-type forever, resets, seeding ----
    def trusted_click(self, x, y):
        with contextlib.suppress(RuntimeError, TimeoutError):  # a point on a frame: Chrome may not route it yet
            wait = self.loaded_at + FRAME_INPUT_S - time.monotonic()
            if wait > 0 and self.evaluate(
                    f"(() => {{ const e = document.elementFromPoint({float(x)}, {float(y)}); "
                    "return !!e && (e.tagName === 'IFRAME' || e.tagName === 'FRAME'); })()", timeout_ms=3000):
                time.sleep(wait)
        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left", clickCount=1)

    SCROLL_STATE = ("(() => [scrollX, scrollY, ...[...document.querySelectorAll('body *')].filter(e => "
                    "e.scrollHeight > e.clientHeight + 4).slice(0, 40).map(e => e.scrollTop)].join(','))()")

    def settle_scroll(self, limit=2.5):
        """Wait until the page stops scrolling. Smooth-scroll libraries (Lenis and the like) turn one wheel turn
        into about a second of gliding; Jev looking mid-glide sees "nothing moved" and then decides on a page that
        is still changing (stale moves). A still page costs one 0.1 s look."""
        deadline = time.monotonic() + limit
        try:
            last = self.evaluate(self.SCROLL_STATE)
            while time.monotonic() < deadline:
                time.sleep(0.1)
                now = self.evaluate(self.SCROLL_STATE)
                if now == last:
                    return
                last = now
        except RuntimeError:
            return  # navigating away: nothing left to settle

    def scroll_further(self):
        """Scroll one screen the way a person does (the wheel, over the middle of the page, so an inner scroll
        panel moves too). -> True when anything on the page actually moved."""
        before = self.evaluate(self.SCROLL_STATE)
        size = self.evaluate("[innerWidth, innerHeight]") or [1280, 900]
        self.call("Input.dispatchMouseEvent", type="mouseWheel", x=size[0] / 2, y=size[1] / 2, deltaX=0,
                  deltaY=round(size[1] * 0.8))
        time.sleep(0.2)
        self.settle_scroll()
        return self.evaluate(self.SCROLL_STATE) != before

    def press(self, value):
        """A key hook: trusted keyDown/keyUp events in the page, as keys.plan() reads `value` (a key name, or
        {press, repeat, interval_ms, hold_ms})."""
        try:
            p = keys.plan(value)
        except ValueError as e:
            raise HookFailed(str(e)) from None
        for _ in range(p["repeat"]):
            for name in p["keys"]:
                spec = keys.spec(name)
                self.call("Input.dispatchKeyEvent", type="keyDown", **spec)
                if p["hold_ms"]:
                    time.sleep(p["hold_ms"] / 1000)
                self.call("Input.dispatchKeyEvent", type="keyUp", **{k: v for k, v in spec.items() if k != "text"})
                if p["interval_ms"]:
                    time.sleep(p["interval_ms"] / 1000)

    def use_pad(self):
        """Put the virtual pad (pad.SHIM_JS) in every document from now on, before the page's own code, and in the
        current one. Only a scenario with a pad hook gets it: other pages keep the real navigator.getGamepads."""
        if getattr(self, "pad_script_id", None):
            return
        self.pad_script_id = self.call("Page.addScriptToEvaluateOnNewDocument", source=pad_mod.SHIM_JS)["identifier"]
        with contextlib.suppress(RuntimeError):
            self.evaluate(pad_mod.SHIM_JS)

    def pad(self, value):
        """A pad hook: each frame of pad.plan(value) set on the virtual pad, held for its time."""
        try:
            frames = pad_mod.plan(value)
        except ValueError as e:
            raise HookFailed(str(e)) from None
        self.use_pad()
        for frame in frames:
            try:
                self.evaluate(pad_mod.set_js(frame))
            except RuntimeError as e:
                raise HookFailed(f"pad: {e}") from None
            if frame["ms"]:
                time.sleep(frame["ms"] / 1000)

    def react(self, value):
        """A react hook: run a key policy in the page every `every_ms` (keys.react_plan) and send the key actions it
        returns as trusted key events, until `until` holds or `for_s` runs out. A policy can also ask for a frame
        ({shot: label}: the cue on screen at that moment). A key held 5 s is released; every key still down is released
        at the end. What it saw goes in self.react_log (frames, releases)."""
        try:
            p = keys.react_plan(value)
        except ValueError as e:
            raise HookFailed(str(e)) from None
        tick = REACT_JS.substitute(until=p["until"] or "false", js=p["js"])
        log = self.react_log = getattr(self, "react_log", [])
        held, start, shots, capped = {}, time.monotonic(), 0, False
        deadline = start + p["for_s"]
        try:
            while True:
                began = time.monotonic()
                if began > deadline:
                    if p["until"]:
                        raise HookFailed(f"react: {p['until']!r} did not hold within {p['for_s']:g} s")
                    return
                # a policy awaiting a promise that never settles must not outlast for_s
                left_ms = int((deadline - began) * 1000)
                out = self.evaluate(tick, timeout_ms=max(1, min(left_ms, max(4 * p["every_ms"], 1000)))) or {}
                if out.get("error"):
                    raise HookFailed(f"react policy: {out['error']}"[:300])
                if out.get("done"):
                    return
                items = out.get("actions")
                items = [x for x in (items if isinstance(items, list) else [items]) if x is not None]
                asked = [x for x in items if isinstance(x, dict) and "shot" in x]
                try:
                    todo = keys.actions([x for x in items if not (isinstance(x, dict) and "shot" in x)])
                except ValueError as e:
                    raise HookFailed(f"react: {e}") from None
                # Keys first, then the frames this tick asked for (SideGame1, 5 Oct: a frame taken first delayed the
                # key-down a grip flash asked for, inside a 0.6 s window). A held key's down and up are logged with
                # when they were sent; a plain press is not (a mashing policy sends seven a second).
                for what, name in todo:
                    spec = keys.spec(name)
                    up = {k: v for k, v in spec.items() if k != "text"}
                    if what in {"press", "down"} and name not in held:
                        self.call("Input.dispatchKeyEvent", type="keyDown", **spec)
                        held[name] = time.monotonic()
                        if what == "down":
                            log.append({"down": name, "at_s": round(held[name] - start, 3)})
                    if what in {"press", "up"} and name in held:
                        self.call("Input.dispatchKeyEvent", type="keyUp", **up)
                        held.pop(name)
                        if what == "up":
                            log.append({"up": name, "at_s": round(time.monotonic() - start, 3)})
                for i, x in enumerate(asked):
                    if i >= keys.MAX_SHOTS_TICK or shots >= keys.MAX_SHOTS_HOOK:
                        if not capped:
                            capped = True
                            log.append({"note": f"frame cap reached ({keys.MAX_SHOTS_TICK} a tick, "
                                                f"{keys.MAX_SHOTS_HOOK} a hook): later frames not kept",
                                        "at_s": round(began - start, 2)})
                        continue
                    shots += 1
                    log.append(self._react_shot(str(x["shot"]), time.monotonic() - start, start))
                for name, since in list(held.items()):  # a hold is bounded, as in a key hook
                    if time.monotonic() - since >= keys.MAX_HOLD_MS / 1000:
                        self.call("Input.dispatchKeyEvent", type="keyUp",
                                  **{k: v for k, v in keys.spec(name).items() if k != "text"})
                        held.pop(name)
                        log.append({"released": name, "at_s": round(time.monotonic() - start, 2),
                                    "why": f"held {keys.MAX_HOLD_MS // 1000} s"})
                time.sleep(max(0.0, p["every_ms"] / 1000 - (time.monotonic() - began)))
        finally:
            for name in held:  # each on its own: one failed keyUp must not leave the others down
                with contextlib.suppress(Exception):
                    self.call("Input.dispatchKeyEvent", type="keyUp",
                              **{k: v for k, v in keys.spec(name).items() if k != "text"})
            held.clear()

    def _react_shot(self, label, at_s, start):
        """A frame a react policy asked for, saved next to the scenario's own screenshot when the run keeps shots. Each
        gets its own file (a number before the label): a policy labelling every round's cue the same keeps them all.
        `at_s` is when the capture began and `took_s` how long it took, both from the hook's `start`."""
        label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-")[:40] or "frame"
        self.react_frames = getattr(self, "react_frames", 0) + 1
        where = getattr(self, "shot_dir", None)
        name = f"{getattr(self, 'shot_prefix', 'react')}-{self.react_frames:03d}-{label}.jpg"
        path = self.screenshot(where / name, marked=False) if where else None  # timed: no marking pass
        return {"shot": label, "at_s": round(at_s, 3), "took_s": round(time.monotonic() - start - at_s, 3),
                "path": str(path) if path else None}

    def find(self, selector):
        if self.guard_cfg is not None:
            self.require_guard()  # present, current, deaf and done judging: else no hook clicks or types here
        found = self.evaluate(FIND_JS.substitute(selector=json.dumps(selector)))
        if not found or found.get("error"):
            raise HookFailed((found or {}).get("error") or f"cannot locate {selector!r}")
        return found

    def click_at(self, spec, timeout=2.0):
        """A real click at a point: CSS pixels from the top-left corner of spec's selector, or of the viewport. Like
        `click`, it waits for the point to hold still two looks in a row, and fails when something else covers it."""
        selector = spec.get("selector")
        point = spec
        if "js" in spec:  # the point is known only at run time (a camera that moves): the expression gives it
            got = self.evaluate(awaited(spec["js"]), timeout_ms=JS_WAIT_MS) or {}
            point = got.get("value") if "error" not in got else None
            if not isinstance(point, dict) or not all(
                    isinstance(point.get(a), (int, float)) and not isinstance(point.get(a), bool) and point[a] >= 0
                    for a in ("x", "y")):
                said = got.get("error") or json.dumps(point)[:120]
                raise HookFailed(f"click_at {selector or 'the viewport'}: its js must return {{x, y}}, two numbers "
                                 f"of 0 or more; it gave {said}")
        expression = AT_JS.substitute(selector=json.dumps(selector), x=json.dumps(point["x"]), y=json.dumps(point["y"]))
        what = f"click_at {selector or 'the viewport'} {point['x']}, {point['y']}"
        deadline, last = time.monotonic() + timeout, None
        while True:
            at = self.evaluate(expression, timeout_ms=3000) or {}
            if at.get("error"):
                raise HookFailed(f"{what}: {at['error']}")
            if not at.get("covered") and last is not None and (at["x"], at["y"]) == (last["x"], last["y"]):
                break
            last = None if at.get("covered") else at
            if time.monotonic() > deadline:
                if at.get("covered"):
                    raise HookFailed(f"{what}: the point is covered by {at.get('over')}")
                break
            time.sleep(0.1)
        self.trusted_click(at["x"], at["y"])

    def settled(self, selector, timeout=2.0):
        """A click target scrolled into view, on top, and at the same spot two looks in a row: a fading splash, a
        panel still scrolling or growing as its images arrive moves or covers it for a moment (SideGame1, 6 Oct).
        -> find()'s result. A target still covered after `timeout` fails naming what covers it."""
        deadline = time.monotonic() + timeout
        last = None
        while True:
            found = self.find(selector)
            if not found["covered"] and last is not None and (found["x"], found["y"]) == (last["x"], last["y"]):
                return found
            last = None if found["covered"] else found
            if time.monotonic() > deadline:
                if found["covered"]:
                    raise HookFailed(f"{selector!r} is covered by {found.get('over') or 'another element'}")
                return found
            time.sleep(0.05)

    def type_into(self, found, text):
        """Click a field located by find() and replace its value with `text`, as typed input."""
        self.trusted_click(found["x"], found["y"])
        mod = 4 if os.uname().sysname == "Darwin" else 2
        self.call("Input.dispatchKeyEvent", type="keyDown", key="a", code="KeyA", modifiers=mod, commands=["selectAll"])
        self.call("Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA", modifiers=mod)
        self.call("Input.insertText", text=text)

    def fill(self, selector, found, text):
        """Type `text` into a field located by find(), or set a picker field's value; then fail if the field did not
        take it, instead of leaving the next step to time out (dash-wholesale-links, 12 Oct)."""
        sel = json.dumps(selector)
        before = self.evaluate(VALUE_JS.substitute(selector=sel))
        held = self.evaluate(SET_PICKER_JS.substitute(selector=sel, types=json.dumps(PICKER_TYPES),
                                                      text=json.dumps(text)))
        if held is not None:
            if held != text:
                raise HookFailed(f"fill {selector!r}: the field took {held!r}, not {text!r}. A date, time or month "
                                 "field takes the form Chrome stores (2027-06-30, 14:30, 2027-06)")
            return
        self.type_into(found, text)
        after = self.evaluate(VALUE_JS.substitute(selector=sel))
        if after is not None and after == before and before != text:  # a page may reformat; unchanged is a no-op
            what = "what it held before" if found["secret"] else repr(after)
            raise HookFailed(f"fill {selector!r}: the field still holds {what}; the typing did not reach it")

    def run_hook(self, hook, current_url):
        kind, value = next(iter(hook.items()))
        if kind == "js":
            self.evaluate(value)
        elif kind == "click":
            found = self.settled(value)
            self.trusted_click(found["x"], found["y"])
        elif kind == "click_at":
            self.click_at(value)
        elif kind == "fill":
            for selector, text in value.items():
                found = self.find(selector)
                if found["secret"] and not is_loopback(current_url):
                    raise HookFailed(f"refused: {selector!r} is a secret field on a non-loopback host")
                self.fill(selector, found, os.path.expandvars(str(text)))
        elif kind == "navigate":
            from urllib.parse import urljoin

            target = urljoin(current_url, value)
            self.check_host(target)
            error = self.navigate(target)
            if error:
                raise HookFailed(f"navigate {target}: {error}")
        elif kind == "wait_for":
            spec = value if isinstance(value, dict) else {"js": value}
            deadline = time.monotonic() + float(spec.get("timeout", 15))
            while not self.js_holds(spec["js"]):
                if time.monotonic() > deadline:
                    raise HookFailed(f"wait_for {spec['js']!r} timed out")
                time.sleep(0.25)
        elif kind == "reload":  # the same page again, keeping the tab's cookies and storage (a player rejoining)
            self.reload()
        elif kind in ("key", "react", "pad"):  # real time: a LIVE frame must not steal its time (live.REALTIME)
            live.REALTIME.set()
            try:
                {"key": self.press, "react": self.react, "pad": self.pad}[kind](value)
            finally:
                live.REALTIME.clear()
        elif kind == "sleep":
            time.sleep(min(float(value), 30))
        elif kind == "command":
            result = self.command(value, current_url)
            if result["exit"] != 0:
                raise HookFailed(f"command exited {result['exit']}: {result['stderr'][-300:]}")
        self.observe()

    def command(self, run, current_url):
        if not self.allow_commands:
            raise HookFailed("command hooks need --allow-commands (they run shell on this machine)")
        env = {k: v for k, v in os.environ.items() if v != providers.PLACEHOLDER}
        env["QAJEV_URL"] = current_url
        try:
            p = subprocess.run(run, shell=True, cwd=self.cwd, env=env, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return {"exit": None, "stdout": "", "stderr": "timed out after 120 s"}
        return {"exit": p.returncode, "stdout": p.stdout[-4000:], "stderr": p.stderr[-4000:]}

    # ---- teardown ----
    def close(self):
        target = getattr(self.browser, "target", None) if hasattr(self, "browser") else None
        net, self.net = getattr(self, "net", None), None
        if net:
            net.stop()
        if getattr(self, "agent", "jev") is None:  # a multiplayer client: its tab and its browser context
            self.browser.close()
            return
        try:
            self.agent.close()
        except Exception:
            if target:
                chrome.close_target(os.environ["BU_CDP_URL"], target)


def stop_daemon():
    """Each run starts its own daemon (unique BU_NAME); leave nothing behind."""
    if _jev is None:
        return
    name = os.environ["BU_NAME"]
    try:
        pid = _jev.admin.ipc.identify(name, timeout=1.0)
    except Exception:
        pid = None
    done = threading.Event()

    def reap():  # restart_daemon waits up to 15 s for the pid to vanish; our exited child would linger as a zombie
        while not done.is_set() and not chrome.reaped(pid):
            time.sleep(0.05)

    if pid:
        threading.Thread(target=reap, daemon=True).start()
    try:
        _jev.admin.restart_daemon(name)
    except Exception:
        pass
    finally:
        done.set()
