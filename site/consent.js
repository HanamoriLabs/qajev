/*! Hanamori consent banner + Google Tag Manager loader. Copy this one file to each marketing site
 * and serve it first-party:
 *
 *   <script src="/consent.js" data-gtm="GTM-XXXXXXX" data-privacy="/privacy" defer></script>
 *
 * - Visitors in the EEA, the UK and Switzerland see a banner (Accept and Reject, equally prominent).
 *   Nothing that sets ad or analytics cookies loads for them until they accept.
 * - Everyone else gets tags without a banner, except that Global Privacy Control turns ads off.
 * - Google Tag Manager only loads once something is granted, after the page's load event, when the
 *   browser is idle or on the first interaction, never in <head>.
 * - It never runs on account, app, login, signup, checkout or delete pages (DENY_PATHS, plus
 *   data-deny="/extra,/paths"), so no pixel ever sees those.
 * - Region comes from Cloudflare's /cdn-cgi/trace (loc=). An unknown region is treated as in-region.
 * - Footer links: <a href="#" data-consent-settings>Cookie settings</a> and
 *   <a href="#" data-privacy-choices>Your privacy choices</a> reopen the banner.
 * - Click tracking: data-track="signup_started" on a CTA pushes {event: "signup_started"} to the dataLayer.
 * - Theming: set --hc-bg, --hc-fg, --hc-muted, --hc-border, --hc-button-bg, --hc-button-fg,
 *   --hc-radius and --hc-font on :root.
 */
