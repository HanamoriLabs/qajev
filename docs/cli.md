# CLI reference

Every command, what it is for, and its options. `qajev <command> --help` prints the same from the tool itself.

| Command | For |
|---|---|
| [`smoke`](#qajev-smoke) | crawl a site for free and list what is broken |
| [`check`](#qajev-check) | one scenario: a goal and expectations on a URL |
| [`run`](#qajev-run) | a suite file, or a project's objectives |
| [`play`](#qajev-play) | test a game or a mobile app |
| [`plan`](#qajev-plan) | a suite's test plan, and the tests that do not say what they prove, without running it |
| [`report`](#qajev-report) | print a run's report again |
| [`projects`](#qajev-projects), [`reports`](#qajev-reports) | known projects; their recent runs |
| [`jobs`](#qajev-jobs), [`stop`](#qajev-stop), [`rerun`](#qajev-rerun), [`top`](#qajev-top), [`dashboard`](#qajev-dashboard) | follow, stop, rerun and watch runs |
| [`nightly`](#qajev-nightly) | every project, every night, notified only on change |
| [`browser`](#qajev-browser) | QAJev's own Chrome, and signing in |
| [`account`](#qajev-account), [`secret`](#qajev-secret) | check a seeded test account, or a `seed:` reference |
| [`doctor`](#qajev-doctor) | check the setup |
| [`init`](#qajev-init) | write a starter suite |
| [`mcp`](#qajev-mcp) | run the MCP server for AI agents |

## Exit codes

| Code | Means |
|---|---|
| `0` | PASS |
| `1` | FAIL: the product is wrong somewhere |
| `2` | INCOMPLETE: something could not be judged |
| `3` | a mistake in the suite or the options |
| `4` | no browser available (or the queue wait ran out); for a cloud run: the dashboard did not answer, an upload was refused, or the wait ran out |
| `5` | refused: the run would change a production site (`mode: mutate` or `--allow-destructive` on a host that is not a local dev host), or a play suite asked for `mode: mutate`. Nothing was started; `--json` prints `{"outcome": "refused", "reason": ...}` |
| `130` | stopped (Ctrl-C or `qajev stop`) |

## Options shared by `check`, `run` and `smoke`

**Browser**

| Option | Does |
|---|---|
| `--headless` | run Chrome with no window |
| `--ephemeral` | a throwaway browser profile, deleted after the run (the report says "throwaway profile") |
| `--profile NAME` | a named QAJev browser profile (sign in to it once with `qajev browser login`) |
| `--cdp-url URL` | attach to a Chrome you started yourself instead of QAJev's |
| `--real-devices ios,android` | also run each website scenario in a real device browser (opt-in; see [Mobile](mobile.md)) |
| `--devices LIST` | the devices every website test runs on (default `desktop,phone`; `--device` pins one) |
| `--motion reduce\|full` | `reduce` (default): pages are told the visitor prefers less motion, so busy animations stop changing under Jev; `full`: as a normal browser |
| `--cpu-throttle N` | run the page's CPU N times slower (1 to 8; default the suite's, else 1), e.g. 4 for a phone-like phone pass; the report says so under Run |

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
| `--env-file FILE` | where your keys are (default `$QAJEV_ENV_FILE`, then `~/.qajev/.env`; never the current folder's `.env`) |
| `--jev-provider auto\|typesafe\|openrouter\|cloudflare` | who makes the decisions: Jev (TypeSafe, OpenRouter) or Clef (Cloudflare; see [Configuration](configuration.md)) |
| `--usd-per-call USD` | the estimated TypeSafe cost per decision, for the ledger |
| `--strict` | count `stuck` scenarios as failures |

**Where it runs** (also on `play`: `--runner` only; see [QAJev Cloud](#qajev-cloud-where-a-run-goes))

| Option | Does |
|---|---|
| `--runner local\|cloud\|auto` | `local`: this Mac, as today. `cloud`: QAJev Cloud; a run that cannot go is refused, never run here. `auto`: the cloud when it can, else this Mac, with one line saying why. Default: `$QAJEV_RUNNER` (the shell, then `~/.qajev/.env`), then the project's `runner`, else `local` |
| `--serve DIR[:PORT]` | cloud runs: ship this built site folder; the cloud machine serves it on its own `127.0.0.1:PORT` (default: the port the run opens, else 8877) |
| `--model clef\|clef-flash\|clef-omni\|jev\|jev-openrouter` | cloud runs: the model that decides (default: the dashboard's) |

**Busy machines**

| Option | Does |
|---|---|
| `--load-high N` | wait before a scenario (`smoke` and `play`: before starting) while the 1-minute load is at or above N (0 = never wait; default 150) |
| `--load-ok N` | resume once the load drops below N (default 100) |
| `--load-wait SECONDS` | total waiting allowed per run (default 600) |

## `qajev smoke`

Crawl the pages of a site (same host only) with **no model calls**, and lint each one: HTTP status, script errors,
Content Security Policy blocks and report-only violations, broken images, a blank page, missing title or description, accessibility basics, layout shifts and more.

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
| `--no-ux` | skip the UX measurements (on by default and free; see [Reports](reports.md#ux)) |

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
| `--visible` | this must be on screen, whole and on top: not covered or cut short (a failure says which); repeatable |
| `--expect-url`, `-u` / `--expect-url-regex` | the final address contains this / matches this |
| `--expect-js`, `-j` | a JavaScript expression that must be true |
| `--fetch URL[=STATUS]` | a request from the page that must answer STATUS (default 200) |
| `--expect-looks STATEMENT` | Clef, looking at the final screenshot, must judge this true (repeatable; needs Clef) |
| `--vision` | Clef sees the screenshot with every decision (needs Clef) |
| `--mode readonly\|mutate` | `mutate` lets Jev change data, on a local dev host only (`localhost`, `127.0.0.1`, `[::1]`, `*.localhost`, `*.test`); any other host is refused, exit 5 |
| `--allow-destructive` | show delete, remove, refund, cancel... controls, hidden by default on every host; a local dev host only (else exit 5); the report carries a red banner |
| `--device NAME` | `desktop`, `tall`, `phone`, `tablet` or `WIDTHxHEIGHT` |
| `--persona TEXT` | who Jev is, e.g. "You are on your phone and new to this site" |
| `--about TEXT` | what this test proves and why, in plain words; shown with its result in the report and dashboard |
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
| `--allow-destructive` | as for `check`: destructive controls shown, every host a local dev host or the run is refused (exit 5) |
| `--project`, `-p` | a project name, or a repository path with `.qajev/project.toml` |
| `--env`, `-e` | the project environment (default: the project's `default_env`) |
| `--suite TAG` | the objectives with this tag (repeatable) |
| `--name NAME` | one stored objective |
| `--objective TEXT` | an ad-hoc goal instead of the stored objectives, with `--url`, `--expect-*` and `--about` |

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
| `--about TEXT` | what the test proves and why (with `--suite`: the whole run; steps carry their own `about:`) |
| `--suite FILE` | several steps in one game session: goal steps, real-time play steps and idle steps (see [Games](games.md)) |
| `--only STEP` | run this step of the `--suite` (repeatable), plus the steps it names in `depends_on` or `judged_by` and every `setup: true` step |
| `--adapter NAME\|PATH` | the game's adapter (bundled name, or a `.gd` / `.js` file) |
| `--expect-screen NAME` | the screen the game must be on at the end |
| `--expect-text TEXT` | the game must show this (repeatable) |
| `--expect-state KEY=VALUE` | a game state value, e.g. `game_over=false` or `kills=">= 1"` (repeatable) |
| `--min-fps N` | the frame rate must be at least this |
| `--expect-looks STATEMENT` | Clef, looking at the game's final screenshot, must judge this true (repeatable; needs Clef and a window) |
| `--vision` / `--no-vision` | Clef sees the game's screen with every decision. On by default with Clef and a window; `--vision` refuses to run without them, `--no-vision` uses the labels only |
| `--allow-errors` | engine or script errors do not fail the run |
| `--expect-closed` | the game must quit by itself with exit code 0 (to test a normal quit) |
| `--allow LABEL` | offer this exact label to Jev although it is hidden by default, e.g. `QUIT` (repeatable) |
| `--hide LABEL` | never offer this exact label to Jev (repeatable) |
| `--game-env KEY=VALUE` | an environment setting for the game (repeatable) |
| `--game-arg ARG` | a switch for an Electron app, e.g. `--game-arg=--fullscreen` (repeatable) |
| `--game-profile NAME` | keep an Electron game's save folder between runs, in `~/.qajev/game-profiles/NAME` (a test profile; [Games](games.md#a-save-kept-between-runs-electron)) |
| `--reset-game-profile` | empty that kept profile before this run |
| `--device NAME` | mobile: the iOS simulator to clone, or the Android virtual device to boot |
| `--install FILE` | mobile: an `.apk` or simulator `.app` to install on the throwaway device first |
| `--headless` | Godot: no window, fastest, no screenshots. Electron apps always open a window and keep their screenshots |
| `--name` | the test's name, shown with the game's in `qajev jobs` and `qajev top` |
| `--max-actions`, `--max-seconds`, `--no-shots`, `--out`, `--cost-cap`, `--load-high`, `--load-ok`, `--load-wait`, `--json`, `--events`, `--quiet`, `--background` | as above |

## `qajev plan`

A suite's test plan, without running anything (no browser, no cost): each test's `about` and its checks in plain
words, and the tests that are NOT DESCRIBED. Use it as a lint before you run a suite or send a pull request.

```bash
qajev plan shop.qajev.yaml            # a website suite (scenarios:)
qajev plan qa/plans/boss.suite.yaml   # a game's steps suite (steps:)
qajev plan shop.qajev.yaml --json     # {name, about, plan, not_described}
```

It exits `0` when every test says what it proves and `2` while any is NOT DESCRIBED (a run of it would be
INCOMPLETE, never PASS). `qa_plan` is the same for agents.

It also checks the suite's **approved test plan** (`plan:`, see [Writing tests](writing-tests.md#an-approved-test-plan)):
the line `Plan file: ...` says whether it is approved, and the JSON has `test_plan` (`path`, `approved`, `approval`,
`sha256`, `problem`). A plan that is missing or not approved is listed, and the exit code stays `0`. With
`QAJEV_REQUIRE_PLAN=1` it exits `2`.

## `qajev plan-hash`

Prints the sha256 an approver puts on a test plan's approval line, from the plan's text as it stands:

```bash
qajev plan-hash tools/qa/plans/2026-10-08-checkout.md   # sha256:3f1c...
```

The approver then adds, as the last line of the plan:

```text
Approved by the Orchestrator 8 Oct 2026 10:34 sha256:3f1c...
```

## The approved test plan on a run

`qajev run`, `check`, `smoke` and `play` take `--plan FILE`: the approved test plan this run follows. It overrides a
suite's `plan:`. A run without an approved plan prints `qajev: warning: no approved test plan: ...` and runs; with
`QAJEV_REQUIRE_PLAN=1` it is refused before the queue and before Chrome (exit `3`, "needs an approved test plan"). The
report shows the plan's path and its approval line, or a banner "No approved test plan". `qajev doctor` says which
rule is on.

## QAJev Cloud: where a run goes

`--runner cloud` sends a `check`, `run` (a suite or a project) or `smoke` to QAJev Cloud (cloud.qajev.com) instead of
this Mac. A cloud machine runs it with a headless, throwaway Chrome.

```bash
qajev check https://shop.example/pricing -g "Find the Pro price. Stop when it shows." -t '$29' --runner cloud
qajev run suites/shop.yaml --runner cloud --model clef --json
qajev run suites/local.yaml --runner cloud --serve dist:5173     # the suite opens http://127.0.0.1:5173
QAJEV_RUNNER=auto qajev smoke https://shop.example/
```

- **Switch with `qajev mode`.** `qajev mode cloud` sends this Mac's next runs to the cloud. `qajev mode local` keeps
  them here, and the cloud gets nothing new (its warm machines go off when idle). `qajev mode auto` uses the cloud when
  it can. `qajev mode dashboard` follows the dashboard's Settings page, "Where runs go by default", so a person can
  switch every agent on this Mac from the dashboard (QAJev looks once a minute). `qajev mode` alone says where runs go
  and why. It writes `QAJEV_RUNNER` in `~/.qajev/.env`; a `QAJEV_RUNNER` set in the shell still wins, and it warns.
- **One project.** `qajev mode cloud --project shop` writes `runner = "cloud"` in that project's `project.toml`;
  `qajev mode default --project shop` removes it. The order: `--runner`, then the project's `runner`, then this
  Mac's mode, else this Mac.

- **The key.** `QAJEV_CLOUD_KEY`, else macOS Keychain (service `qajev-cloud-agent`). QAJev never prints it.
  `QAJEV_CLOUD_URL` names another dashboard (default `https://cloud.qajev.com`). `qajev doctor` checks both.
- **What it prints.** The run's address at once, its progress, then each test's outcome and reason, the gate and
  the report's address. `--json` prints one object: `{"runner": "cloud", "run_id", "url", "gate", "counts",
  "tests": [{"name", "outcome", "about", "reason", "wrong"}], "report_url", "reason"}`. The exit code is as for a
  run here: `0` only on PASS.
- **Why a queued run waits.** While a run cannot start, its progress line says why, in the dashboard's words: cloud
  machines are off, today's cloud minutes are used up, the month's model spend reached its limit, or the account has
  too few credits (with what the run needs, the balance and where to buy). Otherwise it says `waiting for a cloud
  machine`. `qajev jobs r_...` and its `--json` (`"waiting": {"reason", "text"}`) show the same.
- **`--background`** returns the cloud run's id (`r_...`) and address at once. `qajev jobs r_...` shows its state;
  `qajev stop r_...` stops it.
- **What stays on this Mac.** `--runner cloud` refuses these, with the reason and exit `3`, and runs nothing here.
  `--runner auto` runs them here, with one line saying why:
  - a signed-in run: `--profile`, or a suite or project that signs in with an account;
  - a run that writes to a dev server on this Mac (`--mode mutate`, or a suite with `mode: mutate`): the tunnel to
    this Mac is read-only;
  - a target on this Mac the tunnel cannot carry: `::1` or a `*.localhost` name, a port below 1024, or two ports in
    one run. A read-only run on one `127.0.0.1` or `localhost` port goes to the cloud only when the command itself
    says `--runner cloud`: the dispatcher on this Mac then opens a read-only tunnel to that port for the run (only
    GET, HEAD and OPTIONS pass; a page's POST or WebSocket does not). With `auto`, or a mode of `cloud`, it stays here
    or stops with the reason. Or ship the built site with `--serve DIR[:PORT]`. The folder must be a clean build
    (`git archive`): one holding an env file (`.env`, `.dev.vars`), a key (`*.pem`, `*.key`, `id_ed25519`),
    credentials JSON, `.npmrc` or a `.git` folder is refused with their names;
  - a held run: its suite path, project or target holds a word from `QAJEV_CLOUD_HOLD` (comma-separated, any case,
    for example `figgyz,verse`). It goes to the cloud only when the command says `--runner cloud`;
  - `play` (games, native and mobile apps), `--real-devices`, `--cdp-url` and `--allow-commands`.
- **A cloud failure never runs here instead**, also with `auto`: no key, no answer from the dashboard (exit `3` or
  `4`), a refused upload (QAJev stops that run), a run stopped on the dashboard, or a wait that runs out
  (`QAJEV_CLOUD_WAIT`, default 2 hours; the run goes on in the cloud). Each says why, with the run's address when it
  has one.
- The suite file, a project's `.toml` and the `--plan` file go with the run. A folder of more than 150 files, or
  with a file over 45 MB, ships as tar parts (at most 500 MB in all).

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
qajev jobs r_01k7c2...          # a cloud run: its state and tests from QAJev Cloud
```

## `qajev stop`

```bash
qajev stop 20261001-101502-a3f9
qajev stop r_01k7c2...          # a cloud run: QAJev Cloud stops it
```

The run closes its browser, keeps the scenarios that finished and writes its report (INCOMPLETE). QAJev signals
the run only when its recorded process still matches (start time and command), so a process id another program now
uses is left alone ("stale pid, not ours"). A job recorded before 0.4.0 cannot be proved that way: `stop` refuses
it and says how to stop it by hand.

## `qajev rerun`

Run a finished job again with the same settings: a check, a suite or project run, or a game session.

```bash
qajev rerun 20261003-055116-afb3            # the same command again
qajev rerun 20261003-055116-afb3 --failed   # only its tests that failed, got stuck or hit a harness limit
```

`--failed` reruns each of those tests with `--only`, so a test still brings what it depends on (a game step, its
`setup: true` steps). The rerun is a new job; `--background`, `--json` and `--quiet` work as for any run. A check is
one test, so it runs again as it was. It runs in the folder its job ran in, so relative paths mean the same files,
and it reads the suite file as it is now. Anything the command or suite points at (a game's save folder, a seeded
file) is as the first run left it: a rerun starts from a fresh save only if the plan makes one. In `qajev jobs` the
new job's title ends "rerun of JOB" (", failed only" with `--failed`). From MCP: `qa_rerun(job)`.

## `qajev top`

The live dashboard. `--once` prints one snapshot; `--json` prints it as data. `--decisions JOB` opens one job's
decisions as they are made (what the model chose, how sure, the runner-up; `d` in the dashboard), or prints them
with `--once` / `--json`. See [Jobs and top](jobs-and-top.md).

## `qajev dashboard`

Every run in a local web page: filter by project, open each test with its screenshots and decisions, stop, rerun
or start runs. `--background` runs it detached (it outlives the terminal), `--open` opens it in the browser,
`--stop` stops it, `--port` sets the first port to try (default 8790), `--json` prints its address as data,
`--new-key` starts it with a new key. It serves 127.0.0.1 only; the address it prints carries its key, kept across
restarts. See [The dashboard](dashboard.md).

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
qajev browser status                                                 # QAJev's running Chromes (a throwaway one is marked)
qajev browser start --profile shop                                   # start one and leave it running
qajev browser stop --profile shop
qajev browser reap                                                   # clean up after runs that died
```

## `qajev account`

Checks a project's test account by signing in with it (see [Signed-in areas](writing-tests.md#signed-in-areas)).

```bash
qajev account check qa-test --project shop
```

`check` signs in once, in a throwaway Chrome (`--visible` to watch), and says `ok: signed in as ...` or the site's
reason. Since 0.4.0 that is only a seeded test account on a local dev host (`password: seed:FILE#KEY`). Exit code 2
when the sign-in fails, 3 for a mistake in the options or the project.

`add` is refused since 0.4.0: QAJev no longer saves an account's password. Put a seeded local test account in the
suite or project as `password: seed:FILE#KEY`; for any other account, sign in once yourself with
`qajev browser login --url <sign-in page>`.

## `qajev secret`

Says whether QAJev can read a reference (see [Signed-in areas](writing-tests.md#signed-in-areas)). It never prints
the value.

```bash
qajev secret check seed:dev_support/qa_test_user.json#password   # ok: ... can be read (14 characters)
```

Exit code 2 when the reference cannot be read, with the reason. `set` is refused since 0.4.0 (exit 2): QAJev no
longer stores a password; sign in once yourself with `qajev browser login --url <sign-in page>`.

## `qajev doctor`

Checks your keys (present and valid, never printed), Chrome, a free port and the machine's load, and prints the
QAJev version with the commit it runs from (`+dirty` when its code has uncommitted changes) and the production rule
with the hosts that count as local dev hosts. Its `cloud` line says whether the QAJev Cloud key is found (never its
value), the default runner and where it came from, and whether the dashboard answers. `--offline` skips the key
validity check and the dashboard check.

## `qajev init`

```bash
qajev init qajev.yaml     # a starter suite to edit
```

## `qajev mcp`

Runs the MCP server over stdio, for AI agents. See [MCP](mcp.md). `--allow-commands` lets agents run suites that
contain shell commands (off by default).
