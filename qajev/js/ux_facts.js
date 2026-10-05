// UX facts for `qajev smoke`, measured from the page (no model): text contrast against WCAG 2.2 AA, text cut off by
// a box, text drawn outside its box or over other text, an open dialog's state, and the computed style of each kind
// of element (for the cross-page design-consistency report). Everything is a measurement with its threshold; what cannot be measured
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

  // ---- where each text's own words are drawn: one rect per line of its own text nodes, cut down to what the boxes
  // around it show. FlockTab1 and verse1, 5 Oct: an element's box is not its text. A wrapped inline's box spans both
  // lines; a block's text can be drawn past its box, into the next card; a scroller's lines out of view still have
  // boxes. An absolutely positioned box escapes the clips between it and its containing block; a fixed one, all of
  // them. A line's rect is the font's full height, so 15% is taken off its top and bottom (the glyphs), else tight
  // line heights would read as lines drawn over each other.
  const INK = 0.15;
  const clipsOf = (e) => {
    const out = [];
    let escaping = false;
    for (let n = e; n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (escaping) {
        if (cs.position === 'static' && cs.transform === 'none') continue;
        escaping = false;
      }
      if (cs.overflowX !== 'visible' || cs.overflowY !== 'visible') {
        const r = n.getBoundingClientRect(), left = r.left + n.clientLeft, top = r.top + n.clientTop;
        out.push({ n, cs, left, top, right: left + n.clientWidth, bottom: top + n.clientHeight,
                   x: cs.overflowX !== 'visible', y: cs.overflowY !== 'visible' });
      }
      if (cs.position === 'fixed') break;
      if (cs.position === 'absolute') escaping = true;
    }
    return out;
  };
  const cutTo = (r, c) => ({ left: c.x ? Math.max(r.left, c.left) : r.left, right: c.x ? Math.min(r.right, c.right) : r.right,
                             top: c.y ? Math.max(r.top, c.top) : r.top, bottom: c.y ? Math.min(r.bottom, c.bottom) : r.bottom });
  const empty = (r) => r.right - r.left < 1 || r.bottom - r.top < 1;
  const range = document.createRange();
  const lines = (e) => {
    const out = [];
    for (const t of e.childNodes) {
      if (t.nodeType !== 3 || !/\S/.test(t.textContent)) continue;
      range.selectNodeContents(t);
      for (const r of range.getClientRects()) {
        if (r.width < 1 || r.height < 1 || out.length >= 60) continue;
        if (r.right + scrollX < 0 || r.bottom + scrollY < 0) continue;  // moved off the page on purpose (text-indent)
        const inset = r.height * INK;
        out.push({ left: r.left, right: r.right, top: r.top + inset, bottom: r.bottom - inset });
      }
    }
    return out;
  };
  const drawn = texts.slice(0, 1500).map((e) => {
    const clips = clipsOf(e);
    return { e, clips, lines: lines(e).map((r) => ({ r, v: clips.reduce(cutTo, r) })) };
  });

  // ---- text cut off: by its own box (overflow hidden or clip; not an ellipsis or a line clamp on purpose), or partly
  // by a box it sits in (FlockTab1: "$48.18of / $50.00" in a fixed row, its end clipped). Text wholly out of view is
  // not counted (a carousel's other slides), nor text in a moving track (a transformed box inside the clip).
  const clipped = [], cutOff = new Set();
  const hides = (v) => v === 'hidden' || v === 'clip';
  const meant = (cs) => cs.textOverflow === 'ellipsis' || (cs.webkitLineClamp && cs.webkitLineClamp !== 'none');
  for (const e of texts) {
    const cs = getComputedStyle(e);
    const cutX = hides(cs.overflowX) && e.scrollWidth > e.clientWidth + 1 && cs.textOverflow !== 'ellipsis';
    const cutY = hides(cs.overflowY) && e.scrollHeight > e.clientHeight + 1 && !meant(cs);
    if ((cutX || cutY) && e.clientWidth > 0) {
      cutOff.add(e);
      clipped.push({ text: short(e.textContent), path: path(e), cut: cutX && cutY ? 'both' : cutX ? 'width' : 'height',
                     box: `${e.clientWidth}x${e.clientHeight}`, content: `${e.scrollWidth}x${e.scrollHeight}` });
    }
  }
  // a track that moves (a transform, a translate or an animation, on the text or between it and the clip): a carousel
  // or a ticker shows its parts in turn, on purpose (flocktab.com's provider ticker, 5 Oct)
  const moving = (e, top) => {
    for (let n = e; n && n !== top; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.transform !== 'none' || (cs.translate && cs.translate !== 'none') || cs.animationName !== 'none') return true;
    }
    return false;
  };
  for (const { e, clips, lines: ls } of drawn) {
    if (cutOff.has(e) || e.closest('svg')) continue;
    for (const c of clips) {
      if (c.n === e || !(hides(c.cs.overflowX) || hides(c.cs.overflowY)) || meant(c.cs) || moving(e, c.n)) continue;
      const only = { ...c, x: hides(c.cs.overflowX), y: hides(c.cs.overflowY) };
      let lostX = 0, lostY = 0, shown = false, w = 0, h = 0;
      for (const { r } of ls) {
        const v = cutTo(r, only);
        w = Math.max(w, r.right - r.left); h = Math.max(h, r.bottom - r.top);
        if (empty(v)) continue;
        shown = true;
        lostX = Math.max(lostX, (r.right - r.left) - (v.right - v.left));
        lostY = Math.max(lostY, (r.bottom - r.top) - (v.bottom - v.top));
      }
      if (shown && (lostX >= 4 || lostY >= 4)) {
        cutOff.add(e);
        clipped.push({ text: short(e.textContent), path: path(e), cut: lostX >= 4 && lostY >= 4 ? 'both' : lostX >= 4 ? 'width' : 'height',
                       box: `${c.n.clientWidth}x${c.n.clientHeight}`, content: `${Math.round(w)}x${Math.round(h)}` });
        break;
      }
    }
  }

  // ---- text drawn outside the box a person sees it in (the nearest with a border, a background or a shadow: a card,
  // a button), where nothing clips it, by at least 4 px and half its font size: it runs out of its card (FlockTab1:
  // "CLOSED", a flex item whose own box overflowed with it, into the next card). Text over no drawn box is left to the
  // overlap test.
  const spilled = [];
  const drawnBox = (n) => {
    const cs = getComputedStyle(n), bg = rgba(cs.backgroundColor);
    return (bg && bg[3] > 0) || cs.backgroundImage !== 'none' || cs.boxShadow !== 'none' ||
      ['Top', 'Right', 'Bottom', 'Left'].some((s) => parseFloat(cs[`border${s}Width`]) > 0 && (rgba(cs[`border${s}Color`]) || [0, 0, 0, 0])[3] > 0);
  };
  const boxOf = (e) => { for (let n = e; n && n !== document.body && n !== document.documentElement; n = n.parentElement) if (drawnBox(n)) return n; return null; };
  for (const { e, lines: ls } of drawn) {
    if (cutOff.has(e) || e.closest('svg')) continue;
    const box = boxOf(e);
    if (!box) continue;
    const bs = getComputedStyle(box);
    if (bs.overflowX !== 'visible' || bs.overflowY !== 'visible') continue;
    const b = box.getBoundingClientRect(), need = Math.max(4, parseFloat(getComputedStyle(e).fontSize) / 2);
    let worst = null;
    for (const { v } of ls) {
      if (empty(v)) continue;
      for (const [side, by] of [['right', v.right - b.right], ['left', b.left - v.left], ['bottom', v.bottom - b.bottom], ['top', b.top - v.top]]) {
        if (by >= need && (!worst || by > worst.by)) worst = { side, by: Math.round(by), v };
      }
    }
    if (worst && spilled.length < 200) {
      const xs = ls.map((l) => l.r), ink = { w: Math.max(...xs.map((r) => r.right)) - Math.min(...xs.map((r) => r.left)), h: Math.max(...xs.map((r) => r.bottom)) - Math.min(...xs.map((r) => r.top)) };
      spilled.push({ text: short(e.textContent), path: path(e), side: worst.side, by: worst.by,
                     box: `${Math.round(b.width)}x${Math.round(b.height)}`, content: `${Math.round(ink.w)}x${Math.round(ink.h)}` });
    }
  }

  // ---- texts drawn over another text (neither inside the other), line by line as shown, by at least 4 px each way,
  // where a person sees both: at the overlap's middle, nothing opaque lies between the two. SideGame1, 5 Oct: a title
  // screen's text under an opaque full-screen splash was reported. The hit test needs the point on screen; a pair off
  // screen, or one that takes no hits (pointer-events: none), keeps the line test alone.
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
  const shown = drawn.slice(0, 600).map(({ e, lines: ls }) => ({ e, ls: ls.map((l) => l.v).filter((v) => !empty(v)) }))
    .filter((x) => x.ls.length);
  const meet = (a, b) => {
    for (const p of a.ls) for (const q of b.ls) {
      const left = Math.max(p.left, q.left), top = Math.max(p.top, q.top);
      const w = Math.min(p.right, q.right) - left, h = Math.min(p.bottom, q.bottom) - top;
      if (w >= 4 && h >= 4 && !covered(a.e, b.e, left + w / 2, top + h / 2)) return `${Math.round(w)}x${Math.round(h)}`;
    }
    return null;
  };
  for (let i = 0; i < shown.length && overlaps.length < 10; i++) {
    for (let j = i + 1; j < shown.length && overlaps.length < 10; j++) {
      const a = shown[i], b = shown[j];
      if (a.e.contains(b.e) || b.e.contains(a.e)) continue;
      const area = meet(a, b);
      if (area) overlaps.push({ a: short(a.e.textContent), b: short(b.e.textContent), aPath: path(a.e), bPath: path(b.e), area });
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
    spilled: spilled.slice(0, 20), spilled_count: spilled.length,
    overlaps,
    dialog,
    styles,
  };
})()
