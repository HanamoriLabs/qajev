# Changelog

All notable changes to QAJev. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

- Changed: Jev saying DONE without taking a single action, while a check fails, is now **harness**, not a product
  **fail**: it declared victory on a page it never tried, which says nothing about the page. A goal Jev worked on
  and a page that is wrong still fail. The audit of past verdicts found 45 of 87 such FAILs (52%) were wrong.
  ([Reports](docs/reports.md#outcomes))
- Every report names the QAJev commit that judged it: `qajev_commit` in `report.json`, beside the version in
  `report.md` and `report.html`, and in `qajev doctor` (`+dirty` when QAJev's own code had uncommitted changes;
  absent for an installed package). The audit of past verdicts could not tell which rules judged a run.
  ([Reports](docs/reports.md))
- Key and react hooks press chords: Shift, Ctrl, Alt or Meta with one key (`key: Shift+A`), sent as trusted key events
  with the modifiers set (`event.shiftKey`), so a test can open the FiGGYZ Verse town editor with a real Shift+A
  (verse1). With Ctrl or Meta, the browser's and the system's own shortcuts (quit, close, reload, a new tab or window,
  the address bar, print) are refused. ([Writing tests](docs/writing-tests.md))
- LIVE: **Watch live** in the dashboard shows a running test's screen as a moving picture (about 5 frames a
  second, 1280 px; 2 when the machine is busy, 1 during real-time game steps). View only, at most 2 viewers a run,
  behind the dashboard's key on 127.0.0.1. **Bring to front** raises the page in QAJev's own Chrome; **Open the
  page** opens its origin and path only. The frame grabber may only take screenshots, read the page's address and
  size, and bring its own page to the front; the report says how long the run was watched live.
  ([The dashboard](docs/dashboard.md))
- `qajev plan FILE` (and `qa_plan` for agents) prints a suite's test plan without running it: each test's about
  and its checks in plain words, and the tests that are NOT DESCRIBED. It reads website suites and game `steps:`
  suites, costs nothing, and exits 2 while any test is NOT DESCRIBED, so it works as a lint before a run or a pull
  request (SideGame1). ([CLI](docs/cli.md#qajev-plan))
- Fixed: the cleanup after a dead run killed the process group of a game pid it had recorded, and `qajev stop`
  signalled a job's recorded pid, without checking that the pid still belonged to that process. A pid reused by
  another program could have been killed. Each launch now records its process's start time and command; the reaper
  and `qajev stop` act only when both still match, and say "stale pid, not ours: left alone" otherwise. A job
  recorded before this change cannot be proved: `qajev stop` refuses it and says how to stop it by hand.
- An Electron game can keep its save folder between runs, so a test proves a save survives into the next run
  (SideGame1: a setting kept; a long plan run in parts). `--game-profile NAME` (suite `game_profile:`, `qa_play`
  `game_profile`) keeps it in `~/.qajev/game-profiles/NAME`; a suite's own `--profile=` switch may name a git-ignored
  folder in the game's repo instead. Nothing else is allowed: `~/Library` and Application Support folders (real,
  Steam-synced saves), `..` and symlinks are refused before anything is created. QAJev marks a folder
  (`.qajev-game-profile`) when it first uses it, and never uses or empties a folder that holds files without that
  mark (a git-ignored `node_modules/left-pad` is left alone). A kept profile is never deleted
  (`--reset-game-profile` empties it), and the report says which folder was used and that it was kept. Before, a
  suite's `--profile=` was silently ignored. ([Games](docs/games.md#a-save-kept-between-runs-electron))
- Changed (breaking): QAJev no longer reads the current folder's `.env`. A project's file held a Cloudflare token
  for its own deploys, QAJev read it first, and Clef answered 401 (Foley1). QAJev's files are `--env-file`,
  `$QAJEV_ENV_FILE` and `~/.qajev/.env`. Its own keys (Jev, OpenRouter, the text model, Cloudflare for Clef) come
  from those files even when the shell has another value; `QAJEV_<NAME>` pins one on purpose, and a key no file
  sets still comes from the shell (CI). `qajev doctor` and each report's header say where each key came from, names
  only, and a model's HTTP 401 or 403 points there. If you kept QAJev's keys or an `env:` secret in a project's
  `.env`, move them to `~/.qajev/.env` or pass `--env-file`. ([Configuration](docs/configuration.md))
- Fixed: a page with a ticking number (a video timer in a game's HUD, a countdown, a clock) made every one of Jev's
  moves go stale before it acted, so goals there ended as harness (verse2, FiGGYZ Verse). A clock time ("0:03",
  "1:05:09") or a countdown in seconds or minutes ("59 s", "5 min") that ticks, in the page's words, a control's
  label or the text around the target, is no longer a change. Other digits stay exact: a step ("Step 2 of 3"), a
  count ("Cart (2)"), a price or a label ("Buy 100") that changes, new words, another address, a reload, the scroll,
  an input's value or a link's address still make Jev decide again.
- Fixed: a game goal step passed on checks that already held before it began. It ended as soon as its `expect` held,
  so a check true from the start passed after 0 actions (SideGame1: "lesson 1" passed on "still in training"). Such a
  step is now unverified, never passed. A new step option `stop: end` plays the goal to DONE or its budget before the
  checks judge, for a step whose check comes true early. ([Games](docs/games.md))
- Fixed: a scenario without a goal judged its checks on the page as read right after it loaded, before its
  `before:` hooks ran. A `wait_for` there seemed not to wait, and a check that held at once passed on a page still
  loading (verse2: a game judged at its spawn point, nothing drawn yet). The checks now judge the page after the
  hooks.
- A `wait_for` timeout is in seconds, at most 300; a suite that says more is refused when it loads, and 1000 or
  more is named as milliseconds (`timeout: 60000` read as almost 17 hours).
- Fixed: `visible` passed on words a person could not see. A first-run overlay covering the whole page, and a
  label cut to "Message to Or…" by its ellipsis box, both passed (FlockTab1). Visible now means drawn on top and
  whole: the words' own lines are checked for anything drawn over them, and against any `overflow` box that clips
  them. A failure says what a person sees instead: "covered by div#overlay: …" or "cut short: div.truncate shows …".
  An element that lets clicks through (`pointer-events: none`), such as a badge, does not count as covering.
- A `click` hook waits up to 2 s for its target to be on top and still before it clicks, so a splash that fades
  or a menu panel still settling no longer ends a test as harness (SideGame1). A target that stays covered fails
  naming what covers it.
- A seeded TEST user on a local dev host signs in without a person, second factor included. `totp:` gives its
  TOTP secret: QAJev computes the code (RFC 6238) and types it into the page's one-time code field. `cookie:` instead
  sets a session cookie its seed minted. Both come only from the new `seed:FILE#KEY` reference: a key of the app's
  own JSON test-user fixture, which must say `"test_account": true` and list the host in `allowed_hosts`. Both are
  refused unless the sign-in page is localhost, 127.0.0.1, `*.localhost` or `*.test`, written so a browser cannot
  read another host, and the email is at a reserved test domain. No secret or code is written anywhere; the report
  says "as seeded test user … on localhost; TOTP from seed: yes". ([Writing tests](docs/writing-tests.md#signed-in-areas),
  [Projects](docs/projects.md))
- Fixed: an address with a backslash, `user@` or control characters (`http://evil.com\@localhost/`) counted as
  loopback, while a browser reads another host there. Such an address is never loopback or a local dev host, so
  `mutate` mode, an `http` sign-in and secret fields are refused on it.

## 0.3.0: 6 Oct 2026

- A test plan opens every report and the dashboard: one numbered item per test, its `about` and each check in plain
  words, with a box ticked as the run goes (✓ passed, ✗ failed, ! stuck or harness). The dashboard shows it before
  the first test runs. A check gets its words from the new `says` (on a suite's `expect`, `--says` with
  `--expect-js`, `says` on `qa_check`); a test without an `about`, or a `js`, `url_regex`, `fetch` or `command`
  check without words, is NOT DESCRIBED, and the gate is then INCOMPLETE, not PASS. The generic "The page's own
  script check should come out true" is gone. ([Writing tests](docs/writing-tests.md), [Reports](docs/reports.md))

## 0.2.0: 6 Oct 2026

- Fixed: a `js` check passed on any truthy value, so a check written as `cond || 'why it failed'` passed when
  it failed (its 'why' is a non-empty string). A check now passes only on exactly `true`; a string fails with that
  string as the reason, `false` and `null` fail as such, and any other value fails as the wrong type. The value that
  came back is kept on every check in report.json. Waits (`wait_for`, `until`) still hold on any truthy value.
- Fixed: a `react` hook took the frames a tick asked for before sending its keys, so a `{shot}` asked for
  with a `{down}` delayed the press by a screenshot (SideGame1: inside a 0.6 s grip flash). Keys go first now, and the
  report logs each held key's down and up times and each frame's start and duration.
- The dashboard and `qajev top` show the UX notes, not only the reports. In the dashboard a test with notes says how
  many beside its name (`3 UX notes`); open, it lists each note by what it rests on, with its rule and examples, and
  the run has a UX section with the count and the design consistency per device. `qajev top` counts them
  (`UX 12 measured`) on a finished job, a recent report and each scenario in the details, and shows a run's design
  consistency. ([The dashboard](docs/dashboard.md), [Jobs and top](docs/jobs-and-top.md))
- Fixed: a run with `--ephemeral` was reported as "QAJev-managed profile default", and `qajev browser status`
  listed its Chrome as `default`, so a throwaway run looked like the shared profile. Both now say throwaway.
- Fixed: four UX false alarms seen on a real game's title screen. Text hidden for screen readers only was called cut
  off (and low contrast); text under an opaque full-screen splash was called overlapping; a page that takes Tab as a
  game key was called "not reachable by keyboard" (now "keyboard not measurable" on a game, while an ordinary page
  that cancels Tab is "keyboard blocked", a WCAG 2.1.1 failure); and a hidden `display:none`
  h1 counted towards "several <h1>" (hidden ones are now named apart).
- Fixed: the UX layout notes measured element boxes, not the text drawn in them. A status word running out of its
  card into the next one, and a value drawn over its label, got no note; a wrapped inline (its box spans both lines)
  and a log's lines scrolled out of view were called overlapping. Text is now measured line by line where it is
  drawn, cut down to what scrolling or clipping boxes show. New note: "text overflows its box". "Text cut off" also
  counts text partly clipped by a box it sits in (not a carousel's or a ticker's parts out of view).
- The smoke crawl measures each page's UX and adds a UX section to the report, free and never changing the gate:
  WCAG AA text contrast (text over images counted as not measured), keyboard reach, visible focus and traps (QAJev
  presses Tab through the page), 200% zoom (sideways scroll, text cut off), text cut off or overlapping, and open
  dialogs (named, modal, focus, Escape). Design consistency compares each kind of element's computed style across
  pages and names each page that differs, with both values. `--no-ux` skips it. ([Reports](docs/reports.md#ux))
- Fixed: on macOS `qajev dashboard --stop` could return while the dashboard was still exiting and held its lock, so a
  start right after failed ("another dashboard holds the lock but is not serving"). It now waits for the lock too,
  and fails with an error (exit 3) if the dashboard has not stopped within 15 s.
- Fixed: a `qajev dashboard` that looked while another was writing its record found it empty, took that one for
  gone and started serving too. The record is now written whole, then renamed into place; a start removes a
  half-written one that a killed dashboard left behind.
- A scenario with a goal adds struggle signals to the UX section, at no extra cost, from Jev's own run (the goal run
  itself is paid): how findable the goal was (actions, pages, backtracks, scrolls), steps where two options looked
  almost equally right, and text below the fold. A goal not reached because the guard hid what it needed, or because it
  needs a key press or a drag, is harness with that reason instead: it says nothing about the page. A key counts only
  as a key press ("press Tab", "the backquote key"), and a hidden danger control only when the goal asks for its action.
- `key` hooks press any plain key (letters, digits, punctuation, Space, the arrows...), repeat a sequence
  (`{press: [f, j], repeat: 15, interval_ms: 30}`) and hold a key (`hold_ms`, up to 5 s), as trusted key events: real-key
  play-tests for web games. No modifiers or combinations. ([Writing tests](docs/writing-tests.md))
- `react` hooks play in real time: every 50 ms a policy in the page reads what the player can see and returns keys to
  press, hold or release, and frames to keep (`{shot: label}`), until a condition holds. For cues too short for
  slower polling, at a human reaction time the suite states. Held keys are bounded to 5 s and always released.
- The smoke's "tap targets under 24 px" finding names up to 10 of them (tag, text, size, a short CSS path; a field
  by its label, never its value), and counts as WCAG 2.5.8 does: links inside a sentence and small targets with
  room around them are left out, and the finding says how many.
- A run that broke before it finished (browser error, failed hook, missing guard), or that never ran some of its
  checks or steps, is `harness` (gate INCOMPLETE), never `pass` on the checks that did run. The reason names where it
  stopped and what never ran; `report.json` and the MCP results carry `stop_detail` and `not_run`. A five-player
  scenario had graded PASS after two of its checks.
- Multiplayer: the checks over every player's state no longer break once those states pass 64 KB (the browser
  daemon's limit per command); a command over that limit now says so, with its size.
- A scenario whose checks need another page, where Jev only scrolled the start page and clicked nothing, is
  `harness` ("Jev never left the start page"), not `fail`: it says nothing about the product. Jev clicking around
  and not arriving still fails.
- Multiplayer scenarios: `clients: N` opens N players, each in a browser context of its own (own cookies, storage
  and cache). `steps` drive them together (`all`, with `stagger` or `jitter`; `client` for some; `snapshot` reads
  every player's `state` once `until` holds, and when), page checks hold on every player, and `expect.across` checks
  are judged over all their states. The report shows each player's screen and timings. No model calls.
  ([Writing tests](docs/writing-tests.md#multiplayer-several-players-at-once))
- A `reload` hook: the same page again, keeping its cookies and storage.
- A `stuck` or `harness` result names what the guard held back, and the words that made it ("guard hid: 'Shop Show
  clothes and gear you can buy' (danger: buy)"), and the report's note lists them, so a hidden control is no longer
  mistaken for a layout problem.
- Hook clicks and fills prove the guard as Jev's actions do: on a page where it is absent, out of date or still
  waiting, the scenario stops (`guard_missing`) and nothing is clicked.
- The read-only guard no longer causes React hydration errors on React and Next.js sites: it waits for the page's
  load, and for React to take over each server-rendered control, before disabling or hiding it (writing requests
  stay blocked from the first byte). QAJev waits for it before every action and hook click, and stops the scenario
  if it is still waiting after 12 s. A hydration warning that names only the guard's own attributes is a harness
  note in the report, not a page finding.
- A decision that goes stale says why: Jev's own reason, what was wrong with its target ("covered by div#toast",
  "hidden", "off the screen") and what changed under it ("text: 'Score 41' → 'Score 42'", "controls: +'Pay'"). It
  shows in the dashboard's decisions, in `qajev top --decisions`, and at the end of a "stale" stop's reason.
- A "failed to load" finding says why, in Chrome's words (`net::ERR_EMPTY_RESPONSE`, `CORS: ...`), from a network
  log on a debugger link of its own. When the file itself loaded (a module whose import failed), it names the
  requests that failed around it.
- `expect: {status: 404}` in a website suite or project objective: the page must answer with that HTTP status. A
  test that sets out to show a removed page passes on it, and the 404 is no longer also filed as an S2 finding.

- The dashboard wears QAJev's logo: the repo's own brand files (the dark lockup in its header, the mark as its
  favicon), packaged with QAJev; report.html carries the mark inline (it still loads nothing from elsewhere).
- The dashboard's key is kept across restarts (`~/.qajev/dashboard.key`, 0600), so restarting it no longer breaks an
  open tab; `qajev dashboard --new-key` makes a new one.
- [The dashboard](docs/dashboard.md): its own page in the docs: what it shows, starting and stopping it, its key,
  opening a run, what the MCP tools give, and troubleshooting.

- The dashboard, live: an open running run updates as it happens (a server-sent stream, not a 3 s poll), and its
  running test shows what it is doing, its last steps and its screen (a frame every 2 s for website and Electron
  runs, taken only while someone watches). A website test whose check runs a long script now says so ("reading the
  page and running its checks"), where it showed nothing.
- The dashboard's tests have a part for people ("What went wrong": each check in plain words, with what was there
  instead) and a folded part for agents (the raw checks, reason, decisions and actions, and **Copy for an agent**).
- A missing text's check now shows the closest text on the page ("closest on the page: … overlapFrames=5 …")
  instead of the first 400 characters of the page.
- Fixed: `qajev dashboard --stop` returned before the dashboard had exited, so a start right after failed ("another
  dashboard holds the lock but is not serving"). It now waits for it to go.

- `about`: what a test proves and why, in plain words. On a suite, its scenarios or steps, a project objective,
  `--about` on `check`, `play` and `run --objective`, and `about=` on the MCP tools. The report (HTML and Markdown)
  and the dashboard show it under the test's name and the run's title. The MCP instructions ask agents to always
  give it, and a result lists the tests without one under `about_missing`.

- Vision is on by default for games: with Clef deciding and a window to look at (Electron, mobile, a windowed
  Godot game), `qajev play` sends the screen with every decision. `--no-vision` (MCP `vision=false`) or
  `vision: false` on a suite or step turns it off; with Jev, or a headless Godot game, it stays off without
  refusing. Websites stay text-only unless asked.

- Fixed: a game still running that did not answer QAJev in time ("no answer to Runtime.evaluate within 20 s") was
  filed as an S1 "game crashed or closed". QAJev now measures its renderer for 3 s: pegged (80% of a core or more)
  is an S1 "game froze", idle is an S2 "game stopped answering" (most likely the debugger link). A game that
  really closed stays S1 "game crashed or closed".

- `qajev dashboard`: every run on the machine in a local web page (designed in Claude Design, in qajev.com's look).
  Filter by project, result, kind and period; open a run to see what is running now, each test with its checks,
  findings, screenshots and the model's decisions; stop, rerun (all or failed only) or start a project's run.
  `--background` keeps it running after the terminal closes; `--stop` stops it. It serves 127.0.0.1 only, behind a
  key in its address.

- Fixed: a play step with no `decide` clicked the first offer at every decision, so a step like "the bot picks its
  cards under the build's rules" never ran the build's rules (a no-jutsu build went into its boss fight with 5
  jutsu) and still passed. The game's own bot now makes those picks when its adapter offers one (I'M HIM!), and
  the "decision not made" finding names the model that was asked (Jev, Clef).

- A page error in a website run now carries its stack (file, line, column), not only its message, so the finding
  points at the line that threw.

- Changed: a play step's `until` is now a requirement. Not met by the time cap (or the game over first), the step
  fails on a "reached ..." check, where it used to pass on its other checks: "eizen: beaten" (`until: {boss:
  false}`) passed with the boss alive. A soak that only stops early sets `until_optional: true`. `until` also reads a
  key the game's state lacks, such as `screen`, from the game's look.

- Fixed: a play step cut short (the game closed its window, the model failed, the cost cap) passed when its frame
  rate and memory checks held until then. It is now a harness stop with its S1 "game crashed or closed" finding.

- Fixed: `qajev top` took seconds per refresh, too slow to use with many jobs queued: it started a `ps` for every job,
  every second, to check it was alive, and read every finished job's whole log again. On macOS it now asks the
  kernel (no process), and a finished job is read once until its files change: a refresh went from 1.9 s to 0.05 s.

- Run specific tests: `qajev play --suite X --only STEP` (MCP `qa_play(only=...)`) runs chosen steps of a game
  session, plus the steps they name in `depends_on` and every `setup: true` step. `qajev rerun JOB` runs a finished
  job again with the same settings, and `--failed` only its tests that failed, got stuck or hit a harness limit (MCP
  `qa_rerun`). Website suites and projects already had `--only` / `--name`.

- Fixed: `qajev rerun` and `qa_rerun` ran in the current folder, not the folder the job ran in, so a relative path in
  the job's command (a game, a suite) pointed at nothing. A rerun now runs where its job ran.

- A rerun's job title ends "rerun of JOB" (", failed only" with `--failed`), so `qajev jobs` and `qajev top` tell it
  from the job it reruns.

- Fixed: in a game session, one problem the game's own watchdog reported failed every later play step, not only the
  step it happened in (an adapter lists its recent problems on every look, and each step counted them again). Each
  problem is now reported once per launch, in the step where it first appears; the same kind happening again later
  (a new time) still counts, and a relaunch starts the count again.

- `qajev top`: `d` on a job shows its decisions live, newest first: the screen, what the model chose, how sure it
  was, the runner-up and how long it took, coloured by certainty, with the job's median p, low-confidence count,
  BLOCKED/DONE and median answer time above. `qajev top --decisions JOB` opens it directly (`--once` / `--json`
  print it). Runs now log each decision as a `decision` event (websites and games, stale decisions too).
- The bundled I'M HIM! adapter: OUTFITS and CREDITS are named screens (with OUTFITS open over the title it read as
  TITLE MENU, so a step closing it passed while it was still open). A conversation says which line is on show and that
  it closes by itself after the last one, Continue says whether it finishes a typing line or shows the next, and the
  box's ✕ is no longer offered beside the Escape key. Decision models stopped giving up partway through long talks
  and on a talk that comes up after a death.

- Images, with Clef: `looks` expectations (`--expect-looks`, MCP `expect_looks`) are plain statements Clef judges
  from the final screenshot, each passing at a probability of 0.5 or more, with that probability in the report. They
  catch what text checks cannot: a button cut off by its box, a price drawn on a canvas, a label that is an image.
  `vision: true` (`--vision`, MCP `vision`) sends the screenshot with every decision, so Clef can choose a button whose
  label is only pixels. Both work for websites and for games with a window (`qajev play`); a run that asks for them
  without Clef, or a headless Godot game, is refused before it starts, saying what to set.
- Reports count and list the decisions under the name of the model that made them (`6 Clef decisions`,
  `Clef's next step`).
- A Clef call that loses its connection or gets a 5xx from Workers AI is asked once more (nothing was done in the page
  yet); a 4xx is not repeated.

- Clef as the decision model: `QAJEV_JEV_PROVIDER=cloudflare` (or `--jev-provider cloudflare`) with
  `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` sends the decisions to Cloudflare's open Clef models on Workers AI
  instead of Jev (`QAJEV_CLEF_MODEL`: `clef-flash`, the default, or `clef`). QAJev answers a one-option choice itself
  (Clef refuses it) and unwraps Workers AI's envelope; Clef's decisions are priced from its token counts. Reports,
  `qajev doctor` and `qajev top` name the model deciding: Jev, Clef or Clef-flash. Clef is used only when chosen:
  `auto` never sends pages to Workers AI on Cloudflare credentials set for other tools.

- Godot game suites: `seed: qa/...` copies saves from a folder in the game's own `qa/` folder into the throwaway
  `user://` before the first launch (legacy-save fixtures), and a `relaunch: true` step restarts the game on the same
  saves (save, relaunch, Continue), also after a step that quit it.
- `play: {strict_decisions: true}`: a play step fails at the first decision Jev did not make, instead of taking the
  first offer and going on (an S3 finding), so a route run is evidence only when Jev made every choice.
- Fixed: Godot games under `qajev play` saved into the player's real `user://` folder (settings, checkpoints, logs).
  Godot 4 has no `--user-data-dir`, so the flag QAJev passed was ignored. The game now runs with `HOME` (and the XDG
  folders) inside QAJev's throwaway folder, which is where Godot puts `user://`, also for a custom user dir; the boot
  script checks it before the game's first scene and refuses to start the game otherwise. `QAJEV_USER_DIR` names
  the throwaway folder, for a game that keeps files elsewhere.

- Fixed: a `wait_for` hook, or an account's `signed_in: {js: ...}`, whose JavaScript returned a Promise held while
  the Promise was still pending (`!!promise` is true), so `Promise.resolve(false)` or a rejection let a run go on.
  Every JavaScript condition and `js` expectation is now judged by what it settles to; a throw, a rejection or no
  answer within 5 s is not met (an expectation that never answered hung the page check before).

- Runs that meet a sign-in page say so. A scenario that ends on a sign-in page it did not start on is `harness`,
  "needs sign-in: ...", not a product failure. The report, `report.json` and MCP results carry `needs_sign_in`: the
  pages, and a `next_step` telling an agent to ask the person for access (sign in once, or `qajev account add`),
  never for the password. The smoke crawl lists the pages it found behind a sign-in (`behind_sign_in`).
- `qajev account add NAME --email ... --login-url ... [--project P --default]`: a stored test account in one step.
  The Keychain asks for the password, the account is written into the project (references only) or printed for a
  suite, and QAJev signs in once to prove it. `qajev account check NAME --project P` signs in alone.
- Links to other sites (store badges, social links, `mailto:` addresses) stay on the page, in screenshots and for
  checks, and are inert instead of hidden: Jev is still not offered them and a click on them does nothing. Hiding
  them made a Cloudflare-protected email address read as empty once decoded, and store badges vanish (#1).
- `qajev play android:PACKAGE` starts the app's launcher activity with `am start -W` and judges the launch by the
  app's process. `monkey` exits 251 on Android 15 images even when it launched the app, and the run stopped (#1).

- `qajev smoke` and `qajev play` now wait for the load gate (`--load-high`, `--load-ok`, `--load-wait`, or the
  `QAJEV_LOAD_*` variables) before they start, like `check` and `run` do before each scenario. If the machine
  stays busy they exit with code 4 and start nothing. `play` gains the three flags.
- `idle: SECONDS` suite step for `qajev play`: the game runs untouched for that long (no pilot needed, so a
  release build works), then the step's checks run; a crash or close meanwhile is an S1 finding.
- Job titles in `qajev jobs`, `qajev top` and `qa_jobs` say what a run is about: the project or site and the goal
  for website runs, the game and the test's name for games (not `play desktop`), for older jobs too. `qajev top`
  widens the title on a wide terminal.
- Stored test accounts: a suite's `account:` (or a project's `[accounts.NAME]` named by `account = "NAME"`) gives an
  email and a password *reference*: `keychain:SERVICE/ACCOUNT` (macOS Keychain; the secret service on Linux),
  `op://VAULT/ITEM/FIELD` (1Password's `op`) or `env:NAME`. Before the scenarios, QAJev itself signs in, in its own
  unguarded tab, then runs the scenarios guarded and signed in. Jev never sees the password; it is read only at
  sign-in, typed only into a password field on an `https` (or localhost) page of an allowed host, and redacted from
  everything QAJev writes. A failed sign-in stops the run (`harness`, with the page's reason). The report says who
  signed in. `qajev secret set|check REF` stores a Keychain password at the Keychain's own prompt, or checks that a
  reference can be read (never printing it). Projects' older `email_env` / `password_env` still work.
- Real-time play: a decision still closing after Jev's pick (same screen, same offers, within 2.5 s) is not asked
  again. Jev answered DONE there, which matched no offer, so the first offer was clicked a second time and
  "decision not made by Jev" filed. When Jev does answer DONE or BLOCKED at a decision, the finding and the step
  say so, and the history no longer shows that answer's probability as the offer's.
- Content Security Policy violations are findings in every website run (smoke included): `blocked by CSP` (S2)
  when the policy stopped a request or script, `CSP violation (report-only)` (S3) for what a report-only policy
  would stop, with the directive and the address, other hosts' tags and pixels included. They never reach the
  console, so they went unreported before.
- A run stopped (`qajev stop`, Ctrl-C) in its first moments, before its first report, exits with code 130 and
  shows in `qajev jobs` and `qa_job` as INCOMPLETE ("stopped before a report"). It died with a traceback, and its
  job had no gate.
- `examples/launch-demo.yaml`: five website scenarios against the local example site that end in pass, fail and
  stuck in about 30 seconds, for a screen recording ([Getting started](docs/getting-started.md)).
- `qajev top`: a Chrome or game that is up shows in green with a `●`, set apart from the grey "none running" and
  "no game running" lines (a running Chrome was the same grey before).
- `qajev play --allow LABEL --hide LABEL --expect-closed`, and the same on MCP `qa_play` (`allow`, `hide`,
  `expect_closed`).
- Game suites (Godot and Electron) take `hide` and `allow` labels, as mobile suites do, and `expect: {closed:
  true}` passes when the game quits by itself with exit code 0: a test can press QUIT on purpose
  (`allow: [QUIT]`). Fixed: an Electron app that closed mid-step ended the whole run with a traceback.
- Electron games take their end-of-step screenshots also when run `--headless` (the MCP default); `--headless`
  only changes Godot. `qa_play` gains `shots`.
- I'M HIM! adapter: accessibility state for SETTINGS › ACCESSIBILITY (captions, edge markers and spoken menus
  counted across a session, with the last texts; the page's accessibility classes; saved settings as
  `setting_*`; contrast and game speed in a dev build).
- `js:` and `crash_renderer:` suite steps for Electron games: run a script in the page (its value and any page
  error it causes are recorded), or crash the renderer on purpose while the app runs on, for error- and
  crash-reporter proofs.
- I'M HIM! adapter, dev builds: "Open the dev menu (`)" during a run, and the menu's dropdown options and buttons
  as actions ("Enemies: Kunai thrower", "Boss: Fight"); its progress wipe stays hidden.
- Electron: punctuation keys carry their DOM code ("`" is Backquote, "[" BracketLeft...); they went out with an
  empty code, so a game that reads `e.code` ignored them.
- I'M HIM! adapter: settings rows below the fold of a scrolling tab are offered too; picking one scrolls it into
  view and clicks it (they were never offered, so Jev answered BLOCKED).
- I'M HIM! adapter: settings options are offered by row ("Vibration: OFF", "(current)" on the selected one; only
  the first "ON"/"OFF" was offered before), the open settings tab is in the state, and the title and first-launch
  offer actions say they lead to the main menu (Jev answered BLOCKED on them before).

## 0.1.0: first public release

- **Test websites in plain words.** `qajev check` (one goal with expectations), `qajev run` (a suite of
  scenarios), and the free `qajev smoke` crawl (errors, broken links and images, accessibility and SEO basics).
- **Verdicts from the page, never from the model.** Outcomes keep product failures (`fail`) apart from a visitor
  getting lost (`stuck`) and problems on QAJev's side (`harness`). Gates and exit codes for CI.
- **Reports** for people and programs: `report.html` (one self-contained page, light and dark), `report.md`,
  `report.json`, and screenshots.
- **Projects:** store a product's objectives once, run them any time, see what changed since the previous run;
  a cross-project index; `qajev nightly` notifies only on change.
- **Jobs and a machine-wide queue:** every run can be followed and stopped from anywhere (`qajev jobs`, `qajev
  stop`); `qajev top` is a live dashboard showing what Jev is doing and what each run costs.
- **MCP server** (`qajev mcp`) with 15 tools, and a ready-made [agent prompt](AGENT_PROMPT.md).
- **Desktop and phone by default:** every website test and smoke crawl also runs in a phone view.
- **Mobile apps:** native apps and mobile websites on the iOS Simulator and Android emulator.
- **Real device browsers (opt-in):** `--real-devices android` also runs website tests in Chrome on an Android
  emulator, for the bugs only a real device shows. Safari on the iOS Simulator is not there yet: QAJev cannot read
  the page's content there, so those copies come out `harness` and say why.
- **Games:** Godot and Electron games through QAJev's bridge; Jev works the menus, the game's own pilot plays in
  real time, and QAJev checks frame rate, memory, soft-locks and errors.
- **Safety built in:** read-only on real sites, changes only on loopback, dangerous controls hidden, secret and
  payment fields disabled, no downloads, a deaf microphone, a hard cost cap, QAJev's own browser.
- Runs on macOS and Linux (also in Docker and on CI: `QAJEV_CHROME_FLAGS=--no-sandbox`), with TypeSafe or OpenRouter
  for Jev.
