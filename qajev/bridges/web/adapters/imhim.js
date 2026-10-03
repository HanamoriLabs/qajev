// QAJev adapter for I'M HIM! (its Electron desktop build). Which screen is up
// (from the game's own DOM overlays), the keys a player uses there, and the run's state: from the HUD in any
// build, and from window.__game plus the dev watchdog (window.__watch) in a dev-mode build. Read-only, except that
// speechSynthesis.speak is wrapped to count what the game reads aloud (it still speaks).
// Selectors and state paths: the game's src/ui/*.ts and src/game/Game.ts (surveyed 2026-09-30, main ec1d349+).
// SETTINGS › ACCESSIBILITY (a11y-plus, 42e6697): what is switched on, and what the transient aids showed since the
// session started. Captions (src/ui/captions.ts) and edge markers last a few seconds, so they are counted across
// looks; spoken menus (src/ui/narration.ts) are counted by wrapping speechSynthesis.speak, which still speaks.
const A11Y_CLASSES = ['high-contrast', 'font-readable', 'font-dyslexic', 'cue-shapes', 'reduce-motion', 'text-large',
  'text-xl'];
const A11Y_SETTINGS = ['assist', 'gameSpeed', 'qteTime', 'highContrast', 'captions', 'font', 'narration', 'textSize',
  'colourVision', 'reducedMotion'];
function a11y(g) {
  const acc = window.__qajevA11y || (window.__qajevA11y = {
    captionsSeen: 0, captionsLast: [], shown: new Set(), edgesSeen: 0, edgeLast: '', edgeText: ['', ''],
    spoken: 0, spokenLast: [], wrapped: false,
  });
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  // a caption line counts once when it appears (the stack is redrawn as lines fade, so not by element)
  const now = new Set([...document.querySelectorAll('.captions .cap-line')].map((e) => clean(e.innerText)).filter(Boolean));
  for (const t of now) {
    if (acc.shown.has(t)) continue;
    acc.captionsSeen += 1;
    acc.captionsLast = [...acc.captionsLast, t].slice(-3);
  }
  acc.shown = now;
  ['left', 'right'].forEach((side, i) => {
    const e = document.querySelector('.cap-edge.' + side);
    const t = e && e.classList.contains('on') ? clean(e.textContent) : '';
    if (t && t !== acc.edgeText[i]) { acc.edgesSeen += 1; acc.edgeLast = t; }
    acc.edgeText[i] = t;
  });
  const synth = window.speechSynthesis;
  if (synth && !acc.wrapped) {
    const speak = synth.speak.bind(synth);
    synth.speak = (u) => {
      acc.spoken += 1;
      acc.spokenLast = [...acc.spokenLast, clean(u && u.text)].slice(-3);
      return speak(u);
    };
    acc.wrapped = true;
  }
  // Only what happened: every state line is also text Jev reads, and a long screen tipped it into DONE before.
  const out = {};
  if (acc.captionsSeen) Object.assign(out, { captions_seen: acc.captionsSeen, captions_last: acc.captionsLast });
  if (acc.edgesSeen) Object.assign(out, { edges_seen: acc.edgesSeen, edge_last: acc.edgeLast });
  if (acc.spoken) Object.assign(out, { spoken_count: acc.spoken, spoken_last: acc.spokenLast });
  const classes = A11Y_CLASSES.filter((c) => document.documentElement.classList.contains(c));
  if (classes.length) out.a11y_classes = classes;
  try { // the game's saved settings (src/core/Settings.ts SETTINGS_KEY), any build
    const s = JSON.parse(localStorage.getItem('shinobi.settings.v1') || '{}');
    for (const k of A11Y_SETTINGS) if (k in s) out['setting_' + k] = s[k];
  } catch (err) { out.settings_error = String(err); }
  if (g && g.engine) { // dev build: what the engine applies
    if (g.engine.post && typeof g.engine.post.contrast === 'number') out.contrast = g.engine.post.contrast;
    if (typeof g.engine.speedScale === 'number') out.speed = g.engine.speedScale;
  }
  return out;
}

