"""One guarded browser tab driven by Jev.

Order matters and every step here was learned from an incident or a measured miss:
  1. BU_CDP_URL / BU_NAME are set before jev_ultrafast is imported (browser_harness reads them at import).
  2. The tab opens on about:blank, so no site byte loads before the guard is armed.
  3. Page.enable BEFORE Page.addScriptToEvaluateOnNewDocument, or later documents run unguarded.
  4. The guard is re-proven right before every action Jev executes (fail closed).
"""

import contextlib
import json
import os
import subprocess
import threading
import time
import uuid
from string import Template
from types import SimpleNamespace

from . import chrome, netlog, providers
from . import guard as guard_mod
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
            return raw_cdp(method, session_id=session_id, _response_timeout=timeout, **params)
        except TimeoutError:
            return raw_cdp(method, session_id=session_id, _response_timeout=timeout, **params)

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
  // On screen: the smallest elements holding the text, visible and inside the viewport right now.
  const onScreen = (t) => {
    const want = fold(norm(t));
    const holders = [...document.querySelectorAll('body *')].filter(e => fold(norm(e.textContent)).includes(want));
    const smallest = holders.filter(e => ![...e.children].some(c => fold(norm(c.textContent)).includes(want)));
    return smallest.some(e => {
      if (!(e.checkVisibility ? e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}) : true)) return false;
      if (!fold(norm(e.innerText)).includes(want)) return false;
      const r = e.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
    });
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
    js = 'error' in r ? 'error: ' + r.error : (r.value === true || r.value === false ? r.value : !!r.value);
  }
  let status = null;
  try { status = performance.getEntriesByType('navigation')[0].responseStatus || null; } catch (e) {}
  return { probe, url: location.href, title: document.title, status,
    text: spec.text.map(t => has(t)), absent: spec.absent.map(t => has(t)), visible: spec.visible.map(onScreen),
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
  return { x, y, secret, covered: !(hit && (el === hit || el.contains(hit))), tag: el.tagName };
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


KEYS = {
    "Escape": {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
    "Enter": {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"},
    "Tab": {"key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
}


class Session:
    def __init__(self, ledger, *, headless=False, hosts=(), guard_opts=None, allow_commands=False, cwd=None,
                 motion="reduce"):
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
        self.net = None
        Agent = self.jev.agent.Agent
        self.agent = Agent("about:blank", "Wait for instructions.")
        self.browser = self.agent.browser
        try:
            self.call("Page.enable")  # must precede addScriptToEvaluateOnNewDocument
            if motion == "reduce":  # before the first navigation, so the first paint already honours it
                self.call("Emulation.setEmulatedMedia",
                          features=[{"name": "prefers-reduced-motion", "value": "reduce"}])
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
            self.minimize()
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

    def minimize(self):
        if self.headless:
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
        try:
            self.jev.cdp("Browser.setDownloadBehavior", behavior="deny")
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
    def navigate(self, url, timeout=30.0):
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
        return None

    def observe(self):
        self.agent.state["page"] = self.browser.observe(screenshot=False)

    def probe(self, expect):
        spec = {"text": expect.get("text", []), "absent": expect.get("absent", []), "js": bool(expect.get("js")),
                "ci": bool(expect.get("ignore_case")), "visible": expect.get("visible", [])}
        expression = PROBE_JS.substitute(spec=json.dumps(spec), js=awaited(expect.get("js") or "null"))
        return self.evaluate(expression) or {}

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

    def press(self, key):
        spec = KEYS.get(key)
        if not spec:
            raise HookFailed(f"unknown key {key!r}; known: {sorted(KEYS)}")
        self.call("Input.dispatchKeyEvent", type="keyDown", **spec)
        self.call("Input.dispatchKeyEvent", type="keyUp", **{k: v for k, v in spec.items() if k != "text"})

    def find(self, selector):
        if self.guard_cfg is not None:
            self.all_judged()  # so a control the guard hides is refused, never clicked before it is judged
        found = self.evaluate(FIND_JS.substitute(selector=json.dumps(selector)))
        if not found or found.get("error"):
            raise HookFailed((found or {}).get("error") or f"cannot locate {selector!r}")
        return found

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
            found = self.find(value)
            if found["covered"]:
                raise HookFailed(f"{value!r} is covered by another element")
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
        elif kind == "key":
            self.press(value)
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
