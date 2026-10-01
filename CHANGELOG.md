# Changelog

All notable changes to QAJev. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## Unreleased

- `qajev smoke` and `qajev play` now wait for the load gate (`--load-high`, `--load-ok`, `--load-wait`, or the
  `QAJEV_LOAD_*` variables) before they start, like `check` and `run` do before each scenario. If the machine
  stays busy they exit with code 4 and start nothing. `play` gains the three flags.
- `idle: SECONDS` suite step for `qajev play`: the game runs untouched for that long (no pilot needed, so a
  release build works), then the step's checks run; a crash or close meanwhile is an S1 finding.
- Job titles in `qajev jobs`, `qajev top` and `qa_jobs` say what a run is about: the project or site and the goal
  for website runs, the game and the test's name for games (not `play desktop`), for older jobs too. `qajev top`
  widens the title on a wide terminal.
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