// The dev menu (dev builds only): each section's <select> options as "<section>: <option>" (current one marked) and
// its buttons as "<section>: <button>", e.g. "Boss: Fight", "Enemies: Kunai thrower", "Enemies: Spawn ahead".
function devMenuItems() {
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const out = [];
  for (const sec of document.querySelectorAll('.devmenu section')) {
    const h = sec.querySelector('h2');
    const title = h ? clean(h.textContent) : '';
    for (const s of sec.querySelectorAll('select')) {
      for (const o of s.options) {
        const k = (title ? title + ': ' : '') + clean(o.textContent);
        out.push({ kind: 'select', el: s, value: o.value, key: k, label: k + (s.value === o.value ? ' (current)' : '') });
      }
    }
    for (const b of sec.querySelectorAll('button')) {
      const k = (title ? title + ': ' : '') + clean(b.textContent);
      if (clean(b.textContent)) out.push({ kind: 'button', el: b, key: k, label: k });
    }
  }
  return out;
}

// Every option of every radio row in the open settings tab, scrolled into view or not (a hidden tab's rows are not
// visible at all): its row name, its own text, and the label Jev reads ("Vibration: OFF (current)").
function settingsOptions() {
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const out = [];
  for (const b of document.querySelectorAll('.settings-screen.on [role="radio"]')) {
    if (!(b.checkVisibility && b.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }))) continue;
    const group = b.closest('[role="radiogroup"]');
    const name = group && document.getElementById(group.getAttribute('aria-labelledby') || '');
    const row = name ? clean(name.innerText || name.textContent) : '';
    const opt = clean(b.innerText || b.textContent);
    const key = (row ? row + ': ' : '') + opt;
    out.push({ el: b, row, opt, key, label: key + (b.getAttribute('aria-checked') === 'true' ? ' (current)' : '') });
  }
  return out;
}

// Which line of the open conversation is on show: counted as its whole text (.talk-sr) changes, from 1 when a talk
// opens. A line repeated word for word counts once; that only makes the number lower, never wrong about progress.
function noteTalkLine(line) {
  const t = window.__qajevTalk || (window.__qajevTalk = { last: null, n: 0 });
  if (line !== t.last) { t.last = line; t.n += 1; }
  return t.n;
}