(function () {
  "use strict";

  // EU 27 + Iceland, Liechtenstein, Norway (EEA) + United Kingdom + Switzerland.
  var CONSENT_REGIONS = [
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV",
    "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
    "IS", "LI", "NO",
    "GB", "CH",
  ];
  var DENY_PATHS = /^\/(account|accounts|dashboard|console|app|admin|login|logout|signin|sign-in|signup|sign-up|register|auth|checkout|checkouts|cart|orders|billing|delete-[\w-]*)(\/|$)/i;
  var COOKIE = "hc_consent";
  var COOKIE_MAX_AGE = 60 * 60 * 24 * 365; // 12 months

  /** Reads loc= from a Cloudflare /cdn-cgi/trace body ("fl=...\nloc=GB\n..." gives "GB"), or null. */
  function parseTrace(text) {
    var m = /(?:^|\n)loc=([A-Z]{2})(?:\n|$)/.exec(String(text || ""));
    return m ? m[1] : null;
  }

  function inConsentRegion(loc) {
    return loc == null || CONSENT_REGIONS.indexOf(String(loc).toUpperCase()) !== -1;
  }

  function isDeniedPath(path, extra) {
    if (DENY_PATHS.test(path || "/")) return true;
    return (extra || []).some(function (p) {
      p = String(p).trim();
      return p && (path === p || path.indexOf(p.replace(/\/$/, "") + "/") === 0);
    });
  }

  /**
   * What to do for one page view. stored is the visitor's own choice ("granted" / "denied" / null);
   * gpc is navigator.globalPrivacyControl. An explicit choice wins, except that GPC always keeps ads off.
   */
  function decide(input) {
    var region = inConsentRegion(input.loc);
    var stored = input.stored === "granted" || input.stored === "denied" ? input.stored : null;
    var analytics = stored ? stored === "granted" : !region;
    var ads = analytics && !input.gpc;
    return {
      showBanner: region && !stored,
      ads: ads,
      analytics: analytics,
      loadGtm: ads || analytics,
    };
  }

  /** Google Consent Mode v2 fields for a decision. */
  function consentState(d) {
    var ad = d.ads ? "granted" : "denied";
    return { ad_storage: ad, ad_user_data: ad, ad_personalization: ad, analytics_storage: d.analytics ? "granted" : "denied" };
  }

  var core = { CONSENT_REGIONS: CONSENT_REGIONS, parseTrace: parseTrace, inConsentRegion: inConsentRegion, isDeniedPath: isDeniedPath, decide: decide, consentState: consentState };
  if (typeof module !== "undefined" && module.exports) module.exports = core;
  if (typeof window === "undefined" || typeof document === "undefined") return;

  // ---- In the browser -------------------------------------------------------------------------
  var script = document.currentScript;
  var cfg = {
    gtm: script && script.getAttribute("data-gtm"),
    privacy: (script && script.getAttribute("data-privacy")) || "/privacy",
    deny: ((script && script.getAttribute("data-deny")) || "").split(",").filter(Boolean),
  };
  var noop = function () {};
  if (isDeniedPath(location.pathname, cfg.deny)) {
    window.hanamoriConsent = { open: noop, status: function () { return "off"; }, track: noop };
    return;
  }

  window.dataLayer = window.dataLayer || [];
  function gtag() { window.dataLayer.push(arguments); }
  var gpc = navigator.globalPrivacyControl === true;
  // Belt and braces for Google's own tags: denied by default in the consent regions, granted elsewhere.
  gtag("consent", "default", { ad_storage: "denied", ad_user_data: "denied", ad_personalization: "denied", analytics_storage: "denied", region: CONSENT_REGIONS });
  gtag("consent", "default", consentState({ ads: !gpc, analytics: true }));

  function readCookie() {
    var m = document.cookie.match(new RegExp("(?:^|; )" + COOKIE + "=(granted|denied)"));
    return m ? m[1] : null;
  }
  function writeCookie(value) {
    document.cookie = COOKIE + "=" + value + "; Max-Age=" + COOKIE_MAX_AGE + "; Path=/; SameSite=Lax" + (location.protocol === "https:" ? "; Secure" : "");
  }

  var gtmLoaded = false;
  function loadGtmNow() {
    if (gtmLoaded || !cfg.gtm) return;
    gtmLoaded = true;
    window.dataLayer.push({ "gtm.start": Date.now(), event: "gtm.js" });
    var s = document.createElement("script");
    s.async = true;
    s.src = "https://www.googletagmanager.com/gtm.js?id=" + encodeURIComponent(cfg.gtm);
    document.head.appendChild(s);
  }
  /** After load, then whichever comes first: an idle moment or the first interaction. Once per page. */
  var gtmScheduled = false;
  function scheduleGtm() {
    if (gtmScheduled) return;
    gtmScheduled = true;
    var fired = false;
    var events = ["pointerdown", "keydown", "scroll", "touchstart"];
    function go() {
      if (fired) return;
      fired = true;
      events.forEach(function (e) { removeEventListener(e, go, true); });
      loadGtmNow();
    }
    function afterLoad() {
      events.forEach(function (e) { addEventListener(e, go, { capture: true, passive: true, once: true }); });
      if ("requestIdleCallback" in window) requestIdleCallback(go, { timeout: 5000 });
      else setTimeout(go, 3000);
    }
    if (document.readyState === "complete") afterLoad();
    else addEventListener("load", afterLoad, { once: true });
  }

  function apply(d) {
    gtag("consent", "update", consentState(d));
    if (d.loadGtm) scheduleGtm();
  }

  // ---- Banner ----------------------------------------------------------------------------------
  var CSS =
    ".hc-banner{position:fixed;z-index:2147483000;left:16px;right:16px;bottom:16px;max-width:560px;margin:0 auto;box-sizing:border-box;" +
    "padding:16px 18px;background:var(--hc-bg,#14161a);color:var(--hc-fg,#f4f4f5);border:1px solid var(--hc-border,rgba(255,255,255,.14));" +
    "border-radius:var(--hc-radius,12px);font:14px/1.5 var(--hc-font,inherit);box-shadow:0 8px 32px rgba(0,0,0,.28)}" +
    ".hc-banner p{margin:0 0 12px}.hc-banner a{color:inherit;text-decoration:underline}" +
    ".hc-actions{display:flex;gap:8px;flex-wrap:wrap}" +
    ".hc-actions button{flex:1 1 140px;min-height:44px;padding:0 16px;font:inherit;font-weight:600;cursor:pointer;" +
    "background:var(--hc-button-bg,#f4f4f5);color:var(--hc-button-fg,#14161a);border:1px solid var(--hc-button-bg,#f4f4f5);border-radius:calc(var(--hc-radius,12px) - 4px)}" +
    ".hc-actions button:focus-visible{outline:2px solid var(--hc-fg,#f4f4f5);outline-offset:2px}" +
    ".hc-banner .hc-note{margin:10px 0 0;font-size:12px;color:var(--hc-muted,rgba(244,244,245,.7))}";
  function addStyles() {
    if (document.getElementById("hc-style")) return;
    // A constructed sheet keeps the banner working under a CSP without 'unsafe-inline' styles.
    try {
      var sheet = new CSSStyleSheet();
      sheet.replaceSync(CSS);
      document.adoptedStyleSheets = document.adoptedStyleSheets.concat([sheet]);
      var mark = document.createElement("meta");
      mark.id = "hc-style";
      document.head.appendChild(mark);
    } catch (e) {
      var style = document.createElement("style");
      style.id = "hc-style";
      style.textContent = CSS;
      document.head.appendChild(style);
    }
  }

  var banner = null;
  function closeBanner() {
    if (banner) banner.remove();
    banner = null;
  }
  function choose(value) {
    writeCookie(value);
    closeBanner();
    apply(decide({ loc: null, stored: value, gpc: gpc }));
  }
  function openBanner() {
    if (banner) return;
    addStyles();
    banner = document.createElement("div");
    banner.className = "hc-banner";
    banner.setAttribute("role", "region");
    banner.setAttribute("aria-label", "Cookie choices");
    var p = document.createElement("p");
    p.appendChild(document.createTextNode("We use cookies to measure our ads and see which ones work. Accept to allow them, or reject them; the site works the same either way. "));
    var a = document.createElement("a");
    a.href = cfg.privacy;
    a.textContent = "Privacy policy";
    p.appendChild(a);
    var actions = document.createElement("div");
    actions.className = "hc-actions";
    [["Reject", "denied"], ["Accept", "granted"]].forEach(function (pair) {
      var b = document.createElement("button");
      b.type = "button";
      b.textContent = pair[0];
      b.addEventListener("click", function () { choose(pair[1]); });
      actions.appendChild(b);
    });
    banner.appendChild(p);
    banner.appendChild(actions);
    if (gpc) {
      var note = document.createElement("p");
      note.className = "hc-note";
      note.textContent = "Your browser sends Global Privacy Control, so ad cookies stay off whatever you choose.";
      banner.appendChild(note);
    }
    document.body.appendChild(banner);
  }

  document.addEventListener("click", function (e) {
    var t = e.target instanceof Element ? e.target : null;
    if (!t) return;
    var link = t.closest("[data-consent-settings],[data-privacy-choices]");
    if (link) {
      e.preventDefault();
      openBanner();
      return;
    }
    var tracked = t.closest("[data-track]");
    if (tracked) window.dataLayer.push({ event: tracked.getAttribute("data-track") });
  });

  window.hanamoriConsent = {
    open: openBanner,
    status: function () { return readCookie() || "unset"; },
    track: function (event, params) { window.dataLayer.push(Object.assign({ event: event }, params || {})); },
  };

  // ---- Start -----------------------------------------------------------------------------------
  var stored = readCookie();
  function start(loc) {
    var d = decide({ loc: loc, stored: stored, gpc: gpc });
    if (d.showBanner) {
      if (document.body) openBanner();
      else addEventListener("DOMContentLoaded", openBanner, { once: true });
    }
    apply(d);
  }
  if (stored) {
    start(null);
    return;
  }
  var cached = null;
  try { cached = sessionStorage.getItem("hc_loc"); } catch (e) {}
  if (cached) {
    start(cached === "??" ? null : cached);
    return;
  }
  fetch("/cdn-cgi/trace", { cache: "no-store", credentials: "omit" })
    .then(function (r) { return r.ok ? r.text() : ""; })
    .catch(function () { return ""; })
    .then(function (text) {
      var loc = parseTrace(text);
      try { sessionStorage.setItem("hc_loc", loc || "??"); } catch (e) {}
      start(loc);
    });
})();
