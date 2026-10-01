// QAJev's look at a web-page game (evaluated in the page for every observe). Generic part: frame rate and frame
// time from requestAnimationFrame, JS heap, the visible buttons as click actions and the visible text. A game's
// adapter (window.__qajevAdapter, bridges/web/adapters/<game>.js) then says which screen is up, the game's own
// actions (keys too) and its state. Read-only: nothing here changes the page.
(() => {
  if (!window.__qajevMeter) {
    const m = { frames: [], last: performance.now() };
    const tick = (now) => {
      m.frames.push(now - m.last);
      m.last = now;
      if (m.frames.length > 120) m.frames.shift();
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
    window.__qajevMeter = m;
  }
  const m = window.__qajevMeter;
  const recent = m.frames.slice(-60);
  const avg = recent.length ? recent.reduce((a, b) => a + b, 0) / recent.length : 0;
  const shown = (e) => {
    if (!e.checkVisibility || !e.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
    const r = e.getBoundingClientRect();
    return r.width > 2 && r.height > 2 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
  };
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const actions = [];
  const seen = new Set();
  for (const e of document.querySelectorAll('button, [role="button"], [role="menuitem"], a[href]')) {
    if (!shown(e) || e.disabled || e.closest('[inert],[aria-hidden="true"]')) continue;
    const label = clean(e.getAttribute('aria-label') || e.innerText || e.title);
    if (!label || seen.has(label)) continue;
    const r = e.getBoundingClientRect();
    const x = r.x + r.width / 2, y = r.y + r.height / 2;
    if (!e.contains(document.elementFromPoint(x, y))) continue; // covered by something else
    seen.add(label);
    actions.push({ id: 'dom:' + actions.length + ':' + label.slice(0, 40), label: label.slice(0, 120), kind: 'click', x, y });
  }
  const text = clean(document.body && document.body.innerText).slice(0, 3000);
  const base = {
    ok: true, screen: '', texts: text ? [text] : [], actions, state: {},
    fps: avg ? Math.round(1000 / avg) : 0, frame: m.frames.length, paused: document.hidden,
    size: [innerWidth, innerHeight], url: location.href,
    perf: {
      frame_ms: Math.round(Math.max(...recent, 0) * 10) / 10, // the slowest recent frame
      memory_mb: performance.memory ? Math.round(performance.memory.usedJSHeapSize / 1048576 * 10) / 10 : 0,
    },
  };
  const a = window.__qajevAdapter;
  if (!a || !a.observe) return base;
  let extra;
  try { extra = a.observe(base) || {}; } catch (err) { return { ...base, adapter_error: String(err) }; }
  for (const key of Object.keys(extra)) {
    if (key === 'actions' && !extra.replaceActions) base.actions = [...extra.actions, ...base.actions];
    else if (key !== 'replaceActions') base[key] = extra[key];
  }
  return base;
})()
