# CLI reference

Every command, what it is for, and its options. `qajev <command> --help` prints the same from the tool itself.

| Command | For |
|---|---|
| [`smoke`](#qajev-smoke) | crawl a site for free and list what is broken |
| [`check`](#qajev-check) | one scenario: a goal and expectations on a URL |
| [`run`](#qajev-run) | a suite file, or a project's objectives |
| [`play`](#qajev-play) | test a game or a mobile app |
| [`report`](#qajev-report) | print a run's report again |
| [`projects`](#qajev-projects), [`reports`](#qajev-reports) | known projects; their recent runs |
| [`jobs`](#qajev-jobs), [`stop`](#qajev-stop), [`top`](#qajev-top) | follow, stop and watch runs |
| [`nightly`](#qajev-nightly) | every project, every night, notified only on change |
| [`browser`](#qajev-browser) | QAJev's own Chrome, and signing in |
| [`doctor`](#qajev-doctor) | check the setup |
| [`init`](#qajev-init) | write a starter suite |
| [`mcp`](#qajev-mcp) | run the MCP server for AI agents |

## Exit codes

| Code | Means |
|---|---|
| `0` | PASS |
| `1` | FAIL: the product is wrong somewhere |
| `2` | INCOMPLETE: something could not be judged |
| `3` | a mistake in the suite or the options, or the run was refused |
| `4` | no browser available (or the queue wait ran out) |
| `130` | stopped (Ctrl-C or `qajev stop`) |

## Options shared by `check`, `run` and `smoke`

**Browser**

| Option | Does |
|---|---|
| `--headless` | run Chrome with no window |
| `--ephemeral` | a throwaway browser profile, deleted after the run |
| `--profile NAME` | a named QAJev browser profile (sign in to it once with `qajev browser login`) |
| `--cdp-url URL` | attach to a Chrome you started yourself instead of QAJev's |
| `--real-devices ios,android` | also run each website scenario in a real device browser (opt-in; see [Mobile](mobile.md)) |
| `--devices LIST` | the devices every website test runs on (default `desktop,phone`; `--device` pins one) |
| `--motion reduce\|full` | `reduce` (default): pages are told the visitor prefers less motion, so busy animations stop changing under Jev; `full`: as a normal browser |

**Output**

| Option | Does |
|---|---|
| `--out DIR` | where run folders go (default `./qajev-runs`) |
| `--json` | print the report as JSON on stdout |
| `--events` | stream progress as JSON lines on stderr |
| `--quiet`, `-q` | no progress output |
| `--no-shots` | no screenshots |
| `--background` | start the run as a job and return at once (follow it with `qajev jobs`) |

**Cost and model**

| Option | Does |
|---|---|
| `--cost-cap USD` | a hard cap for the whole run (default: the suite's, else $1) |
| `--env-file FILE` | where your keys are (default `./.env`, then `~/.qajev/.env`) |
| `--jev-provider auto\|typesafe\|openrouter` | where Jev's decisions go |
| `--usd-per-call USD` | the estimated TypeSafe cost per decision, for the ledger |
| `--strict` | count `stuck` scenarios as failures |

**Busy machines**

| Option | Does |
|---|---|
| `--load-high N` | wait before a scenario while the 1-minute load is at or above N (0 = never wait; default 150) |
| `--load-ok N` | resume once the load drops below N (default 100) |
| `--load-wait SECONDS` | total waiting allowed per run (default 600) |

## `qajev smoke`

Crawl the pages of a site (same host only) with **no model calls**, and lint each one: HTTP status, script errors,
broken images, a blank page, missing title or description, accessibility basics, layout shifts and more.

```bash
qajev smoke http://localhost:3000 --max-pages 30 --check-links
qajev smoke --project shop            # a project's site, from its smoke_start page
```

| Option | Does |
|---|---|
| `--max-pages N` | how many pages to visit |
| `--check-links` | also check the links it did not visit (download links are checked without downloading) |
| `--device NAME` | `desktop`, `tall`, `phone`, `tablet` or `WIDTHxHEIGHT` |
| `--delay SECONDS` | between pages (default 1 on public sites, 0 on your own machine; robots.txt may ask for more) |
| `--project`, `--env` | crawl a project's environment |

## `qajev check`

One scenario from flags.

```bash
qajev check https://shop.example \
  --goal "Find what the Pro plan costs per month. Stop when that price is visible." \
  --expect-text '$29 per month' --expect-url /pricing
```

| Option | Does |
|---|---|
| `--goal`, `-g` | what Jev should do, in plain words, ending with "Stop when ..." |
| `--expect-text`, `-t` | the page must show this (repeatable) |
| `--absent`, `-a` | the page must not show this (repeatable) |
| `--visible` | this must be on screen, not just in the page (repeatable) |
| `--expect-url`, `-u` / `--expect-url-regex` | the final address contains this / matches this |
| `--expect-js`, `-j` | a JavaScript expression that must be true |
| `--fetch URL[=STATUS]` | a request from the page that must answer STATUS (default 200) |
| `--mode readonly\|mutate` | `mutate` lets Jev change data: localhost only |
| `--device NAME` | `desktop`, `tall`, `phone`, `tablet` or `WIDTHxHEIGHT` |
| `--persona TEXT` | who Jev is, e.g. "You are on your phone and new to this site" |
| `--host HOST` | another host Jev may visit (repeatable) |
| `--max-actions N`, `--max-seconds N` | Jev's budget |
| `--settle SECONDS` | how long to wait for the expectations after Jev stops |
| `--speech TEXT` | what the fake microphone "hears", for pages that listen |

## `qajev run`

A suite file ([Writing tests](writing-tests.md)), or a project ([Projects](projects.md)).

```bash
qajev run qajev.yaml
qajev run qajev.yaml --only "a visitor sends feedback" --headless
qajev run --project shop --suite core
qajev run --project shop --objective "A visitor finds the refund policy" --expect-text "30 days"
```

| Option | Does |
|---|---|
| `--only NAME` | run this scenario, plus what it depends on |
| `--jobs N` | run independent chains of scenarios in parallel (default 1) |
| `--allow-commands` | let the suite run its shell commands |
| `--project`, `-p` | a project name, or a repository path with `.qajev/project.toml` |
| `--env`, `-e` | the project environment (default: the project's `default_env`) |
| `--suite TAG` | the objectives with this tag (repeatable) |
| `--name NAME` | one stored objective |
| `--objective TEXT` | an ad-hoc goal instead of the stored objectives, with `--url` and `--expect-*` |

## `qajev play`

Test a game (a Godot project folder, an Electron `.app` or Electron project folder) or a mobile app
(`ios:<bundle id or URL>`, `android:<package or URL>`). See [Games](games.md) and [Mobile](mobile.md).

```bash
qajev play path/to/game --goal "Start a new game. Stop when you are playing." --expect-screen GAME
qajev play path/to/game --suite session.yaml
```

| Option | Does |
|---|---|
| `--goal`, `-g` | what a player wants, ending with "Stop when ..." |
| `--suite FILE` | several steps in one game session: goal steps and real-time play steps |
| `--adapter NAME\|PATH` | the game's adapter (bundled name, or a `.gd` / `.js` file) |
| `--expect-screen NAME` | the screen the game must be on at the end |
| `--expect-text TEXT` | the game must show this (repeatable) |
| `--expect-state KEY=VALUE` | a game state value, e.g. `game_over=false` or `kills=">= 1"` (repeatable) |
| `--min-fps N` | the frame rate must be at least this |
| `--allow-errors` | engine or script errors do not fail the run |
| `--game-env KEY=VALUE` | an environment setting for the game (repeatable) |
| `--game-arg ARG` | a switch for an Electron app, e.g. `--game-arg=--fullscreen` (repeatable) |
| `--device NAME` | mobile: the iOS simulator to clone, or the Android virtual device to boot |
| `--install FILE` | mobile: an `.apk` or simulator `.app` to install on the throwaway device first |
| `--headless` | no window: fastest, no screenshots (Godot) |
| `--name`, `--max-actions`, `--max-seconds`, `--no-shots`, `--out`, `--cost-cap`, `--json`, `--events`, `--quiet`, `--background` | as above |

## `qajev report`

```bash
qajev report qajev-runs/20261001-101500-shop          # the report as Markdown
qajev report qajev-runs/20261001-101500-shop --json   # as JSON
qajev report qajev-runs/20261001-101500-shop --html   # rebuild report.html and print its path
```

## `qajev projects`

Lists the projects QAJev knows, their environments, and where their reports go. `--json` for data.

## `qajev reports`

Recent project runs, newest first: gate, outcome per objective, cost, the report's path.

```bash
qajev reports --project shop --limit 10
```

## `qajev jobs`

Every QAJev run on this machine: who has the browser, what is queued, running and recent. Give a job id for one
job's progress, the scenarios finished so far, and its report once done. See [Jobs and top](jobs-and-top.md).

```bash
qajev jobs
qajev jobs 20261001-101502-a3f9
```

## `qajev stop`

```bash
qajev stop 20261001-101502-a3f9
```

The run closes its browser, keeps the scenarios that finished and writes its report (INCOMPLETE).

## `qajev top`

The live dashboard. `--once` prints one snapshot; `--json` prints it as data. See [Jobs and top](jobs-and-top.md).

## `qajev nightly`

Each project's `core` objectives plus a smoke crawl, compared with the previous night; notifies only when
something changed. See [Projects](projects.md#nightly-runs).

| Option | Does |
|---|---|
| `--plan` | print what would run |
| `--last` | print the latest digest |
| `--project NAME` | only this project (repeatable) |
| `--max-pages N` | smoke crawl size per project (default 10) |
| `--no-notify` | never notify |
| `--install [--at HH:MM]` | schedule it every night (macOS; elsewhere it prints a cron line) |
| `--uninstall` | remove the schedule |

## `qajev browser`

QAJev's own Chrome.

```bash
qajev browser login --profile shop --url https://shop.example/login   # a window opens: sign in yourself, once
qajev browser status                                                 # QAJev's running Chromes
qajev browser start --profile shop                                   # start one and leave it running
qajev browser stop --profile shop
qajev browser reap                                                   # clean up after runs that died
```

## `qajev doctor`

Checks your keys (present and valid, never printed), Chrome, a free port and the machine's load. `--offline`
skips the key validity check.

## `qajev init`

```bash
qajev init qajev.yaml     # a starter suite to edit
```

## `qajev mcp`

Runs the MCP server over stdio, for AI agents. See [MCP](mcp.md). `--allow-commands` lets agents run suites that
contain shell commands (off by default).
