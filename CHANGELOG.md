# Changelog

All notable changes to QAJev. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

- `expect: {status: 404}` in a website suite or project objective: the page must answer with that HTTP status. A
  test that sets out to show a removed page passes on it, and the 404 is no longer also filed as an S2 finding.

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
