// UX facts for `qajev smoke`, measured from the page (no model): text contrast against WCAG 2.2 AA, text cut off by
// its box, text boxes overlapping, an open dialog's state, and the computed style of each kind of element (for the
// cross-page design-consistency report). Everything is a measurement with its threshold; what cannot be measured
// (text over an image or a gradient, faded text) is counted as such, never guessed. Returns plain JSON.
(() => {
  const visible = (e) => e.checkVisibility ? e.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) : true;
  const short = (s) => String(s || '').replace(/\s+/g, ' ').trim().slice(0, 40);
  const path = (e) => {
    const self = e.tagName.toLowerCase() + (e.id ? '#' + CSS.escape(e.id)
      : [...e.classList].slice(0, 2).map((c) => '.' + CSS.escape(c)).join(''));
    const up = !e.id && e.parentElement && e.parentElement.closest('[id]');
    return up ? `#${CSS.escape(up.id)} ${self}` : self;
  };
  const rgba = (s) => {
    const m = String(s).match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(/[\s,/]+/).filter(Boolean).map(Number);
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
  };
  const over = (top, under) => {  // `top` composited over an opaque `under`
    const a = top[3];
    return [0, 1, 2].map((i) => top[i] * a + under[i] * (1 - a)).concat(1);
  };
  const lum = (c) => {
    const l = c.slice(0, 3).map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * l[0] + 0.7152 * l[1] + 0.0722 * l[2];
  };
  const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
  const hex = (c) => '#' + c.slice(0, 3).map((v) => Math.round(v).toString(16).padStart(2, '0')).join('');
  // The background a text really sits on: its own and its ancestors' colours, composited down to the page's white.
  // null when an image or a gradient is under it (not measurable from styles).
  const backdrop = (e) => {
    const layers = [];
    for (let n = e; n && n.nodeType === 1; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.backgroundImage && cs.backgroundImage !== 'none') return null;
      const c = rgba(cs.backgroundColor);
      if (c && c[3] > 0) { layers.push(c); if (c[3] >= 1) break; }
    }
    return layers.reverse().reduce((under, c) => over(c, under), [255, 255, 255, 1]);
  };
  const faded = (e) => { for (let n = e; n && n.nodeType === 1; n = n.parentElement) if (+getComputedStyle(n).opacity < 1) return true; return false; };
  // Hidden on purpose for screen readers only (the "sr-only" pattern: a 1x1 box, clipped): no one sees it, so it is no
  // visible text to measure. SideGame1, 5 Oct: a live region was reported as text cut off.
  const srOnly = (e) => {
    for (let n = e; n && n.nodeType === 1; n = n.parentElement) {
      const cs = getComputedStyle(n), r = n.getBoundingClientRect();
      if (/inset\(\s*(50|100)%/.test(cs.clipPath) || (cs.clip && cs.clip !== 'auto' && /absolute|fixed/.test(cs.position))) return true;
      if (r.width <= 1 && r.height <= 1 && /hidden|clip/.test(cs.overflow)) return true;
    }
    return false;
  };

  // ---- text: every element with its own visible text
  const texts = [];
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  for (let t = walker.nextNode(); t && texts.length < 3000; t = walker.nextNode()) {
    const e = t.parentElement;
    if (!e || seen.has(e) || !/\S/.test(t.textContent) || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE|OPTION)$/.test(e.tagName)) continue;
    seen.add(e);
    if (!visible(e)) continue;
    const r = e.getBoundingClientRect();
    if (r.width < 1 || r.height < 1 || srOnly(e)) continue;
    texts.push(e);
  }

  // ---- contrast (WCAG 2.2 AA 1.4.3): 4.5:1, or 3:1 for large text (24px, or 18.66px bold); disabled controls exempt
  const low = [], skipped = { image_or_gradient: 0, faded: 0, disabled: 0 };
  let measured = 0;
  for (const e of texts) {
    if (e.closest(':disabled, [aria-disabled="true"]')) { skipped.disabled++; continue; }
    if (faded(e)) { skipped.faded++; continue; }
    const bg = backdrop(e);
    if (!bg) { skipped.image_or_gradient++; continue; }
    const cs = getComputedStyle(e), fg = rgba(cs.color);
    if (!fg) continue;
    measured++;
    const size = parseFloat(cs.fontSize), bold = +cs.fontWeight >= 700;
    const need = size >= 24 || (size >= 18.66 && bold) ? 3 : 4.5;
    const got = ratio(over(fg, bg), bg);
    if (got + 1e-9 < need) low.push({ text: short(e.textContent), ratio: Math.round(got * 100) / 100, need,
                                      fg: hex(over(fg, bg)), bg: hex(bg), size: Math.round(size), path: path(e) });
  }

  // ---- text cut off by its own box (scrolled out of an overflow:hidden/clip box, not an ellipsis on purpose)
  const clipped = [];
  for (const e of texts) {
    const cs = getComputedStyle(e);
    const hides = (v) => v === 'hidden' || v === 'clip';
    const cutX = hides(cs.overflowX) && e.scrollWidth > e.clientWidth + 1 && cs.textOverflow !== 'ellipsis';
    const cutY = hides(cs.overflowY) && e.scrollHeight > e.clientHeight + 1;
    if ((cutX || cutY) && e.clientWidth > 0) {
      clipped.push({ text: short(e.textContent), path: path(e), cut: cutX && cutY ? 'both' : cutX ? 'width' : 'height',
                     box: `${e.clientWidth}x${e.clientHeight}`, content: `${e.scrollWidth}x${e.scrollHeight}` });
    }
  }

  // ---- text boxes overlapping another text box (neither inside the other), by at least 4 px each way, where a person
  // sees both: at the overlap's middle, nothing opaque lies between the two. SideGame1, 5 Oct: a title screen's text
  // under an opaque full-screen splash was reported. The hit test needs the point on screen; a pair off screen, or one
  // that takes no hits (pointer-events: none), keeps the box test alone.
  const covered = (a, b, x, y) => {
    if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return false;
    const stack = document.elementsFromPoint(x, y);
    const ia = stack.findIndex((n) => a.contains(n)), ib = stack.findIndex((n) => b.contains(n));
    if (ia < 0 || ib < 0) return false;
    return stack.slice(Math.min(ia, ib) + 1, Math.max(ia, ib)).some((n) => {
      const cs = getComputedStyle(n), c = rgba(cs.backgroundColor);
      return (c && c[3] >= 0.95) || (cs.backgroundImage && cs.backgroundImage !== 'none');
    });
  };
  const overlaps = [];
  const boxes = texts.slice(0, 600).map((e) => ({ e, r: e.getBoundingClientRect() }));
  for (let i = 0; i < boxes.length && overlaps.length < 10; i++) {
    for (let j = i + 1; j < boxes.length && overlaps.length < 10; j++) {
      const a = boxes[i], b = boxes[j];
      if (a.e.contains(b.e) || b.e.contains(a.e)) continue;
      const left = Math.max(a.r.left, b.r.left), top = Math.max(a.r.top, b.r.top);
      const w = Math.min(a.r.right, b.r.right) - left, h = Math.min(a.r.bottom, b.r.bottom) - top;
      if (w >= 4 && h >= 4 && !covered(a.e, b.e, left + w / 2, top + h / 2))
        overlaps.push({ a: short(a.e.textContent), b: short(b.e.textContent), aPath: path(a.e),
                        bPath: path(b.e), area: `${Math.round(w)}x${Math.round(h)}` });
    }
  }

  // ---- an open dialog: named, modal, focus inside it (Escape is tried from Python: it changes the page)
  const dialogs = [...document.querySelectorAll('dialog[open], [role="dialog"], [role="alertdialog"], [aria-modal="true"]')]
    .filter((d) => visible(d) && d.getBoundingClientRect().width > 0);
  const d = dialogs[0];
  const dialog = d ? {
    path: path(d),
    named: !!(d.getAttribute('aria-label') || d.getAttribute('aria-labelledby') || d.getAttribute('title')),
    modal: d.getAttribute('aria-modal') === 'true' || (d.tagName === 'DIALOG' && d.matches(':modal')),
    focus_inside: d.contains(document.activeElement),
    text: short(d.textContent),
  } : null;

  // ---- the computed style of each kind of element, for comparing pages (the most common style per kind here)
  const KINDS = {
    h1: 'h1', h2: 'h2', h3: 'h3', 'body text': 'main p, article p, p',
    'inline link': 'p a[href], li a[href]',
    button: 'button, [role="button"], input[type="submit"], input[type="button"], a[class*="btn"], a[class*="button"]',
    'text field': 'input[type="text"], input[type="email"], input[type="search"], input:not([type]), textarea, select',
  };
  const FIELDS = {
    h1: ['font-family', 'font-size', 'font-weight', 'color', 'text-transform', 'letter-spacing'],
    'body text': ['font-family', 'font-size', 'font-weight', 'line-height', 'color'],
    'inline link': ['color', 'text-decoration-line', 'font-weight'],
    button: ['font-family', 'font-size', 'font-weight', 'color', 'background-color', 'border-radius', 'padding',
             'text-transform'],
    'text field': ['font-size', 'color', 'background-color', 'border-radius', 'border-top-width', 'border-top-color',
                   'padding'],
  };
  FIELDS.h2 = FIELDS.h3 = FIELDS.h1;
  const styles = {};
  for (const [kind, sel] of Object.entries(KINDS)) {
    const counts = new Map();
    for (const e of [...document.querySelectorAll(sel)].slice(0, 200)) {
      if (!visible(e) || e.closest('[data-qajev-guard]')) continue;
      const cs = getComputedStyle(e);
      const sig = {};
      for (const f of FIELDS[kind]) sig[f] = f === 'font-family' ? cs.getPropertyValue(f).split(',')[0].trim().replace(/["']/g, '')
        : cs.getPropertyValue(f);
      const key = JSON.stringify(sig);
      // a field by its label or name, never by what is typed in it
      const said = /^(INPUT|SELECT|TEXTAREA)$/.test(e.tagName)
        ? e.getAttribute('aria-label') || e.getAttribute('placeholder') || e.getAttribute('name') || e.type
        : e.textContent;
      const c = counts.get(key) || { style: sig, count: 0, example: short(said), path: path(e) };
      c.count++;
      counts.set(key, c);
    }
    if (counts.size) styles[kind] = [...counts.values()].sort((a, b) => b.count - a.count).slice(0, 6);
  }

  return {
    contrast: { measured, low: low.slice(0, 30), low_count: low.length, skipped },
    clipped: clipped.slice(0, 20), clipped_count: clipped.length,
    overlaps,
    dialog,
    styles,
  };
})()
