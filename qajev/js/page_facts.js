// Deterministic page facts for `qajev smoke`: no model involved. Returns plain JSON.
(() => {
  const visible = (e) => e.checkVisibility ? e.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) : true;
  const name = (e) => (e.getAttribute('aria-label') || e.getAttribute('title') || e.innerText || e.value || '').trim();
  const labelled = (e) => {
    if (e.getAttribute('aria-label') || e.getAttribute('aria-labelledby') || e.getAttribute('title')) return true;
    if (e.id && document.querySelector(`label[for="${CSS.escape(e.id)}"]`)) return true;
    return !!e.closest('label');
  };
  const nav = performance.getEntriesByType('navigation')[0] || {};
  const links = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    if (a.dataset.qajevGuard || a.hasAttribute('download')) continue;
    try {
      const u = new URL(a.getAttribute('href'), location.href);
      if (!/^https?:$/.test(u.protocol) || u.host !== location.host) continue;
      u.hash = '';
      links.add(u.href);
    } catch (e) { /* malformed href is reported below */ }
  }
  const ids = {};
  for (const e of document.querySelectorAll('[id]')) if (e.id) ids[e.id] = (ids[e.id] || 0) + 1;
  const imgs = [...document.querySelectorAll('img')];
  const fields = [...document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]),select,textarea')]
    .filter(visible);
  const buttons = [...document.querySelectorAll('button,[role=button]')].filter(visible);
  const text = (document.body && document.body.innerText || '').trim();
  const mixed = location.protocol === 'https:'
    ? performance.getEntriesByType('resource').filter((r) => r.name.startsWith('http:')).map((r) => r.name) : [];
  // Tap targets under 24 CSS px, as WCAG 2.5.8 counts them: a link inside a sentence is exempt (inline), and so is
  // an undersized target with room around it (spacing: a 24 px circle on its centre meets no other target and no
  // other undersized target's circle). Each counted one is named, never by an input's value.
  const targets = [...document.querySelectorAll('a,button,[role=button],input,select,textarea')].filter(visible)
    .map((e) => ({ e, r: e.getBoundingClientRect() })).filter(({ r }) => r.width > 0 && r.height > 0);
  const undersized = targets.filter(({ r }) => r.width < 24 || r.height < 24);
  const under = new Set(undersized);
  const centre = (r) => [r.left + r.width / 2, r.top + r.height / 2];
  const toBox = ([x, y], r) => Math.hypot(Math.max(r.left - x, 0, x - r.right), Math.max(r.top - y, 0, y - r.bottom));
  const inline = (e) => getComputedStyle(e).display === 'inline' && !!e.parentElement
    && [...e.parentElement.childNodes].some((n) => n.nodeType === 3 && /\S/.test(n.textContent));
  const spaced = (t) => {
    const c = centre(t.r);
    return targets.every((o) => o === t || (toBox(c, o.r) >= 12
      && !(under.has(o) && Math.hypot(c[0] - centre(o.r)[0], c[1] - centre(o.r)[1]) < 24)));
  };
  const skipped = { inline: 0, spaced: 0 };
  const small = undersized.filter((t) => {
    if (inline(t.e)) { skipped.inline++; return false; }
    if (spaced(t)) { skipped.spaced++; return false; }
    return true;
  });
  const short = (s) => String(s || '').replace(/\s+/g, ' ').trim().slice(0, 40);
  const said = (e) => (/^(INPUT|SELECT|TEXTAREA)$/.test(e.tagName)
    ? e.getAttribute('aria-label') || e.getAttribute('placeholder') || e.getAttribute('name') || e.type
    : e.getAttribute('aria-label') || e.getAttribute('title') || e.innerText);
  const path = (e) => {
    const self = e.tagName.toLowerCase() + (e.id ? '#' + CSS.escape(e.id)
      : [...e.classList].slice(0, 2).map((c) => '.' + CSS.escape(c)).join(''));
    const up = !e.id && e.parentElement && e.parentElement.closest('[id]');
    return up ? `#${CSS.escape(up.id)} ${self}` : self;
  };
  return {
    url: location.href,
    status: nav.responseStatus || null,
    title: document.title,
    lang: document.documentElement.getAttribute('lang'),
    description: !!document.querySelector('meta[name=description][content]'),
    viewport_meta: !!document.querySelector('meta[name=viewport]'),
    // Headings a person or a screen reader gets: a display:none one (a dev build's hidden menu, SideGame1 5 Oct) is
    // counted apart. A visually hidden (sr-only) h1 still counts: screen readers read it.
    h1: [...document.querySelectorAll('h1')].filter((h) => !h.checkVisibility || h.checkVisibility({ checkVisibilityCSS: true })).length,
    h1_hidden: [...document.querySelectorAll('h1')].filter((h) => h.checkVisibility && !h.checkVisibility({ checkVisibilityCSS: true })).length,
    text_chars: text.length,
    links: [...links],
    broken_images: imgs.filter((i) => i.complete && i.getAttribute('src') && i.naturalWidth === 0).map((i) => i.currentSrc || i.src).slice(0, 20),
    // Only images a person can see: tracking pixels (1x1, display:none) and decorative ones do not need alt text.
    images_without_alt: imgs.filter((i) => {
      if (i.hasAttribute('alt') || i.closest('[aria-hidden="true"]') || /^(presentation|none)$/.test(i.getAttribute('role') || '')) return false;
      const r = i.getBoundingClientRect();
      return visible(i) && r.width > 1 && r.height > 1;
    }).map((i) => (i.currentSrc || i.src || '').slice(0, 200)).slice(0, 20),
    unlabelled_fields: fields.filter((f) => !labelled(f)).length,
    unnamed_buttons: buttons.filter((b) => !name(b)).length,
    duplicate_ids: Object.entries(ids).filter(([, n]) => n > 1).map(([id, n]) => `${id} (x${n})`).slice(0, 20),
    horizontal_overflow: document.documentElement.scrollWidth > innerWidth + 1,
    small_targets: small.length,
    small_targets_skipped: skipped,
    small_target_samples: small.slice(0, 10).map(({ e, r }) => ({
      tag: e.tagName.toLowerCase(), text: short(said(e)), size: `${Math.round(r.width)}x${Math.round(r.height)}`,
      path: path(e),
    })),
    mixed_content: mixed.slice(0, 20),
    timing: {
      ttfb: Math.round(nav.responseStart || 0),
      dom_content_loaded: Math.round(nav.domContentLoadedEventEnd || 0),
      load: Math.round(nav.loadEventEnd || 0),
    },
  };
})()
