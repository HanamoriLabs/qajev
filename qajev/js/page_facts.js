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
  const small = [...document.querySelectorAll('a,button,[role=button],input,select')].filter(visible).filter((e) => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && (r.width < 24 || r.height < 24);
  });
  return {
    url: location.href,
    status: nav.responseStatus || null,
    title: document.title,
    lang: document.documentElement.getAttribute('lang'),
    description: !!document.querySelector('meta[name=description][content]'),
    viewport_meta: !!document.querySelector('meta[name=viewport]'),
    h1: document.querySelectorAll('h1').length,
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
    mixed_content: mixed.slice(0, 20),
    timing: {
      ttfb: Math.round(nav.responseStart || 0),
      dom_content_loaded: Math.round(nav.domContentLoadedEventEnd || 0),
      load: Math.round(nav.loadEventEnd || 0),
    },
  };
})()
