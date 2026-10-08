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

from . import chrome, keys, live, netlog, providers
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
        _jev.model.post_json = ledger.wrap(providers.route(_jev.raw_post_json, _jev.providers))
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
    jev_model.post_json = ledger.wrap(providers.route(raw_post_json, resolved))
    _jev = SimpleNamespace(admin=admin, agent=jev_agent, browser=jev_browser, model=jev_model,
                           raw_post_json=raw_post_json, cdp=patient_cdp, providers=resolved)
    return _jev


JS_WAIT_MS = 5000


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
          || !fold(norm(e.innerText)).includes(want)) { why = 'in the page but hidden'; continue; }
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
  return { probe, url: location.href, title: document.title, status, hidden: document.hidden,
    text: spec.text.map(t => has(t)), absent: spec.absent.map(t => has(t)), visible: seen.map(s => s.ok),
    visible_why: seen.map(s => s.why),
    near: spec.text.map(t => has(t) ? null : nearest(t)), js, says: body.slice(0, 400) };
})()""")

FIND_JS = Template("""(() => {
  const el = document.querySelector($selector);
  if (!el) return { error: 'no element matches ' + $selector };
  if (el.dataset.qajevGuard) return { error: 'refused: the guard hid this control (' + el.dataset.qajevGuard + ')' };
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

# Why a decision went stale, read before Jev looks again: Jev's freshness keys now (its pageKey and the target's
# guard, as Browser.fresh compares them) and the first of Browser.act's target checks that fails.
# While Jev looks and chooses on a page that keeps moving, its animation frames wait, as in a background tab: the frame
# counter and the 3D scene stop, so the page Jev decided on is the page it acts on. Timers, network messages and input
# still run, so a multiplayer page keeps its feed (8 Oct: 29 feed messages arrived during a 3 s hold). release() runs
# the frames that waited. Only window.requestAnimationFrame and the page's CSS animations are held.
HOLD_JS = """(() => {
  if (window.__qajevHold) return;
  const raf = window.requestAnimationFrame.bind(window), caf = window.cancelAnimationFrame.bind(window);
  let on = false, since = 0, waiting = new Map(), next = -1;
  window.requestAnimationFrame = (cb) => {
    if (!on) return raf(cb);
    const id = next--; waiting.set(id, cb); return id;
  };
  window.cancelAnimationFrame = (id) => { if (waiting.has(id)) waiting.delete(id); else caf(id); };
  // held: how many holds, heldMs: their time. A page's own frame-rate check divides by the time it was not held.
  window.__qajevHold = {
    held: 0,
    heldMs: 0,
    hold() {
      if (!on) { on = true; since = performance.now(); this.held++; }
      document.getAnimations().forEach((a) => { try { a.pause(); } catch (e) {} });
      // A frame the page asked for before the hold still draws once: answer after it, so the page is still when read.
      return new Promise((done) => { raf(() => done(true)); setTimeout(() => done(true), 100); });
    },
    release() {
      if (on) this.heldMs += performance.now() - since;
      on = false;
      document.getAnimations().forEach((a) => { try { if (a.playState === 'paused') a.play(); } catch (e) {} });
      const cbs = [...waiting.values()]; waiting = new Map();
      cbs.forEach((cb) => raf(cb));
    },
  };
})()"""

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
        self.jev = load(ledger)
        self.headless = headless
        self.hosts = set(hosts)
        self.guard_opts = guard_opts or {}
        self.allow_commands = allow_commands
        self.cwd = cwd
        self.script_id = None
        self.guard_cfg = None
        self.device = None
        self.assists = []
        self.last_stale = None
        self.holding = False  # the page keeps moving: hold its frames while Jev decides (HOLD_JS)
        self.held = 0  # decisions made on a held page
        self.held_s = 0.0  # and how long the page was held for them
        self.hold_loaded = False  # HOLD_JS is added to each new document of this tab
        self.net = None
        self.carried = {}  # what a page's guard recorded before the tab left it, for the next probe
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
            if not self.headless:  # attached over --cdp-url: ask the browser itself
                try:
                    product = self.jev.cdp("Browser.getVersion").get("product", "")
                    self.headless = "Headless" in product
                except (RuntimeError, TimeoutError, AttributeError):
                    pass
            self._keep_safe()
            for name in ("microphone", "camera"):
                try:
                    self.jev.cdp("Browser.setPermission", permission={"name": name}, setting="denied")
                except (RuntimeError, TimeoutError):
                    pass  # the in-page stub still refuses capture
            # Never minimised: a minimised window draws no frames, so a 3D page stalls (8 Oct). On a Mac the window
            # stays off the person's screen because QAJev starts Chrome hidden (chrome._open_behind).
            self.net = netlog.start(self.page_socket())  # still on about:blank: it hears the site's first request
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

    def set_device(self, device):
        if device == self.device:
            return
        self.call("Emulation.setDeviceMetricsOverride", width=device["width"], height=device["height"],
                  deviceScaleFactor=device.get("scale", 1), mobile=bool(device.get("mobile")))
        self.call("Emulation.setTouchEmulationEnabled", enabled=bool(device.get("mobile")))
        # A phone also says it is one (sites that serve a mobile page by user agent); "" restores the browser's own.
        if device.get("ua"):
            self.call("Emulation.setUserAgentOverride", userAgent=device["ua"], platform=device.get("platform", ""))
        else:
            self.call("Emulation.setUserAgentOverride", userAgent="")
        self.device = device

    # ---- navigation and page reads ----
    def carry(self):
        """Take what the page's guard recorded (blocked and allowed writes, dialogs) before the tab leaves it."""
        with contextlib.suppress(RuntimeError, TimeoutError):
            got = self.evaluate("window.__qajev && window.__qajev.drain ? window.__qajev.drain() : null",
                                timeout_ms=3000) or {}
            for key, items in got.items():
                self.carried.setdefault(key, []).extend(items or [])

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

    def observe(self):
        if self.agent is None:  # a multiplayer client: no Jev looks at it
            return
        self.agent.state["page"] = self.browser.observe(screenshot=False)

    def probe(self, expect):
        spec = {"text": expect.get("text", []), "absent": expect.get("absent", []), "js": bool(expect.get("js")),
                "ci": bool(expect.get("ignore_case")), "visible": expect.get("visible", [])}
        expression = PROBE_JS.substitute(spec=json.dumps(spec), js=awaited(expect.get("js") or "null"))
        out = self.evaluate(expression) or {}
        if self.carried and isinstance(out.get("probe"), dict):  # the pages the tab has left, first
            carried, self.carried = self.carried, {}
            for key, items in carried.items():
                out["probe"][key] = items + (out["probe"].get(key) or [])
        return out

    def check_host(self, url):
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            return
        if parts.netloc not in self.hosts:
            raise LeftSite(f"now on {parts.netloc}, allowed: {sorted(self.hosts)}")

    def screenshot(self, path):
        try:
            data = self.call("Page.captureScreenshot", format="jpeg", quality=70)["data"]
        except (RuntimeError, KeyError, TimeoutError):
            return None
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
        self.holding, self.held, self.held_s = False, 0, 0.0  # held again only after this goal's own stale move
        self.observe()

    def tick(self):
        """predict -> prove the guard on the live document -> act. Returns False when the page went stale; the
        decision then carries why (`stale`: Jev's reason, what was in the way, what changed)."""
        stale = self.jev.browser.StalePage
        state = self.agent.state
        made = len(state["decisions"])
        start = time.monotonic() if self.holding else 0.0
        held = self.holding and self._hold("hold")
        try:
            try:
                if held:  # read the held page: frames drawn since the last read must not make the choice stale
                    self.observe()
                self.agent.command("predict")
                self.require_guard()
                self.agent.command("act", {"fingerprint": state["page"]["fingerprint"]})
                self.held += bool(held)
            finally:
                if held:  # the action's own effects draw now, before the page is read again; on any error too
                    self._hold("release")
                    self.held_s += time.monotonic() - start
            self.settle_scroll()
            return True
        except stale as e:
            self.hold_moving_page()  # it moved under Jev: from now on, its frames wait while Jev decides
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

    def hold_moving_page(self):
        """Hold this page's frames while Jev decides, from its next decision on (HOLD_JS), in each document it loads."""
        if self.holding:
            return
        self.holding = True
        if not self.hold_loaded:  # once per tab: in a later goal's documents it waits, unused, until a hold
            self.hold_loaded = True
            with contextlib.suppress(RuntimeError, TimeoutError):
                self.call("Page.addScriptToEvaluateOnNewDocument", source=HOLD_JS)
        with contextlib.suppress(RuntimeError, TimeoutError):
            self.evaluate(HOLD_JS)

    def _hold(self, op):
        """Hold or release the page's frames. -> True when the page has the hold (a page that blocks it moves on)."""
        try:
            return bool(self.evaluate(f"(async (h) => !!h && (await h.{op}(), true))(window.__qajevHold)",
                                      timeout_ms=3000))
        except (RuntimeError, TimeoutError):
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
        path = self.screenshot(where / name) if where else None
        return {"shot": label, "at_s": round(at_s, 3), "took_s": round(time.monotonic() - start - at_s, 3),
                "path": str(path) if path else None}

    def find(self, selector):
        if self.guard_cfg is not None:
            self.require_guard()  # present, current, deaf and done judging: else no hook clicks or types here
        found = self.evaluate(FIND_JS.substitute(selector=json.dumps(selector)))
        if not found or found.get("error"):
            raise HookFailed((found or {}).get("error") or f"cannot locate {selector!r}")
        return found

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

    def run_hook(self, hook, current_url):
        kind, value = next(iter(hook.items()))
        if kind == "js":
            self.evaluate(value)
        elif kind == "click":
            found = self.settled(value)
            self.trusted_click(found["x"], found["y"])
        elif kind == "fill":
            for selector, text in value.items():
                found = self.find(selector)
                if found["secret"] and not is_loopback(current_url):
                    raise HookFailed(f"refused: {selector!r} is a secret field on a non-loopback host")
                self.type_into(found, os.path.expandvars(str(text)))
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