window.__qajevAdapter = {
  observe(base) {
    const $ = (sel) => document.querySelector(sel);
    const on = (sel) => { const e = $(sel); return !!(e && e.checkVisibility && e.checkVisibility({ checkOpacity: true })); };
    const text = (sel) => { const e = $(sel); return e ? String(e.innerText || '').replace(/\s+/g, ' ').trim() : ''; };
    const key = (id, label, k) => ({ id, label, kind: 'key', key: k });
    const g = window.__game;
    const keys = [];
    let screen = 'LOADING', decision = false, talkLine = 0, talkHint = '', ownPick = null;

    const title = $('.title-screen');
    const titleOn = title && title.classList.contains('on');
    const devMenu = $('.devmenu');
    if (devMenu && !devMenu.hidden) { // dev build: DEV TOOLS over the title or a run (src/dev/devMenu.ts)
      screen = 'DEV TOOLS';
      keys.push(key('close_dev_menu', 'Close the dev menu (Esc)', 'Escape'));
    } else if (on('#ui > .boot-splash:not(.out)')) {
      screen = 'LOGOS';
      keys.push(key('skip_logos', 'Skip the studio logos', 'Escape'));
    } else if (on('section.ecine.on')) {
      screen = 'ENDING';
      keys.push(key('skip_ending', 'Skip the ending cutscene', 'Escape'));
    } else if (on('.tut-dialog')) {
      screen = 'DOJO OFFER';
      // The game's JUST FIGHT and Esc both decline and go back to the title menu (Tutorial.offer on 9cf3bea); "Just
      // fight" read to Jev as starting a run, so a goal in the menu stopped here.
      keys.push(key('just_fight', 'No thanks: go to the main menu (skip the dojo)', 'Escape'),
        key('train_first', 'Train first (the dojo)', 'Enter'));
    } else if (on('.overlay.gameover.on')) {
      screen = 'GAME OVER';
    } else if (on('.levelup.on')) {
      screen = 'LEVEL UP';
      decision = true; // the game waits for the player's card pick
    } else if (on('.qte.on')) {
      screen = 'QTE';
      for (const [k, l] of [['a', 'left attack (A)'], ['d', 'right attack (D)'], ['w', 'up attack (W)'], ['s', 'down attack (S)'], ['Space', 'jump (Space)']]) keys.push(key('qte_' + k, 'QTE: ' + l, k));
    } else if (on('.hero-cv.on:not(.stamped)')) {
      screen = 'HERO CV';
    } else if (on('.stall.on')) {
      screen = on('.stall.on.verdict') ? 'STALL VERDICT' : 'STALL';
      if (screen === 'STALL VERDICT') keys.push(key('close_stall', 'Leave the stall', 'Enter'));
    } else if (on('.talk.on')) {
      screen = 'TALK';
      // Where the conversation is, and that it ends by itself: pressing Continue many times read to a decision
      // model as going nowhere (BLOCKED after 3-4 presses in the Hearing; a talk after a death read as a dead end).
      // Tested on the real models, 2 Oct: Clef 0.55 -> 0.93 on the after-death talk, Jev no longer stuck there.
      // Enter on a line still typing only finishes it (src/ui/DialogueScreen.ts next()), so the label says so.
      const typing = on('.talk.on .talk-box.typing');
      talkLine = noteTalkLine(text('.talk.on .talk-sr'));
      if (!on('.talk.on .talk-choice')) {
        keys.push(key('talk_next', typing ? 'Continue: finish this line (Enter)' : 'Continue: the next line (Enter)',
          'Enter'));
        talkHint = `Conversation, line ${talkLine}: Continue shows the next line; it closes by itself after the last one.`
          + (typing ? ' This line is still typing: Continue shows all of it.' : '');
      } else {
        decision = true;
        talkHint = `Conversation, line ${talkLine}: pick one of the replies.`;
      }
      keys.push(key('talk_close', 'Close the conversation (Esc)', 'Escape'));
    } else if (on('.settings-screen.on')) {
      screen = 'SETTINGS'; // "Esc when done" (its own hint); also its DONE button
      keys.push(key('close_settings', 'Close the settings (Esc)', 'Escape'));
    } else if (on('.controls-screen.on')) {
      screen = 'CONTROLS';
      keys.push(key('close_controls', 'Close the controls (Esc)', 'Escape'));
    } else if (on('.inv.on')) {
      screen = titleOn ? 'CASE FILE' : 'BAG'; // its open tab is state.bag_tab
      keys.push(key('close_bag', 'Close this screen', 'Escape'));
    } else if (on('.overlay.paused.on')) {
      screen = 'PAUSED';
      keys.push(key('resume', 'Resume the game (Esc)', 'Escape'));
    } else if (on('.outfits-screen.on')) {
      // Opened from the title, which stays .on underneath (inert): without this branch it read as TITLE MENU, and a
      // step that "closed" it passed on a screen check while it was still open (I'M HIM! QA, 2 Oct, cb9661c).
      screen = 'OUTFITS';
      keys.push(key('close_outfits', 'Close the outfits screen (Esc)', 'Escape'));
    } else if (on('.credits-screen.on')) {
      screen = 'CREDITS';
      keys.push(key('close_credits', 'Close the credits (Esc)', 'Escape'));
    } else if (titleOn) {
      const stage = title.dataset.stage || (title.classList.contains('quick') ? 'quick' : '');
      screen = { press: 'TITLE', menu: 'TITLE MENU', difficulty: 'DIFFICULTY', quick: 'TITLE' }[stage] || 'TITLE';
      // Named for where it leads: "Press Enter to start" next to a goal that began "From the main menu" read to Jev
      // as no way forward (BLOCKED 0.65 on 9cf3bea).
      if (stage === 'press' || stage === 'quick') keys.push(key('press_start', 'Go to the main menu (press Enter)', 'Enter'));
      if (stage === 'difficulty') keys.push(key('back', 'Back to the title menu', 'Escape'));
    } else if (on('.hud-root')) {
      screen = 'PLAYING';
      keys.push(key('pause', 'Pause the game', 'Escape'), key('bag', 'Open the bag (I)', 'i'),
        key('character', 'Open the character sheet (C)', 'c'), key('talents', 'Open the talents (T)', 't'));
      if (g && devMenu) keys.push(key('dev_menu', 'Open the dev menu (`): spawn foes, fight a boss', 'Backquote'));
    }

    // Controls a QA run must not use: Twitch sign-in, and anything that uploads.
    const deny = /\b(connect|disconnect|twitch|workshop|upload|log ?in|sign ?in)\b/i;
    // The title's own PRESS ANY KEY button does what press_start does; one way forward reads clearer to Jev.
    const dup = keys.some((k) => k.id === 'press_start') ? /^press any (key|button)$/i
      : keys.some((k) => k.id === 'just_fight') ? /^(just fight|train first)$/i : null;

    // SETTINGS: every ON/OFF row has buttons with the same text, and the generic look keeps only the first of each
    // label, so "OFF" for Vibration was not offered at all. Each option becomes "<row>: <option>" here.
    const options = [];
    const optionTexts = new Set();
    if (screen === 'SETTINGS') {
      // Rows below the fold of the tab's scrolling list are offered too (Assist mode, Game speed... on
      // ACCESSIBILITY): Jev picks them like any option, and act() scrolls one into view and clicks it in the page.
      for (const o of settingsOptions()) {
        optionTexts.add(o.opt);
        const r = o.el.getBoundingClientRect();
        const x = r.x + r.width / 2, y = r.y + r.height / 2;
        const onScreen = r.width > 2 && r.height > 2 && r.top >= 0 && r.bottom <= innerHeight
          && o.el.contains(document.elementFromPoint(x, y));
        const id = 'set:' + options.length + ':' + o.label.slice(0, 40);
        options.push(onScreen ? { id, label: o.label.slice(0, 120), kind: 'click', x, y }
          : { id, label: o.label.slice(0, 120), kind: 'adapter', op: 'set_option', match: o.key });
      }
    }
    if (screen === 'DEV TOOLS') {
      for (const it of devMenuItems()) {
        options.push({ id: 'dev:' + options.length + ':' + it.key.slice(0, 40), label: it.label.slice(0, 120),
          kind: 'adapter', op: 'dev_item', match: it.key });
      }
    }
    if (screen !== 'TALK') window.__qajevTalk = null; // the next conversation counts its lines from 1
    // The talk box's ✕ (aria-label "Close conversation") does what talk_close does: one way out reads clearer.
    const sameAsKey = screen === 'TALK' ? /^close conversation$/i : null;
    let actions = screen === 'DEV TOOLS' ? [...keys, ...options] : [...keys, ...options, ...base.actions.filter((a) => !deny.test(a.label) && !(dup && dup.test(a.label))
      && !(sameAsKey && sameAsKey.test(a.label)) && !optionTexts.has(a.label))];
    if (screen === 'DEV TOOLS') actions = actions.filter((a) => !deny.test(a.label));

    // The game's bot (dev build, ?autoplay) waits on every decision a player makes (level-up cards, dialogue
    // choices, the bag, talents, stalls, the game-over card...) and lists its options: those are Jev's to pick.
    const ap = window.__autoplay;
    const kinds = { levelup: 'LEVEL UP', dialogueChoice: 'TALK CHOICE', hire: 'HIRE', gameover: 'GAME OVER',
      bag: 'BAG', talents: 'TALENTS', stall: 'STALL', roster: 'STALL ROSTER' };
    if (ap && ap.waitingFor) {
      screen = kinds[ap.waitingFor] || String(ap.waitingFor).toUpperCase();
      decision = true;
      actions = ap.options.map((o) => ({ id: 'decide:' + ap.decisionId + ':' + o.index, kind: 'adapter', op: 'decide',
        index: o.index, label: (o.label + (o.detail ? ' (' + o.detail + ')' : '')).slice(0, 160) }));
      // The bot's own pick, by the build's rules: QAJev takes it when a play step asks no model (not one of Jev's).
      ownPick = { id: 'decide:' + ap.decisionId + ':own', kind: 'adapter', op: 'decide_own', label: "the bot's own pick" };
    }

    const state = { screen };
    if (talkLine) state.talk_line = talkLine;
    const tab = $('.inv.on [id^="inv-tab-"].on, .inv.on [id^="inv-tab-"][aria-selected="true"]');
    if (tab) state.bag_tab = tab.id.replace('inv-tab-', '');
    const setTab = $('.settings-screen.on [id^="set-tab-"][aria-selected="true"]');
    if (setTab) state.settings_tab = setTab.id.replace('set-tab-', '');
    const lvl = text('.lvl-badge'), kills = text('.kills'), timer = text('.timer');
    if (lvl) state.level_text = lvl;
    if (kills) state.kills_text = kills;
    if (timer) state.timer = timer;
    const toast = text('.hud-root .toast.on');
    if (toast) state.toast = toast;
    if (g) { // dev-mode build: the game's own numbers
      try {
        const st = g.testing && g.testing.stages ? g.testing.stages() : null;
        Object.assign(state, {
          phase: g.phase, paused: !!g.paused, training: !!g.training, run_time: Math.round((g.time || 0) * 10) / 10,
          hp: g.player ? Math.round(g.player.hp) : null, alive: g.player ? !!g.player.alive : null,
          kills: g.player ? g.player.kills : null,
          level: g.progression ? g.progression.level : null,
          stage: st ? st.stage : null, stage_mode: st ? st.mode : null, stage_name: st && st.info ? st.info.name : null,
          enemies: g.enemies && g.enemies.alive ? g.enemies.alive.length : null,
          boss: !!(g.enemies && g.enemies.boss), physics_failed: !!(g.physics && g.physics.failed),
          game_over: g.phase === 'dead',
        });
      } catch (err) { state.state_error = String(err); }
    }
    Object.assign(state, a11y(g));
    if (ap) state.autoplay = ap.state;
    if (ap && ap.waitingFor) state.waiting_for = ap.waitingFor;
    // The game's clock is its tick: a soft-lock is a clock that stops while nothing waits for the player.
    if (g && typeof g.time === 'number') state.tick = Math.round(g.time * 100);
    if (g && g.paused) state.settings_open = true; // the pause menu: waits for the player
    else if (g && !(g.streamCalm ? g.streamCalm() : true)) state.overlay = true; // a talk, QTE, stall, cutscene...
    else if (g && g.phase === 'title') state.overlay = true; // the clock waits while the bot starts a run
    // The dev watchdog's blockers (page errors, its own soft-lock and overlay timers), as the game's own findings.
    const w = window.__watch;
    const problems = w && w.blockers ? w.blockers.slice(-20).map((b) => ({ kind: b.kind, detail: b.detail, t: b.t })) : [];

    // What the player reads: the open screen's own text, not the menu underneath it.
    const roots = {
      'LOGOS': '#ui > .boot-splash', 'ENDING': 'section.ecine.on', 'DOJO OFFER': '.tut-dialog',
      'GAME OVER': '.overlay.gameover.on', 'LEVEL UP': '.levelup.on', 'QTE': '.qte.on', 'HERO CV': '.hero-cv.on',
      'STALL': '.stall.on', 'STALL VERDICT': '.stall.on', 'TALK': '.talk.on', 'SETTINGS': '.settings-screen.on',
      'CONTROLS': '.controls-screen.on', 'BAG': '.inv.on', 'CASE FILE': '.inv.on', 'PAUSED': '.overlay.paused.on',
      'OUTFITS': '.outfits-screen.on', 'CREDITS': '.credits-screen.on',
      'DEV TOOLS': '.devmenu', 'TITLE': '.title-screen', 'TITLE MENU': '.title-screen', 'DIFFICULTY': '.title-screen',
    };
    const own = roots[screen] ? text(roots[screen]) : '';
    const texts = [];
    if (screen === 'LEVEL UP') texts.push('Level up! Pick one card.');
    if (ap && ap.waitingFor) texts.push('The game waits for your choice (' + ap.waitingFor + '). Pick one option.');
    if (screen === 'PLAYING') texts.push('Playing a run. ' + [lvl, kills, timer].filter(Boolean).join(' · '));
    if (talkHint) texts.push(talkHint);
    // Kept short: a long screen (1,400 characters of key bindings) tipped Jev into DONE; 600 kept it on task.
    texts.push((own || base.texts[0] || '').slice(0, 600));
    return { screen, decision, actions, state, problems, texts, replaceActions: true, ...(ownPick ? { own_pick: ownPick } : {}) };
  },

  // An action QAJev hands back to the page: a decision, through the bot's own decide().
  act(action) {
    const ap = window.__autoplay;
    if (action.op === 'decide' && ap) return ap.decide(action.index);
    if (action.op === 'decide_own' && ap) return ap.decide(undefined, 'bot'); // no choice: the bot's own pick
    if (action.op === 'dev_item') { // pick a dev menu option, or press one of its buttons
      const it = devMenuItems().find((x) => x.key === action.match);
      if (!it) return { ok: false, error: 'that dev menu item is no longer there' };
      it.el.scrollIntoView({ block: 'center' });
      if (it.kind === 'select') {
        it.el.value = it.value;
        it.el.dispatchEvent(new Event('change', { bubbles: true }));
      } else it.el.click();
      return { ok: true };
    }
    if (action.op === 'set_option') { // a settings row scrolled out of the list: bring it in, then click it
      const o = settingsOptions().find((x) => x.key === action.match);
      if (!o) return { ok: false, error: 'that setting is no longer on the screen' };
      o.el.scrollIntoView({ block: 'center' });
      o.el.click();
      return { ok: true };
    }
    return { ok: false, error: 'unknown adapter action' };
  },

  // Real-time play: the game's own bot (dev build, ?autoplay) fights; QAJev turns it on and off.
  pilot(on) {
    const ap = window.__autoplay;
    if (!ap) return false;
    if (on) ap.start(); else ap.stop();
    return true;
  },
};
