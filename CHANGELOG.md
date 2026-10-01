# Changelog

All notable changes to QAJev. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
