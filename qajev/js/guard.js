// QAJev in-page guard. Installed with Page.addScriptToEvaluateOnNewDocument (after Page.enable)
// so it runs before any page script, on every document the tab loads, in every frame.
// `__QAJEV_CONFIG__` is replaced with a JSON object by qajev/guard.py.
(() => {
  const incoming = __QAJEV_CONFIG__;
  if (window.__qajev) {
    // Same document, new scenario settings: re-arm in place (the guard itself is not replaceable).
    if (window.__qajev.v !== incoming.v) window.__qajev.reconfigure(incoming);
    return;
  }

  const state = { v: '', mode: '', deaf: false, hidden: 0, errors: [], blocked: [], lcp: 0, cls: 0, busySince: null,
                  hydration: 0, hydrationText: [] };
  const MAX = 200;
  const push = (list, item) => { if (list.length < MAX) list.push(item); };
  const re = (list) => (list && list.length ? new RegExp(list.join('|'), 'i') : null);
  const loopback = /^(localhost|127\.\d+\.\d+\.\d+|\[::1\]|[^.]+\.localhost)$/i.test(location.hostname);
  let cfg, deny, mutating, allow, allowRequest, hosts, readOnly, heard;
  const configure = (next) => {
    cfg = next; state.v = next.v; state.mode = next.mode;
    deny = re(next.deny); mutating = re(next.mutating); allow = re(next.allow); allowRequest = re(next.allow_requests);
    hosts = new Set(next.hosts); readOnly = next.mode === 'readonly'; heard = next.speech;
  };
  configure(incoming);

  // ---- Deaf: the page can never reach a real microphone or camera. ----
  class DeafRecognition extends EventTarget {
    constructor() {
      super();
      this.continuous = false; this.interimResults = false; this.lang = 'en-US'; this.maxAlternatives = 1;
      this.onstart = this.onresult = this.onerror = this.onend = this.onaudiostart = this.onspeechend = null;
      this.live = false;
    }
    _fire(type, extra) {
      const event = Object.assign(new Event(type), extra || {});
      this.dispatchEvent(event);
      const handler = this['on' + type];
      if (typeof handler === 'function') handler.call(this, event);
    }
    start() {
      this.live = true;
      setTimeout(() => {
        this._fire('start');
        if (heard) {
          const alt = { transcript: heard, confidence: 0.99 };
          const result = Object.assign([alt], { isFinal: true, item: (i) => [alt][i] });
          const results = Object.assign([result], { item: (i) => [result][i] });
          this._fire('result', { resultIndex: 0, results });
        } else {
          this._fire('error', { error: 'no-speech', message: 'QAJev: no real microphone' });
        }
        this._fire('end'); this.live = false;
      }, 250);
    }
    stop() { if (this.live) { this.live = false; setTimeout(() => this._fire('end'), 50); } }
    abort() { this.stop(); }
  }
  const refuse = () => Promise.reject(new DOMException('QAJev: capture devices are disabled', 'NotAllowedError'));
  try {
    Object.defineProperty(window, 'SpeechRecognition', { value: DeafRecognition, configurable: false, writable: false });
    Object.defineProperty(window, 'webkitSpeechRecognition', { value: DeafRecognition, configurable: false, writable: false });
    if (navigator.mediaDevices) {
      Object.defineProperty(navigator.mediaDevices, 'getUserMedia', { value: refuse, configurable: false, writable: false });
      Object.defineProperty(navigator.mediaDevices, 'getDisplayMedia', { value: refuse, configurable: false, writable: false });
    }
    for (const legacy of ['getUserMedia', 'webkitGetUserMedia']) {
      if (legacy in navigator) {
        Object.defineProperty(navigator, legacy, {
          value: (_c, _ok, fail) => fail && fail(new DOMException('QAJev', 'NotAllowedError')), configurable: false,
        });
      }
    }
    state.deaf = window.webkitSpeechRecognition === DeafRecognition && window.SpeechRecognition === DeafRecognition;
  } catch (e) {
    state.deaf = false;
  }

  // A hydration warning (React's) that names only attributes the guard itself sets: QAJev's doing, a harness note.
  // React 18 lists them ("Extra attributes from the server: disabled,data-qajev-guard", "Prop `target` did not
  // match"); React 19 prints a diff whose changed lines are `+ name=...` / `- name=...`.
  const GUARD_ATTRS = new Set(['data-qajev-guard', 'data-qajev-display', 'disabled', 'inert', 'aria-hidden', 'style',
    'target']);
  function guardHydration(args) {
    // printf-style, as the console prints it: React 18 passes the attribute names as separate arguments
    const parts = args.map((a) => (a && a.message) || String(a));
    let i = 1;
    const text = [typeof args[0] === 'string' ? parts[0].replace(/%[sdifoOc]/g, () => (i < parts.length ? parts[i++] : ''))
      : parts[0], ...parts.slice(i)].join(' ');
    if (!/hydrat|did not match|Extra attributes from the server/i.test(text)) return false;
    const names = [];
    const extra = /Extra attributes from the server:[^\n]*?\s([\w-]+(?:,\s*[\w-]+)*)\s*(?:\n|$)/i.exec(text);
    if (extra) names.push(...extra[1].split(/,\s*/));
    const prop = /Prop `([\w-]+)` did not match/i.exec(text);
    if (prop) names.push(prop[1]);
    for (const m of text.matchAll(/^\s*[+-]\s+([\w-]+)=/gm)) names.push(m[1]);
    // the guard's marker must be among them (a lone `disabled` may be the page's own), or only its `target` rewrite
    return names.length > 0 && (names.some((n) => /^data-qajev-/i.test(n)) || names.every((n) => n === 'target')) &&
      names.every((n) => GUARD_ATTRS.has(n.toLowerCase()) || /^data-qajev-/i.test(n));
  }

  // ---- Errors, failed requests, vitals: product signals collected for the report. ----
  const where = (el) => el && (el.src || el.href || el.currentSrc || el.tagName);
  addEventListener('error', (e) => {
    if (e.target && e.target !== window) push(state.errors, { kind: 'resource', detail: String(where(e.target)) });
    // the stack (Chrome's starts with the message) points at the line; a cross-origin script gives only a message
    else push(state.errors, { kind: 'exception', detail: String(e.error && e.error.stack || e.message || e.error) });
  }, true);
  // CSP violations never reach console.error, and the requests they stop are often third-party (tags, pixels)
  addEventListener('securitypolicyviolation', (e) => {
    push(state.errors, { kind: 'csp', disposition: e.disposition, directive: e.effectiveDirective,
                         detail: String(e.blockedURI || e.sourceFile || '') });
  }, true);
  addEventListener('unhandledrejection', (e) => {
    push(state.errors, { kind: 'rejection', detail: String(e.reason && (e.reason.stack || e.reason.message) || e.reason) });
  });
  const consoleError = console.error;
  console.error = function (...args) {
    const text = args.map((a) => (a && a.message) || String(a)).join(' ');
    // re-classed, never hidden: its text goes to the report's harness note
    if (guardHydration(args)) { state.hydration++; push(state.hydrationText, text.slice(0, 500)); }
    else push(state.errors, { kind: 'console', detail: text.slice(0, 500) });
    return consoleError.apply(this, args);
  };
  try {
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        if (entry.responseStatus >= 400) push(state.errors, { kind: 'http', status: entry.responseStatus, detail: entry.name });
      }
    }).observe({ type: 'resource', buffered: true });
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) state.lcp = Math.round(entry.startTime);
    }).observe({ type: 'largest-contentful-paint', buffered: true });
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) if (!entry.hadRecentInput) state.cls += entry.value;
    }).observe({ type: 'layout-shift', buffered: true });
  } catch (e) { /* older engines: vitals stay 0 */ }

  // ---- Read-only network: no writes leave the page. Recorded, never silent. ----
  const writeBlocked = (method, url) => {
    if (!readOnly) return false;
    const m = String(method || 'GET').toUpperCase();
    if (m === 'GET' || m === 'HEAD' || m === 'OPTIONS') return false;
    if (allowRequest && allowRequest.test(String(url))) return false;
    push(state.blocked, { method: m, url: String(url).slice(0, 300) });
    return true;
  };
  const realFetch = window.fetch;
  window.fetch = function (input, init) {
    const method = (init && init.method) || (input && input.method) || 'GET';
    const url = (input && input.url) || input;
    if (writeBlocked(method, url)) return Promise.reject(new TypeError('QAJev read-only: blocked ' + method));
    return realFetch.apply(this, arguments);
  };
  const xhrOpen = XMLHttpRequest.prototype.open, xhrSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) { this.__qajev = [method, url]; return xhrOpen.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function () {
    if (this.__qajev && writeBlocked(this.__qajev[0], this.__qajev[1])) throw new DOMException('QAJev read-only', 'NetworkError');
    return xhrSend.apply(this, arguments);
  };
  if (navigator.sendBeacon) {
    const beacon = navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon = (url, data) => (writeBlocked('POST', url) ? true : beacon(url, data));
  }
  const formBlocked = (form) => writeBlocked(form.method || 'GET', form.action);
  addEventListener('submit', (e) => { if (formBlocked(e.target)) { e.preventDefault(); e.stopImmediatePropagation(); } }, true);
  const formSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () { if (!formBlocked(this)) return formSubmit.call(this); };

  // ---- Danger controls: hidden (not removed, so frameworks keep their tree) before Jev can see them. ----
  const CONTROL = 'a,button,summary,input[type=submit],input[type=button],input[type=reset],input[type=image],' +
    '[role=button],[role=link],[role=menuitem],[role=menuitemradio],[role=menuitemcheckbox],[role=tab],[role=option],[role=switch]';
  const FIELD = 'input[type=password],input[autocomplete^="cc-"],input[name*=card i],input[name*=cvc i],input[name*=cvv i],' +
    'input[name*=iban i],input[autocomplete=one-time-code],input[autocomplete=new-password],input[autocomplete=current-password]';
  const squash = (s) => String(s || '').replace(/\s+/g, ' ').trim().slice(0, 200);
  // Everything a person could read on the control: deny patterns may appear in any of it.
  const label = (el) => squash([el.innerText || el.textContent, el.getAttribute('aria-label'), el.getAttribute('title'),
    el.tagName === 'INPUT' ? el.value : ''].join(' '));
  // Its accessible name: what read-only patterns anchor to.
  const accessibleName = (el) => squash(el.getAttribute('aria-label') || el.innerText || el.textContent ||
    el.getAttribute('title') || (el.tagName === 'INPUT' ? el.value : ''));
  const offsite = (el) => {
    if (el.tagName !== 'A' || !el.href) return false;
    let url;
    try { url = new URL(el.href, location.href); } catch (e) { return true; }
    if (!/^https?:$/.test(url.protocol)) return url.protocol !== 'javascript:' && url.protocol !== 'blob:';
    return url.host !== location.host && !hosts.has(url.host);
  };
  const dangerous = (el) => {
    const text = label(el);
    if (allow && allow.test(text)) return null;
    if (deny && deny.test(text)) return 'danger: ' + text.slice(0, 60);
    if (readOnly && mutating && mutating.test(accessibleName(el))) return 'read-only: ' + text.slice(0, 60);
    if (offsite(el)) return OFFSITE;
    return null;
  };
  // An off-site link (a store badge, a social link) stays on the page as a visitor sees it, for the screenshots and
  // the checks; inert keeps it out of Jev's actions and its clicks. Risky controls are taken off the page.
  const OFFSITE = 'off-site link';
  // What the guard holds back now, and the words that made it, so a stuck or harness result can name them (a
  // tooltip's "buy" hid a "Shop" button: verse1, 4 Oct). -> {label, why, match}
  const held = new Map();
  const words = (pattern, text) => { const m = pattern && pattern.exec(text); return m ? squash(m[0]) : null; };
  // A record's label never carries an input's value: a secret field may hold one (filled by the page, the browser
  // or a hook), and the label goes into the reason and the reports. An input is named by its attributes instead.
  const named = (el) => squash(el.tagName === 'INPUT'
    ? el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || el.type
    : label(el)).slice(0, 80);
  const record = (el, why) => {
    const kind = why === OFFSITE ? OFFSITE : why === 'field' ? 'secret field' : why.split(':')[0];
    // a button input's value is its caption, so the deny words may come from it; a secret field has no match
    const match = kind === 'danger' ? words(deny, label(el)) : kind === 'read-only' ? words(mutating, accessibleName(el))
      : kind === OFFSITE ? (() => { try { return new URL(el.href, location.href).host; } catch (e) { return null; } })()
      : null;
    held.set(el, { label: named(el), why: kind, match });
  };
  const hide = (el, why) => {
    if (el.dataset.qajevGuard === why) return;
    if (el.dataset.qajevGuard) restore(el);  // its reason changed (a label rewritten): judge it afresh
    el.dataset.qajevGuard = why;
    el.inert = true;
    state.hidden++;
    record(el, why);
    if (why === OFFSITE) return;
    el.dataset.qajevDisplay = el.style.getPropertyValue('display');
    el.style.setProperty('display', 'none', 'important');
    el.setAttribute('aria-hidden', 'true');
  };
  function restore(el) {
    if (!el.dataset.qajevGuard || el.dataset.qajevGuard === 'field') return;
    if (el.dataset.qajevGuard !== OFFSITE) {
      el.style.removeProperty('display');
      if (el.dataset.qajevDisplay) el.style.setProperty('display', el.dataset.qajevDisplay);
      el.removeAttribute('aria-hidden');
    }
    el.inert = false;
    delete el.dataset.qajevGuard; delete el.dataset.qajevDisplay;
    state.hidden--;
    held.delete(el);
  }
  const judge = (el) => {
    if (el.matches(FIELD)) {
      // Secrets are the person's job. Disabled, not removed, so forms keep rendering.
      if (!(cfg.allow_secret_fields && loopback) && !el.disabled) { el.disabled = true; el.dataset.qajevGuard = 'field'; state.hidden++; record(el, 'field'); }
      return;
    }
    if (el.tagName === 'A' && el.target === '_blank' && !offsite(el)) el.target = '_self';
    const why = dangerous(el);
    if (why) hide(el, why); else restore(el);
  };
  // React (Next.js and the like) hydrates the server's HTML and reports any attribute it did not render as a
  // mismatch, the page's own error. So the guard writes nothing onto the page's elements until it has loaded (at
  // most HYDRATE_MS after it was parsed), and on a page React is hydrating, nothing onto an element until React has
  // taken it over (React checks an element in the same step that puts its fiber key on it), at most HYDRATE_MS
  // after the load. Writes stay blocked all along; QAJev waits for `pending` to empty before Jev or a hook acts.
  const HYDRATE_MS = 5000;
  const waiting = new Set();
  let readyAt = document.readyState === 'complete' ? performance.now() : null;  // re-armed in place: already loaded
  const keyed = (o, prefix) => { for (const k of Object.keys(o)) if (k.startsWith(prefix)) return true; return false; };
  const reactHydrating = () => !!(window.__NEXT_DATA__ || self.__next_f) ||
    [document, document.documentElement, document.body, ...document.querySelectorAll('#__next,#root,#app,[data-reactroot]')]
      .some((c) => c && keyed(c, '__reactContainer$'));
  const flush = () => {
    const react = reactHydrating(), late = performance.now() - readyAt > HYDRATE_MS;
    for (const el of waiting) {
      if (!el.isConnected) waiting.delete(el);
      else if (!react || late || keyed(el, '__reactFiber$')) { waiting.delete(el); judge(el); }
    }
    if (waiting.size) setTimeout(flush, 100);
  };
  const ready = () => { if (readyAt === null) { readyAt = performance.now(); flush(); } };
  if (readyAt === null) {
    addEventListener('load', ready, { once: true });
    document.addEventListener('DOMContentLoaded', () => setTimeout(ready, HYDRATE_MS), { once: true });
  }
  // Server-rendered elements (in the page by its load) wait; any added later were rendered in the page and are
  // judged at once.
  const judgeSoon = (el) => {
    if (readyAt === null || waiting.size && waiting.has(el)) waiting.add(el);
    else judge(el);
  };
  state.pending = () => waiting.size;
  const sweep = (root) => {
    if (root.nodeType !== 1) return;
    if (root.matches(CONTROL) || root.matches(FIELD)) judgeSoon(root);
    for (const el of root.querySelectorAll(CONTROL + ',' + FIELD)) judgeSoon(el);
  };
  const guardDoc = () => sweep(document.documentElement || document);
  // Observe `document`, never `document.documentElement`: at new-document time it is still null.
  new MutationObserver((records) => {
    for (const r of records) {
      if (r.type === 'childList') { for (const n of r.addedNodes) sweep(n); }
      const host = (r.target.nodeType === 1 ? r.target : r.target.parentElement);
      const control = host && host.closest && host.closest(CONTROL);
      if (control) judgeSoon(control);
    }
  }).observe(document, { childList: true, subtree: true, characterData: true, attributes: true,
    attributeFilter: ['aria-label', 'title', 'href', 'value', 'type', 'role'] });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', guardDoc, { once: true });
  guardDoc();

  // ---- Optional PII redaction: page text goes to TypeSafe. ----
  if (cfg.redact_emails) {
    const EMAIL = /[\w.+-]+@[\w-]+(\.[\w-]+)+/g;
    const redact = (root) => {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) if (EMAIL.test(n.data)) n.data = n.data.replace(EMAIL, '[email]');
    };
    new MutationObserver(() => document.body && redact(document.body)).observe(document, { childList: true, subtree: true });
  }

  // What the guard holds back now; a control the page removed is let go (the map must not keep it alive).
  const forgetGone = () => {
    for (const el of held.keys()) if (!el.isConnected) held.delete(el);
    return [...held.values()];
  };

  const BUSY = '[aria-busy="true"],[role=progressbar],.spinner,.loading,[data-loading="true"]';
  state.probe = () => {
    // A busy element counts only when it covers a real part of the screen: a small decorative progress bar in a
    // hero is not a spinner.
    const area = innerWidth * innerHeight;
    const busy = [...document.querySelectorAll(BUSY)].some((e) => {
      if (!(e.checkVisibility && e.checkVisibility())) return false;
      const r = e.getBoundingClientRect();
      const w = Math.max(0, Math.min(r.right, innerWidth) - Math.max(r.left, 0));
      const h = Math.max(0, Math.min(r.bottom, innerHeight) - Math.max(r.top, 0));
      return w * h >= 0.15 * area;
    });
    const text = (document.body && document.body.innerText || '').trim();
    const blank = busy || text.length < 30;
    if (blank && state.busySince === null) state.busySince = performance.now();
    if (!blank) state.busySince = null;
    const out = {
      v: state.v, mode: state.mode, deaf: state.deaf && window.webkitSpeechRecognition === DeafRecognition,
      hidden: state.hidden, errors: state.errors.splice(0), blocked: state.blocked.splice(0), pending: waiting.size,
      guard_hydration: state.hydration, guard_hydration_details: state.hydrationText.splice(0),
      hidden_controls: forgetGone().slice(0, 20),
      blank_ms: state.busySince === null ? 0 : Math.round(performance.now() - state.busySince),
      lcp: state.lcp, cls: Math.round(state.cls * 1000) / 1000,
    };
    if (signInWall()) out.sign_in = true;
    return out;
  };
  // A page asking the visitor to sign in: a password field on screen, or a sign-in address with an email field
  // (the first step of a two-step sign-in). What a run that is not signed in meets instead of the page it wanted.
  const SIGNIN_PATH = /(^|[\/_.-])(log-?in|sign-?in|auth|sso|session|identifier)([\/_.-]|$)/i;
  const IDENTITY = 'input[type=email],input[autocomplete=username],input[autocomplete=email],input[name*=email i],' +
    'input[name*=user i],input[name*=login i]';
  const shown = (e) => (e.checkVisibility ? e.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) : true);
  function signInWall() {
    if ([...document.querySelectorAll('input[type=password]')].some(shown)) return true;
    return SIGNIN_PATH.test(location.pathname) && [...document.querySelectorAll(IDENTITY)].some(shown);
  }
  state.reconfigure = (next) => { configure(next); guardDoc(); };
  Object.defineProperty(window, '__qajev', { value: state, configurable: false, writable: false, enumerable: false });
})();
