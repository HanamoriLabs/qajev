/* Browser-only UX measurements. CDP, interaction and reporting live in Python. */
(function () {
  "use strict";
  if (window.__qajevUx) return;

  const CONTROL = 'a[href],button,input:not([type="hidden"]),select,textarea,summary,' +
    '[role="button"],[role="link"],[role="checkbox"],[role="radio"],[role="switch"],' +
    '[role="tab"],[role="menuitem"],[role="option"],[role="slider"],[role="combobox"],' +
    '[contenteditable=""],[contenteditable="true"]';
  const BUTTON = 'button,input[type="submit"],input[type="button"],[role="button"]';
  const TEXT_TYPES = new Set(["text", "search", "email", "url", "tel", "password", "number"]);
  const ALL_CHECKS = ["cover", "keyboard", "shift", "input-kind", "tap", "clip"];
  const ids = new WeakMap();
  const elements = new Map();
  const snapshots = new Map();
  const mains = new Map();
  const overlayNodes = new WeakSet();
  let nextId = 1;
  let nextSnapshot = 1;
  let overlay = null;
  let shift = null;
  let dismiss = null;
  let mutationVersion = 0;
  let lastMutation = performance.now();

  const clean = value => String(value || "").replace(/\s+/g, " ").trim();
  const round = value => Math.round(value);
  const area = r => Math.max(0, r.width) * Math.max(0, r.height);
  const css = el => getComputedStyle(el);
  const rect = r => ({x: r.x, y: r.y, width: r.width, height: r.height,
    left: r.x, top: r.y, right: r.x + r.width, bottom: r.y + r.height});
  const arrayRect = r => [r.x, r.y, r.width, r.height].map(round);
  const viewport = () => rect({x: 0, y: 0, width: innerWidth, height: innerHeight});
  const intersect = (a, b) => rect({x: Math.max(a.left, b.left), y: Math.max(a.top, b.top),
    width: Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left)),
    height: Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top))});
  const raf = () => new Promise(resolve => requestAnimationFrame(resolve));
  const frames = async () => { await raf(); await raf(); };
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

  function id(el) {
    if (!(el instanceof Element)) return null;
    if (!ids.has(el)) {
      const value = "u" + nextId++;
      ids.set(el, value);
      elements.set(value, typeof WeakRef === "function" ? new WeakRef(el) : el);
    }
    return ids.get(el);
  }

  function resolve(value) {
    if (value instanceof Element) return value;
    if (value && typeof value === "object") {
      if (Number.isInteger(value.jevNode)) return window.__jevFast?.nodes.get(value.jevNode) || null;
      if (Number.isFinite(value.x) && Number.isFinite(value.y)) return hitAt(value.x, value.y);
      return resolve(value.id || value.key || value.elementId);
    }
    if (typeof value !== "string") return null;
    const saved = elements.get(value);
    if (saved) return typeof saved.deref === "function" ? saved.deref() || null : saved;
    try { return document.querySelector(value); } catch (_) { return null; }
  }

  function isControl(el) {
    if (!(el instanceof Element) || /^(HTML|BODY)$/.test(el.tagName) || el.id === "root") return false;
    return el.matches(CONTROL) || (el.hasAttribute("tabindex") && el.tabIndex >= 0 && css(el).cursor === "pointer");
  }

  function controls() {
    const found = [];
    for (const el of document.querySelectorAll(CONTROL + ',[tabindex]')) {
      if (isControl(el)) found.push(el);
      // The extra entry tells callers that the 300-control scan was capped.
      if (found.length > 300) break;
    }
    return found;
  }

  // Inventory does not measure style or geometry. After a budget stop the
  // remaining tabindex candidates are explicitly unchecked, rather than
  // guessed to be non-controls from an unmeasured cursor style.
  function controlCandidates() {
    return Array.from(document.querySelectorAll(CONTROL + ',[tabindex]')).filter(el =>
      !/^(HTML|BODY)$/.test(el.tagName) && el.id !== "root" &&
      (el.matches(CONTROL) || el.tabIndex >= 0));
  }

  function controlAncestor(el) {
    for (let p = el; p; p = p.parentElement) if (isControl(p)) return p;
    return null;
  }

  function ignored(el, check, allowDisabled) {
    if (!(el instanceof Element) || !el.isConnected) return true;
    if (["none", "presentation"].includes(el.getAttribute("role"))) return true;
    for (let p = el; p; p = p.parentElement) {
      if (p.getAttribute("aria-hidden") === "true" || p.hasAttribute("inert")) return true;
      const ignores = (p.getAttribute("data-qa-ux-ignore") || "").split(/\s+/);
      if (ignores.includes("all") || ignores.includes(check)) return true;
      if (!allowDisabled && p.getAttribute("aria-disabled") === "true" && (p === el || isControl(p))) return true;
    }
    if (!allowDisabled && el.disabled === true) return true;
    return ["UX-COVER", "UX-SHIFT", "UX-TAP", "UX-CLIP"].includes(check) && css(el).pointerEvents === "none";
  }

  function paddingBox(el) {
    const b = el.getBoundingClientRect();
    const sx = el.offsetWidth ? b.width / el.offsetWidth : 1;
    const sy = el.offsetHeight ? b.height / el.offsetHeight : 1;
    return rect({x: b.x + el.clientLeft * sx, y: b.y + el.clientTop * sy,
      width: el.clientWidth * sx, height: el.clientHeight * sy});
  }

  function isVisible(el) {
    if (typeof el.checkVisibility === "function") {
      return el.checkVisibility({opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true});
    }
    for (let p = el; p; p = p.parentElement) {
      const s = css(p);
      if (s.display === "none" || s.visibility === "hidden" || s.visibility === "collapse" || Number(s.opacity) === 0) return false;
    }
    return true;
  }

  function visibleRect(el) {
    if (!el || !el.isConnected || !isVisible(el)) return rect({x: 0, y: 0, width: 0, height: 0});
    let r = rect(el.getBoundingClientRect());
    if (r.width < 1 || r.height < 1) return rect({x: r.x, y: r.y, width: 0, height: 0});
    if (css(el).position !== "fixed") {
      for (let p = el.parentElement; p; p = p.parentElement) {
        const s = css(p);
        const b = paddingBox(p);
        if (s.overflowX !== "visible") {
          const left = Math.max(r.left, b.left);
          r = rect({x: left, y: r.y, width: Math.max(0, Math.min(r.right, b.right) - left), height: r.height});
        }
        if (s.overflowY !== "visible") {
          const top = Math.max(r.top, b.top);
          r = rect({x: r.x, y: top, width: r.width, height: Math.max(0, Math.min(r.bottom, b.bottom) - top)});
        }
        if (s.position === "fixed") break;
      }
    }
    return intersect(r, viewport());
  }

  function ratio(el) {
    const total = area(rect(el.getBoundingClientRect()));
    return total ? Math.min(1, area(visibleRect(el)) / total) : 0;
  }

  const onScreen = el => ratio(el) >= 0.5;

  function nearLabel(el, metadataOnly) {
    for (let n = el, depth = 0; n && depth < 3; n = n.parentElement, depth++) {
      let previous = n.previousElementSibling;
      for (let j = 0; previous && j < 2; j++, previous = previous.previousElementSibling) {
        const text = clean(metadataOnly ? previous.textContent : previous.innerText);
        const containsControl = metadataOnly ? previous.matches(CONTROL + ',[tabindex]') ||
          Boolean(previous.querySelector(CONTROL + ',[tabindex]')) : isControl(previous) ||
          Array.from(previous.querySelectorAll(CONTROL + ',[tabindex]')).some(isControl);
        if (text.length >= 1 && text.length <= 60 && !containsControl) return text;
      }
    }
    return "";
  }

  function name(el, metadataOnly) {
    const labelled = clean((el.getAttribute("aria-labelledby") || "").split(/\s+/)
      .map(value => { const node = document.getElementById(value); return node ? metadataOnly ? node.textContent : node.innerText : ""; }).join(" "));
    const labels = el.labels ? clean(Array.from(el.labels).map(label => metadataOnly ? label.textContent : label.innerText).join(" ")) : "";
    const field = /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable;
    const image = el.querySelector("img[alt]");
    return (labelled || clean(el.getAttribute("aria-label")) || labels || (field ? nearLabel(el, metadataOnly) : "") ||
      clean(metadataOnly ? el.textContent : el.innerText) || (/^(button|submit|reset)$/i.test(el.type || "") ? clean(el.value) : "") ||
      clean(el.title) || (image ? clean(image.alt) : "")).slice(0, 60);
  }

  function fieldLabel(el, metadataOnly) {
    return name(el, metadataOnly) || clean(el.getAttribute("placeholder")) || clean(el.getAttribute("name") || el.id);
  }

  function desc(el, metadataOnly) {
    const role = el.getAttribute("role") || (el.tagName === "INPUT" ? "input[" + (el.getAttribute("type") || "text") + "]" : el.tagName.toLowerCase());
    const label = /^(INPUT|TEXTAREA)$/.test(el.tagName) || el.isContentEditable ? fieldLabel(el, metadataOnly) : name(el, metadataOnly);
    return role + (label ? ' "' + label + '"' : "");
  }

  function key(el) {
    const escape = value => typeof CSS !== "undefined" && CSS.escape ? CSS.escape(value) : value.replace(/[^\w-]/g, char => "\\" + char);
    if (el.hasAttribute("data-testid")) return '[data-testid="' + el.getAttribute("data-testid").replace(/[\\"\n\r]/g, char => "\\" + char) + '"]';
    if (el.id) return "#" + escape(el.id);
    const parts = [];
    for (let p = el; p && parts.length < 6; p = p.parentElement) {
      let nth = 1;
      for (let sibling = p.previousElementSibling; sibling; sibling = sibling.previousElementSibling) if (sibling.tagName === p.tagName) nth++;
      parts.unshift(p.tagName.toLowerCase() + ":nth-of-type(" + nth + ")");
    }
    return parts.join(" > ");
  }

  function uncheckedInfo(el) {
    return {key: key(el), desc: desc(el, true)};
  }

  function incomplete(output, check, reason, pending) {
    const unchecked = Array.from(new Set(pending)).map(uncheckedInfo);
    if (!unchecked.length) return;
    output.status = "incomplete";
    output.skipped.push({check, reason: "scan incomplete: " + reason, unchecked});
  }

  function info(el) {
    return el ? {id: id(el), key: key(el), desc: desc(el), name: name(el), label: fieldLabel(el), is_text_field: textField(el),
      center_clear: (() => { const r = visibleRect(el), hit = hitAt(r.x + r.width / 2, r.y + r.height / 2); return composedContains(el, hit); })(),
      rect: arrayRect(rect(el.getBoundingClientRect())), visible_rect: arrayRect(visibleRect(el)), ratio: ratio(el)} : null;
  }

  function finding(check, el, message, data, extras) {
    return {check, severity: "advisory", url: location.href, element: info(el), message, data: data || {},
      mark: Object.assign({elementId: id(el)}, extras || {})};
  }

  function hitAt(x, y) {
    let hit = document.elementFromPoint(x, y);
    const seen = new Set();
    while (hit && hit.shadowRoot && typeof hit.shadowRoot.elementFromPoint === "function" && !seen.has(hit)) {
      seen.add(hit);
      const inside = hit.shadowRoot.elementFromPoint(x, y);
      if (!inside || inside === hit) break;
      hit = inside;
    }
    return hit;
  }

  function composedContains(el, hit) {
    for (let p = hit; p; p = p.parentElement || (p.getRootNode().host || null)) if (p === el) return true;
    return false;
  }

  function covering(el) {
    const v = visibleRect(el);
    if (!area(v)) return null;
    const points = [[0.5, 0.5], [0.2, 0.2], [0.8, 0.2], [0.2, 0.8], [0.8, 0.8]];
    const covered = [];
    for (let n = 0; n < points.length; n++) {
      const hit = hitAt(v.x + v.width * points[n][0], v.y + v.height * points[n][1]);
      let fine = hit && composedContains(el, hit);
      if (!fine && hit && el.labels) fine = Array.from(el.labels).some(label => composedContains(label, hit));
      if (!fine && hit && composedContains(hit, el) && css(el).pointerEvents === "none") fine = true;
      if (!fine) covered.push({hit, center: n === 0});
    }
    if (!covered.some(point => point.center) && covered.length < 2) return null;
    const representative = covered.find(point => point.center) || covered[0];
    return representative.hit ? {cover: representative.hit, count: covered.length} : null;
  }

  function coverBranch(el, cover) {
    const ancestors = new Set();
    for (let p = el; p; p = p.parentElement) ancestors.add(p);
    let child = cover;
    for (let p = cover; p; p = p.parentElement) {
      if (ancestors.has(p)) return child;
      child = p;
    }
    return cover;
  }

  function skipCover(el, cover) {
    const branch = coverBranch(el, cover);
    if (area(intersect(rect(branch.getBoundingClientRect()), viewport())) >= 0.8 * innerWidth * innerHeight) return true;
    for (let p = cover; p; p = p.parentElement) {
      if (["status", "alert"].includes(p.getAttribute("role")) || p.hasAttribute("aria-live")) return true;
      if (p === branch) break;
    }
    return false;
  }

  function saveScroll() {
    const result = [];
    for (const el of document.querySelectorAll("*")) {
      if (el === overlay || el.scrollTop || el.scrollLeft || el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1) {
        result.push([el, el.scrollLeft, el.scrollTop]);
      }
    }
    return result;
  }

  function restoreScroll(saved) {
    for (const [el, x, y] of saved) {
      if (el.isConnected && (el.scrollLeft !== x || el.scrollTop !== y)) el.scrollTo({left: x, top: y, behavior: "instant"});
    }
  }

  function snapshot() {
    const token = "s" + nextSnapshot++;
    const active = document.activeElement;
    let selection = null;
    try { if (active && typeof active.selectionStart === "number") selection = [active.selectionStart, active.selectionEnd, active.selectionDirection]; } catch (_) { /* Some input kinds have no selection API. */ }
    snapshots.set(token, {scroll: saveScroll(), active, selection});
    if (snapshots.size > 20) snapshots.delete(snapshots.keys().next().value);
    return token;
  }

  function restore(token) {
    const saved = snapshots.get(token);
    if (!saved) return false;
    snapshots.delete(token);
    if (saved.active && saved.active !== document.body && saved.active !== document.documentElement && saved.active.isConnected && typeof saved.active.focus === "function") {
      saved.active.focus({preventScroll: true});
      if (saved.selection) {
        try { saved.active.setSelectionRange(...saved.selection); } catch (_) { /* A page may have changed the input kind. */ }
      }
    } else if (document.activeElement && typeof document.activeElement.blur === "function") document.activeElement.blur();
    restoreScroll(saved.scroll);
    return true;
  }

  function coverFinding(el, result) {
    const cover = result.cover;
    const branch = coverBranch(el, cover);
    let position = "in flow";
    for (let p = cover; p; p = p.parentElement) {
      if (["fixed", "sticky", "absolute"].includes(css(p).position)) { position = css(p).position; break; }
      if (p === branch) break;
    }
    const b = rect(cover.getBoundingClientRect());
    return finding("UX-COVER", el, desc(el) + " is covered by " + desc(cover) + " (" + position + ", " +
      round(b.width) + "x" + round(b.height) + " px) at " + result.count + " of 5 tap points.",
    {points_covered: result.count, points_total: 5, cover: Object.assign(info(cover), {position, z_index: css(cover).zIndex})}, {coverId: id(cover)});
  }

  function tapFinding(el, opts) {
    if ((el.tagName === "A" || el.getAttribute("role") === "link") && css(el).display === "inline" &&
        el.parentElement && Array.from(el.parentElement.childNodes).some(node => node.nodeType === Node.TEXT_NODE && clean(node.textContent))) return null;
    let r = rect(el.getBoundingClientRect());
    if (el.tagName === "INPUT" && ["checkbox", "radio"].includes(el.type) && el.labels) {
      for (const label of el.labels) {
        const b = rect(label.getBoundingClientRect());
        const x = Math.min(r.x, b.x), y = Math.min(r.y, b.y);
        r = rect({x, y, width: Math.max(r.right, b.right) - x, height: Math.max(r.bottom, b.bottom) - y});
      }
    }
    const minimum = numberOpt(opts, "ux_tap_min_px", "tap_min_px", 44);
    if (r.width >= minimum - 0.5 && r.height >= minimum - 0.5) return null;
    return finding("UX-TAP", el, desc(el) + " is " + round(r.width) + "x" + round(r.height) +
      " px. Make the tap area at least " + minimum + "x" + minimum + " px.", {width: r.width, height: r.height, minimum});
  }

  function inputFindings(el, check) {
    if (el.tagName !== "INPUT" || !["text", "search"].includes(el.type) || el.readOnly ||
        (el.getAttribute("inputmode") || "").toLowerCase() === "none" || el.getAttribute("role") === "combobox" || el.hasAttribute("aria-haspopup")) return [];
    const label = fieldLabel(el), value = el.value.trim(), placeholder = el.placeholder || "";
    const dateWords = /\b(date|day|dob|birthday|birth date|expiry|expires|expiration|due|deadline)\b/i;
    const countDate = /\b(days|number of|how many|count|qty)\b/i;
    const dateValue = /^(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.\-]\d{1,2}[/.\-](?:\d{2}|\d{4}))$/;
    const dateHint = /\b(dd|mm|yyyy|yy)\s*[/.\-]/i;
    const results = [];
    if ((!check || check === "UX-INPUT-DATE") && !ignored(el, "UX-INPUT-DATE") && ((dateWords.test(label) && !countDate.test(label)) || dateValue.test(value) || dateHint.test(placeholder) || dateValue.test(placeholder))) {
      results.push(finding("UX-INPUT-DATE", el, desc(el) + " is a text box for a date" + (value ? ' (it holds "' + value + '")' : "") +
        '. Use a date picker (input type="date" or a picker).', {value, placeholder}));
    }
    const mode = (el.getAttribute("inputmode") || "").toLowerCase();
    const search = el.type === "search" || el.getAttribute("role") === "searchbox" || /\bsearch\b/i.test(label);
    const notNumber = /\b(phone|tel|mobile|zip|postcode|post code|code|id|pin|otp|serial|cert|card number|iban|account)\b/i;
    const money = /\b(price|fee|cost|amount|total|rm|myr|usd|sgd|pay|payment|deposit|budget|offer|discount|tax|tip)\b|[$€£¥]/i.test(label);
    const count = /\b(qty|quantity|count|number of|how many|tables|stock|copies|pieces|pcs|age|year)\b/i.test(label);
    if ((!check || check === "UX-INPUT-NUMBER") && !ignored(el, "UX-INPUT-NUMBER") && !search && !["numeric", "decimal", "tel"].includes(mode) && !notNumber.test(label) &&
        (money || count || /^-?\d+([.,]\d+)?$/.test(value))) {
      const inputmode = money ? "decimal" : "numeric", keyboardType = money ? "decimal-pad" : "number-pad";
      results.push(finding("UX-INPUT-NUMBER", el, desc(el) + " is a " + (money ? "money" : "number") +
        ' field but opens the full keyboard. Add inputmode="' + inputmode + '" (React Native: keyboardType="' + keyboardType + '").',
      {kind: money ? "money" : "number", inputmode, value}));
    }
    return results;
  }

  function ellipsis(el) {
    const s = css(el);
    return s.textOverflow === "ellipsis" || (s.webkitLineClamp && s.webkitLineClamp !== "none" && s.webkitLineClamp !== "0");
  }

  function clipFinding(el) {
    if (/^(INPUT|TEXTAREA|SELECT|SCRIPT|STYLE|NOSCRIPT)$/.test(el.tagName)) return null;
    let block = null;
    for (let p = el, depth = 0; p && depth <= 2; p = p.parentElement, depth++) if (ellipsis(p)) { block = p; break; }
    if (block) {
      if (ignored(block, "UX-CLIP")) return null;
      const s = css(block), control = controlAncestor(block);
      const primary = control || block.matches('h1,h2,h3,h4,h5,h6,[role="heading"],label') || Number.parseInt(s.fontWeight, 10) >= 600 || Number.parseFloat(s.fontSize) >= 17;
      const cutX = block.scrollWidth > block.clientWidth + 1;
      const cutY = s.webkitLineClamp && s.webkitLineClamp !== "none" && s.webkitLineClamp !== "0" && block.scrollHeight > block.clientHeight + 1;
      if (!primary || (!cutX && !cutY)) return null;
      const shown = round(Math.min(1, cutX ? block.clientWidth / block.scrollWidth : block.clientHeight / block.scrollHeight) * 100);
      const full = clean(block.innerText);
      return finding("UX-CLIP", control || block, "The label of " + desc(control || block) + ' is cut off with "…" (shows ' + shown + '% of "' + full + '").',
        {kind: "ellipsis", percent_visible: shown, text: full}, {elementId: id(block)});
    }
    let container = null;
    for (let p = el; p; p = p.parentElement) {
      const s = css(p);
      if (["auto", "scroll", "overlay"].includes(s.overflowX) || ["auto", "scroll", "overlay"].includes(s.overflowY)) return null;
      if (ellipsis(p)) return null;
      if (["hidden", "clip"].includes(s.overflowX) || ["hidden", "clip"].includes(s.overflowY)) { container = p; break; }
    }
    if (!container || ignored(container, "UX-CLIP")) return null;
    const b = paddingBox(container), s = css(container);
    if (b.width <= 2 || b.height <= 2) return null;
    const range = document.createRange();
    range.selectNodeContents(el);
    const t = rect(range.getBoundingClientRect());
    if (!area(t)) return null;
    let clipBox = rect(t);
    if (["hidden", "clip"].includes(s.overflowX)) clipBox = rect({x: b.x, y: clipBox.y, width: b.width, height: clipBox.height});
    if (["hidden", "clip"].includes(s.overflowY)) clipBox = rect({x: clipBox.x, y: b.y, width: clipBox.width, height: b.height});
    const visible = area(intersect(t, clipBox)) / area(t);
    const hidden = Math.max(["hidden", "clip"].includes(s.overflowX) ? Math.max(b.left - t.left, t.right - b.right, 0) : 0,
      ["hidden", "clip"].includes(s.overflowY) ? Math.max(b.top - t.top, t.bottom - b.bottom, 0) : 0);
    if (hidden <= 2 || visible <= 0 || visible >= 0.98) return null;
    const text = clean(el.innerText || el.textContent);
    return finding("UX-CLIP", container, 'Text "' + text + '" in ' + desc(container) + " is cut off (" + round(hidden) + " px hidden).",
      {kind: "hard-clip", hidden_px: hidden, percent_visible: round(visible * 100), text}, {elementId: id(el)});
  }

  function numberOpt(opts, name, alias, fallback) {
    const value = opts[name] === undefined ? opts[alias] : opts[name];
    return typeof value === "number" && Number.isFinite(value) ? value : fallback;
  }

  function checks(opts) {
    const chosen = opts.ux_checks || opts.checks || ALL_CHECKS;
    return new Set(Array.isArray(chosen) ? chosen : ALL_CHECKS);
  }

  async function scanStatic(opts) {
    opts = opts || {};
    const output = {findings: [], skipped: [], notes: []};
    let spent = 0, tick = performance.now(), timedOut = false;
    const enabled = checks(opts), all = controlCandidates(), candidates = [], counts = new Map(), extra = new Map();
    const cap = Math.max(1, Math.floor(numberOpt(opts, "ux_max_findings", "max_findings", 10)));
    const pending = new Map();
    if (enabled.has("input-kind")) {
      const inputs = all.filter(el => el.tagName === "INPUT" && ["text", "search"].includes(el.type) && !el.readOnly &&
        (el.getAttribute("inputmode") || "").toLowerCase() !== "none" && el.getAttribute("role") !== "combobox" && !el.hasAttribute("aria-haspopup"));
      pending.set("UX-INPUT-DATE", new Set(inputs));
      pending.set("UX-INPUT-NUMBER", new Set(inputs));
    }
    if (enabled.has("tap")) pending.set("UX-TAP", new Set(all));
    if (enabled.has("cover")) pending.set("UX-COVER", new Set(all));
    const texts = enabled.has("clip") ? Array.from(document.querySelectorAll("*")).filter(el =>
      !/^(INPUT|TEXTAREA|SELECT|SCRIPT|STYLE|NOSCRIPT)$/.test(el.tagName) &&
      Array.from(el.childNodes).some(node => node.nodeType === Node.TEXT_NODE && clean(node.textContent))) : [];
    if (enabled.has("clip")) pending.set("UX-CLIP", new Set(texts));
    let controlCount = 0, controlCapped = false, textCapped = false;
    function budget() {
      spent += performance.now() - tick; tick = performance.now();
      if (spent <= 200) return true;
      timedOut = true; return false;
    }
    function add(item) {
      if (!item) return;
      const count = counts.get(item.check) || 0;
      counts.set(item.check, count + 1);
      if (count < cap) output.findings.push(item);
      else extra.set(item.check, (extra.get(item.check) || 0) + 1);
    }
    for (const el of all) {
      if (!budget()) break;
      if (controlCount >= 300) { controlCapped = true; break; }
      if (!isControl(el)) {
        for (const [check, waiting] of pending) if (check !== "UX-CLIP") waiting.delete(el);
        continue;
      }
      controlCount++;
      for (const check of ["UX-INPUT-DATE", "UX-INPUT-NUMBER", "UX-TAP", "UX-COVER"]) {
        const waiting = pending.get(check);
        if (!waiting || !waiting.has(el)) continue;
        if (!budget()) break;
        if (check === "UX-INPUT-DATE" || check === "UX-INPUT-NUMBER") {
          inputFindings(el, check).forEach(add);
          waiting.delete(el);
        } else if (ignored(el, check) || !onScreen(el)) waiting.delete(el);
        else if (check === "UX-TAP") { add(tapFinding(el, opts)); waiting.delete(el); }
        else {
          const covered = covering(el);
          if (covered && !skipCover(el, covered.cover)) candidates.push(el);
          else waiting.delete(el);
        }
      }
      if (timedOut) break;
    }
    if (enabled.has("clip") && !timedOut) {
      const seen = new Set();
      let textCount = 0;
      for (const el of texts) {
        if (!budget()) break;
        if (textCount++ >= 300) { textCapped = true; break; }
        if (ignored(el, "UX-CLIP") || !onScreen(el)) { pending.get("UX-CLIP").delete(el); continue; }
        const item = clipFinding(el);
        if (item && !seen.has(item.element.key + "|" + item.data.kind)) { seen.add(item.element.key + "|" + item.data.kind); add(item); }
        pending.get("UX-CLIP").delete(el);
      }
    }
    // Time waiting for scroll/animation is not JavaScript scan CPU time. The
    // delayed recheck is shared by all candidate controls, rather than per one.
    const persistent = [];
    for (const el of candidates) {
      if (timedOut || !budget()) break;
      if (!el.isConnected) { pending.get("UX-COVER").delete(el); continue; }
      const saved = saveScroll();
      try {
        el.scrollIntoView({block: "center", inline: "nearest", behavior: "instant"});
        if (!budget()) break;
        await frames();
        tick = performance.now();
        const covered = covering(el);
        if (covered && !skipCover(el, covered.cover)) persistent.push(el);
        else pending.get("UX-COVER").delete(el);
      } finally { restoreScroll(saved); }
      if (!budget()) break;
    }
    if (persistent.length && !timedOut && budget()) {
      await delay(1000);
      tick = performance.now();
      for (const el of persistent) {
        if (!budget()) break;
        if (el.isConnected && !ignored(el, "UX-COVER") && onScreen(el)) {
          const covered = covering(el);
          if (covered && !skipCover(el, covered.cover)) add(coverFinding(el, covered));
        }
        pending.get("UX-COVER").delete(el);
      }
    }
    for (const [check, waiting] of pending) {
      const reason = timedOut ? "200 ms measurement budget exhausted" : check === "UX-CLIP" && textCapped ?
        "300 text-element cap reached" : controlCapped ? "300 control cap reached" : "measurement was not completed";
      incomplete(output, check, reason, waiting);
    }
    for (const [check, count] of extra) output.skipped.push({check, reason: "and " + count + " more", count});
    return output;
  }

  function textField(el) {
    if (!isControl(el) || el.readOnly || (el.getAttribute("inputmode") || "").toLowerCase() === "none") return false;
    return el.tagName === "TEXTAREA" || el.isContentEditable || (el.tagName === "INPUT" && TEXT_TYPES.has(el.type));
  }

  function fields() {
    return controls().slice(0, 300).filter(el => textField(el) && !ignored(el, "UX-KEYBOARD") && onScreen(el)).map(info);
  }

  function mainButton(field) {
    const buttons = controls().slice(0, 300).filter(el => el.matches(BUTTON) && !ignored(el, "UX-KEYBOARD-MAIN", true) && onScreen(el));
    const explicit = buttons.find(el => el.hasAttribute("data-qa-main"));
    if (explicit) return explicit;
    const form = field.closest("form");
    if (form) {
      const submit = buttons.filter(el => el.form === form && ((el.tagName === "BUTTON" && el.type === "submit") || (el.tagName === "INPUT" && el.type === "submit")));
      if (submit.length) return submit[submit.length - 1];
    }
    const named = buttons.filter(el => /^(save|done|submit|continue|next|create|add|confirm|send|apply|update|finish|start|sign in|log in|sign up|pay|buy)\b/i.test(name(el)));
    named.sort((a, b) => area(rect(b.getBoundingClientRect())) - area(rect(a.getBoundingClientRect())) || b.getBoundingClientRect().top - a.getBoundingClientRect().top);
    return named[0] || null;
  }

  function fingerContainers(el) {
    const result = [];
    const root = document.scrollingElement;
    for (let p = el.parentElement; p; p = p.parentElement) {
      if (p !== root && p !== document.body && p !== document.documentElement && ["auto", "scroll", "overlay"].includes(css(p).overflowY) && p.scrollHeight > p.clientHeight + 1) result.push(p);
    }
    const rootBlocked = [document.documentElement, document.body].filter(Boolean).some(p => ["hidden", "clip"].includes(css(p).overflowY));
    if (!rootBlocked && root && root.scrollHeight > root.clientHeight + 1) result.push(root);
    return result;
  }

  function scrollNeed(el) {
    const r = el.getBoundingClientRect();
    if (r.bottom > innerHeight) return r.bottom - innerHeight + 8;
    if (r.top < 0) return r.top - 8;
    // A scroll container may clip it before the viewport edge.
    const v = visibleRect(el);
    if (v.bottom < r.bottom) return r.bottom - v.bottom + 8;
    if (v.top > r.top) return r.top - v.top - 8;
    return 0;
  }

  function reachableMain(el) {
    if (!el) return {reachable: null, ratio: null, containers: []};
    const containers = fingerContainers(el), saved = containers.map(p => [p, p.scrollLeft, p.scrollTop]);
    let achieved;
    try {
      for (const container of containers) {
        const need = scrollNeed(el);
        if (!need) break;
        const room = need > 0 ? container.scrollHeight - container.clientHeight - container.scrollTop : container.scrollTop;
        container.scrollTop += Math.sign(need) * Math.min(Math.abs(need), Math.max(0, room));
        if (ratio(el) >= 0.9) break;
      }
      achieved = ratio(el);
    } finally { restoreScroll(saved); }
    return {reachable: achieved >= 0.9, ratio: achieved, containers: containers.map(id)};
  }

  function keyboardInfo(fieldId, opts) {
    opts = opts || {};
    const field = resolve(fieldId), output = {findings: [], skipped: [], notes: []};
    if (!field || !textField(field)) { output.skipped.push({check: "UX-KEYBOARD", reason: "field is no longer a text field"}); return output; }
    if (["UX-KEYBOARD-FOCUS", "UX-KEYBOARD-MAIN", "UX-KEYBOARD-DISMISS"].every(check => ignored(field, check))) {
      output.skipped.push({check: "UX-KEYBOARD", reason: "field is ignored"}); return output;
    }
    const identity = id(field);
    if (opts.resetMain || !mains.has(identity)) mains.set(identity, mainButton(field));
    const main = opts.mainId ? resolve(opts.mainId) : mains.get(identity);
    const active = document.activeElement;
    const fieldRatio = ratio(field), mainRatio = main && main.isConnected ? ratio(main) : null;
    const mode = opts.ux_keyboard_main || opts.keyboard_main || "reachable";
    const mainIgnored = ignored(field, "UX-KEYBOARD-MAIN") || (main && ignored(main, "UX-KEYBOARD-MAIN", true));
    const reached = main && main.isConnected && !mainIgnored && mode !== "visible" ? reachableMain(main) : {reachable: mainRatio === null ? null : mainRatio >= 0.9, ratio: mainRatio, containers: []};
    const done = controls().slice(0, 300).find(el => !ignored(el, "UX-KEYBOARD-DISMISS") && onScreen(el) &&
      (/^(done|hide keyboard|close keyboard|dismiss keyboard)$/i.test(name(el)) || el.hasAttribute("data-qa-dismiss-keyboard")));
    const hint = (field.getAttribute("enterkeyhint") || "").toLowerCase();
    Object.assign(output, {field: info(field), main: main && main.isConnected ? info(main) : null,
      mainId: main && main.isConnected ? id(main) : null, field_ratio: fieldRatio, main_ratio: mainRatio,
      reachable_main: reached.reachable, reachable_ratio: reached.ratio, scroll_containers: reached.containers,
      active_id: id(active), active_is_field: active === field || composedContains(field, active), active_is_text_field: textField(active),
      done: done ? info(done) : null, return_hint: hint, return_eligible: field.tagName !== "TEXTAREA" && ["done", "go", "next", "search", "send"].includes(hint),
      dismiss_ignored: ignored(field, "UX-KEYBOARD-DISMISS"),
      viewport: {width: innerWidth, height: innerHeight}});
    if (innerWidth > 500) { output.skipped.push({check: "UX-KEYBOARD", reason: "viewport is not a phone (" + innerWidth + " px wide)"}); return output; }
    if (opts.phase !== "open") return output;
    if (!ignored(field, "UX-KEYBOARD-FOCUS") && fieldRatio < 0.9) output.findings.push(finding("UX-KEYBOARD-FOCUS", field,
      "With the keyboard open, " + desc(field) + " is hidden while you type in it (" + round(fieldRatio * 100) + "% visible).",
      {percent_visible: round(fieldRatio * 100)}, {wholeViewport: true}));
    if (main && main.isConnected && main !== field && !ignored(field, "UX-KEYBOARD-MAIN") && !ignored(main, "UX-KEYBOARD-MAIN", true) && !reached.reachable) {
      const message = "With the keyboard open on " + fieldLabel(field) + ", the main button " + desc(main) +
        (mode === "visible" ? " is hidden while typing" : " is hidden and cannot be scrolled into view") +
        " (button top " + round(main.getBoundingClientRect().top) + " px, visible area ends at " + innerHeight + " px).";
      output.findings.push(finding("UX-KEYBOARD-MAIN", main, message,
        {field: info(field), percent_visible: round(mainRatio * 100), main_mode: mode, reachable: reached.reachable},
        {elementId: identity, wholeViewport: true, bottomLabel: name(main) + " is below here"}));
    }
    if (opts.dismissFailed && !ignored(field, "UX-KEYBOARD-DISMISS")) output.findings.push(finding("UX-KEYBOARD-DISMISS", field,
      "No way to close the keyboard on " + fieldLabel(field) + ": no Done button, Return does not close it, and tapping outside does not close it.",
      {field: info(field)}, {wholeViewport: true}));
    return output;
  }

  function neutralPoint(fieldId) {
    const field = resolve(fieldId);
    const targets = controls().slice(0, 300).filter(el => onScreen(el) && !ignored(el, "UX-KEYBOARD-DISMISS", true)).map(visibleRect);
    let best = null, bestDistance = -1;
    for (let y = 8; y <= innerHeight - 8; y += 16) for (let x = 8; x <= innerWidth - 8; x += 16) {
      const hit = hitAt(x, y);
      if (!hit || hit === field || (field && composedContains(field, hit))) continue;
      let qualifies = true;
      for (let p = hit; p; p = p.parentElement) {
        const ignores = (p.getAttribute("data-qa-ux-ignore") || "").split(/\s+/);
        if (isControl(p) || p.tagName === "LABEL" || ignores.includes("tap-outside") || ignores.includes("all")) { qualifies = false; break; }
      }
      if (!qualifies) continue;
      let nearest = Infinity;
      for (const target of targets) nearest = Math.min(nearest, Math.hypot(Math.max(target.left - x, 0, x - target.right), Math.max(target.top - y, 0, y - target.bottom)));
      if (nearest > bestDistance) { bestDistance = nearest; best = {x, y}; }
    }
    return best;
  }

  function dismissEnd() {
    if (!dismiss) return;
    window.removeEventListener("mousedown", dismiss.mouse, true);
    window.removeEventListener("submit", dismiss.submit, true);
    dismiss = null;
  }

  function dismissStart(opts) {
    dismissEnd(); opts = opts || {};
    dismiss = {mouse: event => event.preventDefault(), submit: event => event.preventDefault()};
    if (opts.mouse !== false && !opts.submit) window.addEventListener("mousedown", dismiss.mouse, true);
    if (opts.submit) window.addEventListener("submit", dismiss.submit, {capture: true, once: true});
    return true;
  }

  function contentPosition(el) {
    const r = rect(el.getBoundingClientRect());
    for (let p = el; p; p = p.parentElement) if (["fixed", "sticky"].includes(css(p).position)) return {x: r.x, y: r.y, scrollX: 0, scrollY: 0};
    let x = 0, y = 0;
    const root = document.scrollingElement;
    for (let p = el.parentElement; p; p = p.parentElement) {
      if (p === root || p.scrollWidth > p.clientWidth + 1 || p.scrollHeight > p.clientHeight + 1 || p.scrollLeft || p.scrollTop) { x += p.scrollLeft; y += p.scrollTop; }
    }
    return {x: r.x + x, y: r.y + y, scrollX: x, scrollY: y};
  }

  function scrollContainer(el) {
    for (let p = el.parentElement; p; p = p.parentElement) if (["auto", "scroll", "overlay", "hidden"].includes(css(p).overflowY) && p.scrollHeight > p.clientHeight + 1) return p;
    return document.scrollingElement;
  }

  function shiftBefore(targetKey) {
    const started = performance.now();
    const target = resolve(targetKey), tracked = [];
    const all = controlCandidates().filter(el => !target || !(el === target || el.contains(target) || target.contains(el)));
    const pending = new Set(all);
    const output = {tracked: 0, url: location.href, target: target ? info(target) : null, skipped: []};
    let count = 0, reason = "200 ms before-action measurement budget exhausted";
    for (const el of all) {
      if (performance.now() - started > 200) break;
      if (count >= 300) { reason = "300 control cap reached before action"; break; }
      if (isControl(el)) {
        count++;
        if (!ignored(el, "UX-SHIFT") && onScreen(el)) tracked.push({el, before: rect(el.getBoundingClientRect()), position: contentPosition(el)});
      }
      pending.delete(el);
    }
    incomplete(output, "UX-SHIFT", reason, pending);
    shift = {url: location.href, tracked, added: new Set(), started: performance.now(), incomplete: output.skipped};
    output.tracked = tracked.length;
    return output;
  }

  function shiftAfter(opts) {
    opts = opts || {};
    const saved = shift; shift = null;
    const output = {findings: [], skipped: [], notes: []};
    if (!saved) return output;
    output.skipped.push(...saved.incomplete);
    if (saved.incomplete.length) output.status = "incomplete";
    if (saved.url !== location.href) { output.notes.push("screen changed"); return output; }
    const started = performance.now(), alive = [];
    const pending = new Set(saved.tracked.map(item => item.el));
    const minimum = numberOpt(opts, "ux_shift_px", "shift_px", 8), moved = [];
    for (const item of saved.tracked) {
      if (performance.now() - started > 200) break;
      if (item.el.isConnected && !ignored(item.el, "UX-SHIFT") && onScreen(item.el)) {
        alive.push(item);
        const position = contentPosition(item.el);
        const dx = position.x - item.position.x, dy = position.y - item.position.y;
        if (Math.max(Math.abs(dx), Math.abs(dy)) > minimum) moved.push(Object.assign({}, item, {dx, dy, position, after: rect(item.el.getBoundingClientRect())}));
      }
      pending.delete(item.el);
    }
    if (pending.size) {
      incomplete(output, "UX-SHIFT", "200 ms after-action measurement budget exhausted", pending);
      return output;
    }
    if (saved.tracked.length && alive.length < saved.tracked.length * 0.5) { output.notes.push("screen changed"); return output; }
    const groups = [];
    for (const item of moved) {
      const group = groups.find(g => Math.abs(g[0].dx - item.dx) <= 2 && Math.abs(g[0].dy - item.dy) <= 2);
      if (group) group.push(item); else groups.push([item]);
    }
    if (saved.tracked.length >= 4 && groups.some(group => group.length >= saved.tracked.length * 0.6)) { output.notes.push("screen transition"); return output; }
    const cap = Math.max(1, Math.floor(numberOpt(opts, "ux_max_findings", "max_findings", 10)));
    const pendingGroups = new Set(moved.map(item => item.el));
    let reportCount = 0, causeTimedOut = false;
    for (const group of groups.slice(0, cap)) {
      if (performance.now() - started > 200) { causeTimedOut = true; break; }
      const first = group[0], container = scrollContainer(first.el);
      const causes = [];
      for (const el of saved.added) {
        if (performance.now() - started > 200) { causeTimedOut = true; break; }
        if (el.isConnected && isVisible(el) && area(visibleRect(el)) &&
            el.getBoundingClientRect().bottom <= first.after.top + 1 && scrollContainer(el) === container) causes.push(el);
      }
      if (causeTimedOut) break;
      causes.sort((a, b) => area(rect(b.getBoundingClientRect())) - area(rect(a.getBoundingClientRect())));
      const cause = causes[0] || null;
      const vertical = Math.abs(first.dy) >= Math.abs(first.dx), distance = Math.abs(vertical ? first.dy : first.dx);
      const direction = vertical ? (first.dy >= 0 ? "down" : "up") : (first.dx >= 0 ? "right" : "left");
      const action = opts.action || "the action";
      const before = rect({x: first.position.x - first.dx - first.position.scrollX, y: first.position.y - first.dy - first.position.scrollY,
        width: first.before.width, height: first.before.height});
      const message = desc(first.el) + " moved " + round(distance) + " px " + direction + " after " + action + "." +
        (cause ? " Cause: a " + round(cause.getBoundingClientRect().height) + " px tall " + desc(cause) + " appeared above it." : " Content above it changed size.") +
        (group.length > 1 ? " And " + (group.length - 1) + " more." : "");
      output.findings.push(finding("UX-SHIFT", first.el, message,
        {dx: first.dx, dy: first.dy, before_rect: arrayRect(first.before), after_rect: arrayRect(first.after),
          settle_ms: opts.settle_ms === undefined ? round(performance.now() - saved.started) : opts.settle_ms,
          group_count: group.length, cause: info(cause)}, {beforeRect: arrayRect(before), causeId: id(cause)}));
      reportCount++;
      for (const item of group) pendingGroups.delete(item.el);
    }
    incomplete(output, "UX-SHIFT", causeTimedOut ? "200 ms cause/group measurement budget exhausted" :
      "finding cap reached with " + (groups.length - reportCount) + " groups remaining", pendingGroups);
    return output;
  }

  function isOverlay(node) {
    return node && (overlayNodes.has(node) || (node.getRootNode && node.getRootNode().host && overlayNodes.has(node.getRootNode().host)));
  }

  const observer = new MutationObserver(records => {
    let changed = false;
    for (const record of records) {
      if (isOverlay(record.target)) continue;
      const relevant = [...record.addedNodes, ...record.removedNodes].filter(node => !isOverlay(node));
      if (record.type === "childList" && !relevant.length) continue;
      changed = true;
      if (shift) for (const node of record.addedNodes) {
        if (!(node instanceof Element) || isOverlay(node)) continue;
        shift.added.add(node);
        for (const child of node.querySelectorAll("*")) shift.added.add(child);
      }
    }
    if (changed) { lastMutation = performance.now(); mutationVersion++; }
  });
  observer.observe(document, {subtree: true, childList: true, attributes: true, characterData: true});

  function settleInfo() {
    let running = 0;
    if (typeof document.getAnimations === "function") for (const animation of document.getAnimations()) {
      if (animation.playState !== "running" || !animation.effect) continue;
      const timing = animation.effect.getComputedTiming();
      if (Number.isFinite(timing.endTime)) running++;
    }
    return {lastMutation, quiet_ms: performance.now() - lastMutation, mutation_version: mutationVersion,
      finite_animations: running, now: performance.now()};
  }

  function unmark() {
    if (overlay) overlay.remove();
    overlay = null;
    return true;
  }

  function mark(spec) {
    unmark(); spec = spec || {};
    if (spec.mark) spec = spec.mark;
    const el = resolve(spec.elementId || spec.element || spec.id || spec.key);
    const cover = resolve(spec.coverId || spec.cover), cause = resolve(spec.causeId || spec.cause);
    overlay = document.createElement("div");
    overlayNodes.add(overlay);
    overlay.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647;contain:strict;";
    const shadow = overlay.attachShadow({mode: "closed"});
    function outline(r, color, dashed, width) {
      if (!r || !r.width || !r.height) return;
      const box = document.createElement("div");
      box.style.cssText = "position:absolute;box-sizing:border-box;pointer-events:none;left:" + r.x + "px;top:" + r.y +
        "px;width:" + r.width + "px;height:" + r.height + "px;border:" + width + "px " + (dashed ? "dashed " : "solid ") + color + ";";
      shadow.appendChild(box);
    }
    if (el) outline(rect(el.getBoundingClientRect()), "#e00000", false, 3);
    if (cover) outline(rect(cover.getBoundingClientRect()), "#ee8600", true, 2);
    if (cause) outline(rect(cause.getBoundingClientRect()), "#ee8600", true, 2);
    if (Array.isArray(spec.beforeRect)) outline(rect({x: spec.beforeRect[0], y: spec.beforeRect[1], width: spec.beforeRect[2], height: spec.beforeRect[3]}), "#e00000", true, 2);
    if (spec.bottomLabel) {
      const bar = document.createElement("div");
      bar.style.cssText = "position:absolute;bottom:0;left:0;right:0;border-bottom:4px solid #e00000;background:#fff;color:#a00000;font:bold 14px sans-serif;padding:5px;";
      bar.textContent = spec.bottomLabel;
      shadow.appendChild(bar);
    }
    (document.documentElement || document).appendChild(overlay);
    if (spec.wholeViewport || !el) return {x: 0, y: 0, width: innerWidth, height: innerHeight, scale: 1};
    const regions = [];
    if (Array.isArray(spec.beforeRect)) regions.push(intersect(rect({x: spec.beforeRect[0], y: spec.beforeRect[1],
      width: spec.beforeRect[2], height: spec.beforeRect[3]}), viewport()));
    for (const related of [cover, cause]) if (related) regions.push(visibleRect(related));
    let v = visibleRect(el);
    for (const r of regions) if (area(r) > 0) {
      const left = Math.min(v.left, r.left), top = Math.min(v.top, r.top);
      v = rect({x: left, y: top, width: Math.max(v.right, r.right) - left, height: Math.max(v.bottom, r.bottom) - top});
    }
    const width = Math.min(innerWidth, Math.max(160, v.width + 96));
    const height = Math.min(innerHeight, Math.max(120, v.height + 96));
    return {x: Math.max(0, Math.min(innerWidth - width, v.x + v.width / 2 - width / 2)),
      y: Math.max(0, Math.min(innerHeight - height, v.y + v.height / 2 - height / 2)), width, height, scale: 1};
  }

  window.__qajevUx = Object.freeze({scanStatic, shiftBefore, shiftAfter, keyboardInfo, neutralPoint, mark, unmark, fields,
    snapshot, restore, dismissStart, dismissEnd, settleInfo, state: settleInfo,
    scrollField: value => { const el = resolve(value); if (!el) return false; el.scrollIntoView({block: "nearest", inline: "nearest", behavior: "instant"}); return true; },
    focusField: value => { const el = resolve(value); if (!el || !textField(el)) return false; el.focus({preventScroll: true}); return document.activeElement === el || composedContains(el, document.activeElement); },
    element: value => { const el = resolve(value); return el ? info(el) : null; }});
})();
