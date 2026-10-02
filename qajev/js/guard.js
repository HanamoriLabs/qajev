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

  const state = { v: '', mode: '', deaf: false, hidden: 0, errors: [], blocked: [], lcp: 0, cls: 0, busySince: null };
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

  // ---- Errors, failed requests, vitals: product signals collected for the report. ----
  const where = (el) => el && (el.src || el.href || el.currentSrc || el.tagName);
  addEventListener('error', (e) => {
    if (e.target && e.target !== window) push(state.errors, { kind: 'resource', detail: String(where(e.target)) });
    else push(state.errors, { kind: 'exception', detail: String(e.message || e.error) });
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
    push(state.errors, { kind: 'console', detail: args.map((a) => (a && a.message) || String(a)).join(' ').slice(0, 500) });
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
  const hide = (el, why) => {
    if (el.dataset.qajevGuard === why) return;
    if (el.dataset.qajevGuard) restore(el);  // its reason changed (a label rewritten): judge it afresh
    el.dataset.qajevGuard = why;
    el.inert = true;
    state.hidden++;
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
  }
  const judge = (el) => {
    if (el.matches(FIELD)) {
      // Secrets are the person's job. Disabled, not removed, so forms keep rendering.
      if (!(cfg.allow_secret_fields && loopback) && !el.disabled) { el.disabled = true; el.dataset.qajevGuard = 'field'; state.hidden++; }
      return;
    }
    if (el.tagName === 'A' && el.target === '_blank' && !offsite(el)) el.target = '_self';
    const why = dangerous(el);
    if (why) hide(el, why); else restore(el);
  };
  const sweep = (root) => {
    if (root.nodeType !== 1) return;
    if (root.matches(CONTROL) || root.matches(FIELD)) judge(root);
    for (const el of root.querySelectorAll(CONTROL + ',' + FIELD)) judge(el);
  };
  const guardDoc = () => sweep(document.documentElement || document);
  // Observe `document`, never `document.documentElement`: at new-document time it is still null.
  new MutationObserver((records) => {
    for (const r of records) {
      if (r.type === 'childList') { for (const n of r.addedNodes) sweep(n); }
      const host = (r.target.nodeType === 1 ? r.target : r.target.parentElement);
      const control = host && host.closest && host.closest(CONTROL);
      if (control) judge(control);
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
      hidden: state.hidden, errors: state.errors.splice(0), blocked: state.blocked.splice(0),
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
