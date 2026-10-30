# Changelog

All notable changes to QAJev. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## 0.4.34: 30 Oct 2026

### Added

- `ux_settle_timeout_ms` (CLI `--ux-settle-timeout-ms`) caps each UX quiet wait for
  continuously updating pages. The default stays 10 seconds, every UX scan still
  runs, and an expired wait records "page did not settle".

- An opt-in registered-machine observer snapshots selected local jobs and approved report roots into a durable
  outbox, then publishes tests and evidence idempotently. Human-placed machine credentials are observation-only;
  registration requires the companion Cloud API. No model route, execution agent or service is enabled by default.
  `--project-for FOLDER=PROJECT` (repeatable) names each job's project by the folder it ran in, for a machine that
  serves several projects. A job no mapped folder holds gets `--project`, or is skipped and counted as `no_project`.
- Configurable local browser-run concurrency (`--browser-runs` / `QAJEV_BROWSER_RUNS`) with FIFO queueing,
  RAM admission for N above 1, isolated throwaway Chrome resources and unique report folders. Serial N=1
  admission stays unchanged. Job results and reports
  separate queue and run time; native actions, real devices and external Chrome remain exclusive.

### Fixed

- Inventory outgoing diagnostic tails and reject unclassified cuts. Dashboard-start and nightly errors clean
  complete output before shortening it, including JSON that can be captured by a job wrapper.
- Clean generated command, page-check and mobile failure diagnostics before retaining a character tail, so a
  cut through a home path cannot expose its username. Command matching and declared cloud inputs stay exact.
- Linux parallel-browser admission now respects every ancestor cgroup v2 memory limit as well as the host's
  available memory. Sibling scopes consuming a shared parent budget can no longer be overlooked when deciding
  whether another run fits. Concurrency defaults and configured RAM budgets stay unchanged.
- Managed Chrome now starts with a fixed runtime environment allowlist, excluding model credentials and arbitrary
  application variables in serial and parallel jobs. Existing profiles without the isolation marker require a
  browser restart. Command-hook behavior and its `--allow-commands` gate are unchanged.
  The restart refusal names the profile's `qajev browser stop --profile=NAME` command. The whole option is
  shell-quoted when needed, preserving leading dashes and shell syntax in the profile name.
  On Linux, `http_proxy`, `HTTP_PROXY`, `https_proxy`, `HTTPS_PROXY`, `no_proxy`, `NO_PROXY`, `all_proxy`,
  `ALL_PROXY` and `LD_LIBRARY_PATH` no longer reach managed Chrome;
  proxy URLs can contain passwords. Use `QAJEV_CHROME_FLAGS=--proxy-server=http://127.0.0.1:8080` for a
  proxy without credentials.
- Spawned scenario workers can restore a trusted provider integration before executing checks. The companion
  Cloud runner uses this to preserve dashboard model routing with `--jobs` above one, without putting a job token
  in worker arguments or reports.
- Planted-fault guidance names the actual `graded: "pass"` and `graded: "fail"` values in CLI help, MCP,
  reports and documentation. Evaluation and report grades are unchanged.
- Chrome web scenarios measure page geometry and an actual capture before checks or hooks, including when
  screenshots are not retained. A mismatch is harness rather than a scripted pass. Reports keep layout size,
  visual viewport, DPR and capture pixels; mobile layout zoom and declared device scale are accounted for.
  Saved, reactive and vision frames also validate their own capture-time geometry. A later mismatch remains
  harness even after passing checks, and rejected images are not retained or sent to the vision judge.
- Headless mouse presses activate their receiving tab before input, including direct Jev actions. Background
  canvases can acquire real pointer lock after a trusted press instead of reporting `WrongDocumentError` while
  focus emulation says the page is focused. Permission denials and headed-window focus policy remain unchanged.
- Parallel CLI admission charges only the next browser against remaining RAM, while retaining startup
  reservations across processes. A resident browser no longer makes an otherwise affordable second run queue.
  Declared budgets and the largest active reserve remain protected; default serial and native admission stay exclusive.
- A lost CDP or browser-daemon transport stops reuse of that connection. The affected scenario remains
  harness; remaining scenarios are explicitly not run, with no automatic replay of hooks or actions.
  Target/session command rejections from a closed popup do not invalidate a usable browser transport.
  Daemon replies reporting `cdp_disconnected` or `Client is stopping` also stop transport reuse.
- Managed Chrome disables background update, network-time, search-eligibility, on-device model, dictionary
  and speculative preconnection requests. Cloud messaging endpoints cannot leave managed profiles' machine.
  Additional disabled features preserve the managed defaults, and older running profiles require a restart.
  Chromium's single-dash switches and quoted whitespace cannot bypass the messaging or feature policy,
  or override parallel managed launches' profile and debug port.
- Linux viewport captures activate the test tab before readback to avoid long waits on inactive or minimized
  surfaces. Captures keep their native pixels, requested crops, individual frame files and measured timing.
- Browser diagnostics above 1 MiB become visible omission markers before CDP transfer. Normal text remains
  whole for Python privacy cleaning; oversized measured JS results keep their original boolean verdict.
- Page console log/info measurements join warnings and errors in scenario reports, with the same privacy,
  retention limits and navigation handling. Warnings and errors replace routine lines in a full buffer.
  Console output does not add a new pass/fail rule.
- JavaScript checks retain returned values and failure reasons up to 64 KiB per item, with visible cut counts
  above that limit. Oversized measurement objects show a truncation marker. Explicit `{ok, says, evidence}` results
  attach measured numbers to the report, and each scenario keeps bounded page warnings/errors with visible
  omission counts. Measurement data is escaped and never treated as image paths.
- Managed Chrome profiles disable password saving, automatic sign-in and online password leak checks before
  launch, while preserving unrelated preferences. Older running profiles require a restart before reuse.
- Device authorization preserves only its opaque device_code field; other token-request metadata follows
  the same default privacy rule as every other metadata request.
- Search inputs and explicit labels or placeholders beginning with "Search" keep text keyboards for queries
  such as card names and 151. Numeric fees, filters and fields near search headings still receive findings.
- Cloud clients clean metadata on every route by default, including raw JSON. Named file/model/protocol inputs
  and run arguments stay exact. An inventory test requires a privacy classification for every production route.
- Quoted local paths in generated reports and metadata keep their spaces together even inside another pair
  of quotes or backticks, so a path's trailing file name no longer survives the privacy cleaner.
- Dispatcher machine status, heartbeat and claim metadata use the shared report-path cleaner. Tunnel-refusal
  reasons hide restored local paths while preserving the explanation; declared run inputs stay unchanged.
- A lost reply for the last declared cloud input is confirmed with one exact-file read if the retried upload
  receives 403 or 409 after dispatch starts. Matching bytes let the run continue. An initial refusal or a missing,
  unreadable or different file still fails; run creation and stop requests are never retried.
- Cloud submissions keep declared suites, served assets, archive parts and check arguments byte-for-byte.
  Filesystem-path cleanup applies to QAJev's reports, logs and ownership/tunnel metadata. It does not rewrite
  JavaScript routes, regular expressions, CSS URLs or expected text in the submitted inputs. Unquoted paths in
  report prose stop at whitespace, so the following explanation and independent paths are preserved.
- A failed `command` check shows the end of its output: the last 400 characters of stderr, or of stdout when stderr
  is empty. It showed the first 200 characters of stderr only, so a gate that writes its failed step to stdout (a
  JSON report) failed as "exit 1:" with no reason. A command's output is redacted before any cut, so a secret
  that crosses the cut no longer leaves its tail in the report.
- Headless Chrome, as in QAJev Cloud, says it is a normal Chrome. Its user agent said `HeadlessChrome/155`, and some
  sites refuse a headless browser. Now it says `Chrome/155` with `QAJev/<version>` on the end, and its client hints
  name "Google Chrome", not "HeadlessChrome". QAJev itself still knows the browser is headless.

## 0.4.33: 14 Oct 2026

### Changed

- QAJev's Chrome says it is QAJev. Every page it drives sends the browser's own user agent with `QAJev/<version>`
  on the end, from its first request, in its dedicated workers and on a phone too. A page that counts a person's visits
  can now skip QAJev's. On a desktop, the browser's own user-agent client hints stay as they were when QAJev can read
  them. When it cannot, the tab sends no client hints.

### Fixed

- A frame on another site that QAJev could not guard, and whose document had not loaded yet, is named by its iframe's
  `src`. Before, the harness reason read "the frame on another site ()", and a reviewed `unguarded_frames` host could
  never match it (a plaza screen's starting video, 14 Oct). A frame with no web `src` either is still not proven
  read-only: it is called "a frame with no address yet".

## 0.4.32: 14 Oct 2026

### Fixed

- A goal whose checks already held before Jev moved no longer ends "reached" before Jev's first action. A sign-in
  goal ended in 5 s with 0 decisions, because its `url` check matched the sign-in page's `?next=/workshop`. Jev now
  acts at least once first, and if it takes no action, the run is harness: the checks say nothing about the goal.
- QAJev's own child processes (the MCP server's runs, background jobs, nightly, the dashboard, open_url's page run)
  run the installed QAJev, never a `qajev/` folder in the working folder. A session started in an old checkout ran
  that checkout's 0.1.0, which refused `--says`. They start Python with `-P`.
- A native app's throwaway home, with any `app.files` token in it, is removed by the next run's reaper even when the
  killed run's pid now belongs to another process. Each home records when its run started, so a reused pid no longer
  keeps it.

## 0.4.31: 14 Oct 2026

### Added

- A bridge key for a native app: `app.bridge_key: true` gives the app a new `QAJEV_BRIDGE_KEY` each launch, and
  QAJev sends it as the bridge's first line, so only that run can drive the app (Twinbox Desktop's bridge needs it).
- A test token for a native app: `app.files` writes a seeded TEST account's value into the app's throwaway home
  before it starts, such as Twinbox's staging token file. The value comes only from a `"test_account": true`
  fixture whose `account` is at one of `app.test_domains`, and only under an approved test plan that names
  app.files. A suite may only narrow the test domains: names reserved for testing, or ones listed in
  `QAJEV_TEST_DOMAINS` in `~/.qajev/.env`. The file is 0600, never written through a symbolic link, and goes with
  the home, on a crash or a stop too. The report shows only the file and its fixture. QAJev's browser never gets it.

### Fixed

- Play's event stream (`--events` and a job's events.jsonl) now redacts secrets as the report does.
- An env file (`--env-file`, `$QAJEV_ENV_FILE`, `~/.qajev/.env`) can no longer set `QAJEV_HOME`, `QAJEV_ENV_FILE` or
  `QAJEV_TEST_DOMAINS`. Before, one that set `QAJEV_HOME` pointed every child process at another folder's `.env`.

## 0.4.30: 14 Oct 2026

### Added

- A page the app opened, checked in QAJev's Chrome: a play suite's `open_url:` step reads the URL from the app's state
  (`from`), fails and opens nothing unless it matches `url_regex` (`^https://<host>/...`, the host written out) and
  is on that host, then runs the page as one scenario of `qajev run` on the same machine, with a suite's `expect`
  and `guard`. `{last}` in `guard.allow_requests` (each pattern one host, written out) is the URL's own last path
  part, escaped, so a Stripe checkout's `/init` POST goes through for that session only. The guard takes nothing
  else, so payment fields stay disabled, and a URL that is not https or holds a user name or password opens nothing.
  Each step's page run writes to a new folder. A page check that cannot judge is harness. The inner run takes the
  browser slot from its parent play run. Twinbox step 21 checks its Stripe test checkout this way.

- Real keys on a QAJev Cloud Mac: a play suite's `keys:` step brings the app (`app`, its bundle id) to the front, can
  put text on the clipboard (`clipboard`) and click one of the app's controls (`click`, at its real screen point from
  the app's `state.window`), then presses keys and chords (`press`, with `{type: text}` for typed text). Then its
  `expect` checks judge what the keys did. The presses are macOS events from the per-run Mac's `qajev-keys` helper,
  only while the app is in front. Anywhere without that trusted helper, this Mac too, the step presses nothing and is
  harness. A suite is refused when it loads if a key is a system shortcut (Cmd+Q, Cmd+Tab, Cmd+Space...), or if the
  app is one of Apple's, a browser, a terminal or a password manager, or not the suite's own app.
  Input pushed through an app's bridge cannot prove a paste, a shortcut or focus (Twinbox step 20a: a box name that
  would not paste).

### Fixed

- A running MCP server now reads `~/.qajev/.env` again before each run. Before, it kept the values the file had when
  the server started, so `qajev mode local` never reached it: servers started before 01:10 on 14 Oct kept sending
  runs to QAJev Cloud after the Mac was set to local. A changed line now wins, and a removed line is gone. A value
  the shell set, or the process changed, still wins over the file.

## 0.4.29: 14 Oct 2026

### Added

- Any native app with a QAJev bridge: a play suite's `app:` names the app's `command`, its `env` and a `home_env`
  that QAJev points at a throwaway folder (`HOME` is throwaway too), so the app never reads the person's real
  settings or tokens. The app speaks the Godot bridge's protocol, so Jev's choices, state checks, `relaunch`, shots
  and `looks` work as for a game. A log line at `ERROR` or `FATAL`, or a Rust panic, fails `no_errors`. `app:` wins
  over the `.app` folder guess, which took any `.app` for an Electron game. The app starts from an allowlist of
  variables (PATH, LANG, LC_*, TERM and a few more), never QAJev's whole environment, so no key reaches it.
  Built for Twinbox Desktop's test suite.
- A handoff step in a play suite: `handoff: {who, does, until, minutes}` hands a step to a person (a GitHub device
  login, a sign-in to their own account) and waits up to 30 minutes, sending no input. It tells the run's watchers
  ("waiting for José: ..."), passes once the app's state shows `until`, and ends **stuck**, never fail, when time
  runs out. QAJev never reads, types or records a code or a password in it.
- Planted faults: a test can say it is meant to fail, and on which check: `expect_fail: "<the check's says>"` on a
  suite's scenario, `--expect-fail` on `qajev check`, `expect_fail` on `qa_check` (`true`: the test's only check).
  It keeps its real outcome. The gate counts it as met only when it failed on that check and every other check
  passed. A pass ("the planted fault was not caught"), or a failure on another check, turns the gate FAIL, and a
  crash or a time-out is never a catch. Reports count the planted tests and tag each one, and report.json carries
  `expected`, `expected_check`, `graded` and `graded_reason`. Before, our own planted tests read as product
  failures and filled QAJev Cloud's Needs a look (the Orchestrator).

### Changed

- A report picture marks where the read-only guard hid a control: a dashed box labelled "hidden by guard" in its
  place. The control itself stays invisible and inert, and the page is put back after the picture. The report lists
  each hidden control with the rule that hid it and its page (`page` is new in `guard_hidden_controls`), from every
  page of the test, not only the last. A missing "Save" button read as a page bug twice (13 Oct).
- A suite sent with `--runner cloud` is now filed under its project on QAJev Cloud: the one in its repo's
  `.qajev/project.toml`, else the project whose `repo` holds the suite. Before, only a `--project` run named one, so
  the run's page could not say which project it belonged to (José).
- Every cloud run now names its project when one is known, in this order: `--project`, the suite's project, the
  folder the run starts from (its `.qajev/project.toml`, else the project whose `repo` holds it), then the one
  registered project with an environment on the URL's host and port. A check or smoke of a URL named none before, so
  all 435 cloud runs this month showed "No project" on the Usage page (the Orchestrator).

## 0.4.28: 13 Oct 2026

### Changed

- `qajev check`, `smoke`, `run` (suites and projects) and the MCP tools refuse a URL that holds a user name or password
  (`https://alice:s3cret@shop.example/`, with or without the scheme), anywhere in a run's arguments or suite. They
  refuse it before Chrome starts and before a job record or a cloud run keeps it. The message says to sign in once
  with `qajev browser login --url URL` and run with that profile. Before, the password reached report.json and
  report.html, which a cloud run uploads, and a smoke's report folder name (redteam).
- `qajev check` and `qa_check` name their test by the site and path, plus the goal when there is one:
  "qajev.com /pricing · Find the Pro price", not "check". QAJev Cloud's Needs a look counts failures per project and
  test, so a test named "check" merged every check of a site into one problem (the Orchestrator). The phone copy keeps
  its " (phone)" ending.
- `qajev plan` and `qa_plan` flag a website test whose only checks say a text is absent: a blank page passes it, so it
  proves nothing, even with a `url` or `status` check. The plan lists it as ABSENT ONLY (`absent_only` in the JSON)
  and exits 2. It reads each test's own `expect`, not a steps suite's or a client's. A Foley step that checked
  only that "Sign in" was absent passed on a blank page in 0.89 s and briefly looked like a product bug (the Orchestrator).

- When Jev says DONE after 0 actions and a check was already false before Jev moved, the run stays harness, but the
  reason now names the check, says it was false before Jev moved, and says how to get a FAIL: run the check without a
  goal (the Orchestrator).
- A failed sign-in names the requests that failed after it began, by host and path, with Chrome's reason (for example
  "requests that failed: www.google.com/recaptcha/api.js (net::ERR_NAME_NOT_RESOLVED)"). Before, a seeded test user
  that was "not signed in after 20 s" on a QAJev Cloud machine said nothing about why.

## 0.4.27: 13 Oct 2026

### Fixed

- The guard hid a to-do row's own title when it held a danger word, such as the task "Buy paint". In a list item with
  a checkbox, the control whose every name (text, aria-label, title) equals the checkbox's label is now shown, unless
  it reads as a purchase ("Buy now", "Checkout", "Subscribe", a price in any common currency). On production a
  destructive title stays hidden. The row's other controls, its Delete included, keep the usual rules (Foley;
  redteam).
- The guard missed a control's name when it came from `aria-labelledby` or from a child (an image's alt, a child's
  aria-label, title or `aria-labelledby`, an inline svg's title), so a "Delete" or "Pay" named that way stayed
  visible, on production too. Every name now counts, including the names inside the element `aria-labelledby` points
  to. A long caption no longer hides the aria-label. A control is judged again when that element changes or arrives
  after the control (redteam).

## 0.4.26: 13 Oct 2026

### Added

- `login.tab: same` signs a seeded account in in the scenarios' own tab, not in a tab of QAJev's own that then closes.
  For an app that keeps one writer per site (Foley's browser database); the default stays `own` (foleyapp).
- A web worker's console errors, warnings and uncaught exceptions are findings: `worker error` (S2) and
  `worker warning` (S3), each naming the worker's script. Before, only the page's own console was read, and an app
  whose database runs in a worker hid its errors (foleyapp).

### Fixed

- The guard hid controls whose label holds drag-and-drop wording ("drag to move. Drop a task here …") as destructive,
  because of the word "drop". Drag-and-drop phrasing is now set aside like a harmless reset; "Drop table", "Drop all"
  and a control that names data stay hidden (Foley).

### Changed

- `qajev doctor` on a fresh install is not a failure: with no model key yet it exits `0` and ends with the next steps
  (a key in `~/.qajev/.env`, or `qajev mode cloud`). An optional key that is not set shows `--`, not `ok`. It no
  longer names an internal approver role. `--json` adds `ready`, `next` and each check's `level` (new-user test).

## 0.4.25: 13 Oct 2026

### Fixed

- A seeded sign-in failed with "no element matches [data-qajev-signin=…]" when the page drew a field again between
  QAJev marking it and finding it (Foley's panel, after "Already have an account? Sign in"). It now marks and finds
  again, up to 3 times.
- A browser that kept a session made a seeded sign-in fail: the app showed its sign-in door for a moment, then its
  signed-in view, so the `login.open` steps found nothing to click. A page that turns signed in while QAJev waits for a
  step now counts as signed in (Foley).

### Added

- `qajev report r_...` and `qa_report("r_...")` read a QAJev Cloud run's report with this machine's cloud key, and keep
  it with the screenshots its report names in `~/.qajev/cloud-reports/<id>/`. An agent no longer needs the
  dashboard's sign-in for its own run's steps and last screen (dash-wholesale-links).

### Changed

- A hook whose selector matches a guarded control first now says how many elements it matches and asks for a narrower
  selector, instead of only "the guard hid this control (danger: Sign out)", which read as if the test tried to sign
  out (dash-wholesale-links).

## 0.4.24: 13 Oct 2026

### Fixed

- `login.open` stopped at the first step that showed the email field, so a panel that opens on "Create account" was
  submitted as a sign-up. Now every step is clicked when its control shows, and one that never shows while the email
  field is there is skipped (foleyapp).

## 0.4.23: 13 Oct 2026

### Added

- `login.open` takes a list of steps, clicked in order until the email field shows, and a step can be
  `{text: "Sign in or create account"}`: a button found by its visible text, for a control no CSS selector tells apart
  (foleyapp).

## 0.4.22: 13 Oct 2026

### Fixed

- A seeded sign-in with `login.open` looked for the button once, at once. An app that checks the session first draws it
  later, so the sign-in failed. Now QAJev waits up to 10 s for the button or the email field (foleyapp).
- A failed sign-in now says where the browser was and the start of what the page showed, so it can be read without a
  second run.

### Changed

- A cloud run that waits for credits is followed for 1 minute, not 2 hours. Then QAJev says why, with the run id and
  `qajev stop r_...`, and returns with exit `4`, also with `--quiet` and `--json`, which printed nothing before. The run
  stays queued until a top-up, and QAJev Cloud ends it after 2 hours with no charge (the Orchestrator).

## 0.4.21: 13 Oct 2026

### Fixed

- `seed:FILE#KEY` reads a dotted KEY as a path into nested objects, such as `#A.password` in a fixture with one object per
  test user. Before, only top-level keys worked, so a cloud run stopped at "the fixture has no text value for 'A.email'"
  (foleyapp).

## 0.4.20: 13 Oct 2026

### Added

- `login.open` in an account: the button QAJev clicks first to show the sign-in form, such as "Sign in with email"
  (foleyapp).

### Fixed

- A `fill` hook did nothing on a date, time or month field, and said nothing, so the next step timed out instead.
  Now QAJev sets such a field's value as a person's edit does, and any fill whose text never reaches its field fails,
  saying so (dash-wholesale-links).
- A seeded sign-in typed before the page's scripts had run. On a dev server that builds the page on the first request,
  they cleared the fields and the form sent nothing ("not signed in after 20 s"). Now QAJev waits for the page to load,
  checks the fields before it submits, types them again once, and else says the page cleared them
  (dash-wholesale-links).

## 0.4.19: 12 Oct 2026

### Added

- Every screenshot in report.html opens in a lightbox: full size, fitted to the window, with the test and the step as
  its caption, links to the run's other screenshots, and a way back to its thumbnail. It works with no script, so the
  sandboxed copy on QAJev Cloud has it too. Where scripts run, Esc closes it and the arrows move (José).
- A cloud run may sign in with a seeded test account, through the full tunnel with `data: local`. Only `seed:FILE#KEY`
  references from a fixture that says `"test_account": true` qualify. QAJev sends one copy of each fixture with only the
  values the run's account uses, and the cloud hides those values in all it sends back (José via the Orchestrator).

## 0.4.18: 12 Oct 2026

### Added

- The full tunnel to this machine's dev servers, for QAJev Cloud. A suite's `tunnel:` (or a project env's `tunnel`)
  names each port and its dev server's command, and `data: local` for writes. The run then goes to the cloud with
  several ports, writes and WebSockets. Before it starts, this machine checks that exactly that process holds
  each port, inside the project's checkout. With `tunnel:`, `--runner auto` sends the run too (verse-ship, foleyapp).
- `qajev tunnel FILE [--env ENV]` prints the tunnel a suite or project env declares. The cloud dispatcher on this
  machine checks each run's tunnel against the file on disk with it (redteam).
- A cloud run's `--json` result gives each test's `checks` (`check`, `ok`, `value`), as a run here does. A gate that
  reads `tests[].checks[].value` reads a cloud run the same way. Before, the value showed only inside `wrong` (verse-ship).

## 0.4.17: 12 Oct 2026

### Fixed

- `qajev smoke` shows its live screen, as a check or a suite does: on the dashboard and on QAJev Cloud's run page. Smoke
  never started the live frame writer, so every smoke said "No live screen yet" (José).

### Added

- `QAJEV_LOCAL_RUNS=off` in `~/.qajev/.env`: this machine runs no QAJev test. Every browser run here is refused
  before Chrome starts and says to send it to QAJev Cloud with `--runner cloud`. The file is read at every run, so a shell
  cannot turn it back on, and a long-lived MCP server sees the change (José).
- Each page of a smoke run records its `lcp` (ms) and `cls` in the report, as a check does. Smoke measured them but used
  them only for its "slow largest contentful paint" finding (figgyz.com).

## 0.4.16: 12 Oct 2026

### Added

- `color_scheme` (`light` or `dark`) on a scenario, a suite or a project objective, `--color-scheme` on the command
  line, and `color_scheme` on `qa_check`, `qa_run_suite`, `qa_smoke` and `qa_project_run`: the page's
  `prefers-color-scheme` answers so, so a test can prove that dark mode follows the system (foleyapp). A test's own
  setting wins. Reduced motion stays on. The report and its JSON name the scheme.

## 0.4.15: 12 Oct 2026

### Fixed

- Web Workers run again. From 0.4.12, QAJev's guard for frames on another site made Chrome hold every worker at its
  start, so a page that waits on a worker never went on (a stream player stayed on "Selecting pipeline", twinbox1).
  Workers now start at once, with `block_urls` in them first.

## 0.4.14: 12 Oct 2026

### Added

- A queued cloud run says why it does not start: cloud machines are off, today's minutes are used up, the model spend
  reached its limit, or too few credits. The progress line, `qajev jobs r_...`, `--json` (`waiting`) and MCP progress
  and `qa_job` show it.
- `qajev projects --json` gives each project's `site`, `runner` and `objective_names`, for QAJev Cloud's Projects page.

## 0.4.13: 12 Oct 2026

- Each test lists what its frames said and asked for, so a framed tool that stalls shows why: `frame_console` (log,
  info, warn and debug lines, errors, exceptions, rejections) and `frame_requests` (each request that failed, or still
  waits after a second, with its state), each with the frame's address. The report shows the first 20 of each. The top
  page's own console is left as it is: its errors are findings, as before (verse-workshop's rig tool, 12 Oct).
## 0.4.12: 12 Oct 2026

- **Frames on another site are guarded.** Chrome runs such a frame apart from the page, and QAJev 0.4.11 and earlier
  left it unguarded: in a readonly run it could send writes and reach `block_urls` addresses. QAJev now attaches to
  each one before it runs (Target.setAutoAttach, waiting for the debugger), puts the read-only guard and the block list
  in it, then lets it run. Its blocked writes and its requests to blocked addresses are listed with its `frame`, and
  the live test that proved the leak now passes. A frame QAJev still cannot guard is named and makes the test harness,
  as in 0.4.10.
- Readonly runs on QAJev 0.4.11 and earlier, on pages with frames on another site, were not proven read-only.

## 0.4.11: 12 Oct 2026

- `guard: {unguarded_frames: [{host, why}]}`: a reviewed exception for a frame on another site, by exact host name.
  It is listed as "accepted: not guarded" with its reason, and it does not make the test harness. Any other such
  frame still does. Verse's town screen embeds YouTube, so every Verse load was harness under 0.4.10.
- Fixed: a test no longer inherits the last test's frame on another site. In 0.4.10, test 2 of a suite ended as
  harness for a frame on test 1's page.
- Fixed: a click into an iframe right after the page loads is no longer lost. Chrome dropped such clicks for about
  0.5 s after the load: 0 of 12 at once, 4 of 4 after 0.5 s. `click` and `click_at` now wait until 0.75 s after the
  load when their point is on a frame.
## 0.4.10: 12 Oct 2026

- A `click_at` step: a real mouse click at a point, `{selector, x, y}` in CSS pixels from the element's top-left corner
  (or the viewport's without `selector`). It reaches into an iframe, so a test can click a thing in a 3D view, where
  `click`'s center depends on the camera (verse-games, 12 Oct). A point another element covers, or one outside the
  element, fails and says why. `{selector, js}` takes the point from an expression on the top page that returns
  `{x, y}`, for a point known only at run time.
- Known, so nobody chases it again: a visible run minimizes its window after each page, and the page keeps drawing.
  QAJev's Chrome flags (`--disable-backgrounding-occluded-windows`, `--disable-renderer-backgrounding`) keep a
  minimized page "visible", with its animation frames running (about 50 in 600 ms, measured on macOS, 12 Oct). A page
  that stalls in a QAJev window stalls for another reason.
- **Known gap (0.4.9 and every earlier version):** a frame on another site than the page runs as a target of its own
  (Chrome's site isolation), and QAJev's read-only guard and `block_urls` do not reach it. Such a frame can send
  writes and reach blocked addresses, even in a readonly run, on production too. Found 12 Oct with a live test: the
  frame's GET and POST reached the blocked server. Frames on the page's own site (any port) are guarded. 0.4.11 guards
  frames on other sites.
- Until then, such a frame is named in `frames_not_observed`, the report says "Frame not observed: <address>. QAJev's
  read-only guard and block_urls do not reach it, so it may send writes", and the test ends as harness in every mode,
  never pass.

## 0.4.9: 12 Oct 2026

- A run with `block_urls` lists every request to a blocked address, from the page and every frame in it:
  `block_list_requests` in `report.json`, with the method, the address, the frame that sent it and whether Chrome
  blocked it. Before, a blocked GET was listed nowhere (verse-workshop, 12 Oct).
- `blocked_writes` now holds the writes the read-only guard stopped inside frames too, each with its `frame`. Before,
  QAJev read only the top page's record, so a framed page on another origin could try to save and the report said
  nothing.
- One-line installer: `curl -LsSf https://qajev.com/install.sh | sh` on macOS and Linux. It installs uv when it is
  missing (the old `uv tool install` line failed with "command not found: uv" on a new machine), then QAJev, puts it on
  the PATH and checks for Chrome. `QAJEV_VERSION` pins a release; `QAJEV_NO_MODIFY_PATH=1` changes no shell file.
  Windows: `irm https://qajev.com/install.ps1 | iex` installs it in WSL, or says how to turn WSL on.

## 0.4.8: 11 Oct 2026

- `--serve DIR` refuses a folder that holds a secret: an env file (`.env`, `.dev.vars`), a key (`*.pem`, `*.key`,
  `id_ed25519`), credentials JSON, `.npmrc` or a `.git` folder. It names them and asks for a clean build
  (`git archive`): the folder goes to a cloud machine.

## 0.4.7: 11 Oct 2026

- A failed test now names what QAJev's guard hid on its page too ("guard hid: 'Roll back to this' (danger: Roll back);
  a check that reads a hidden control sees QAJev's hiding, not the page"). Before, only stuck and harness results did,
  so a `js` check that measured a hidden button read like a product bug.

## 0.4.6: 11 Oct 2026

- An on-screen check (`visible`) whose text QAJev's guard hid now says "hidden by QAJev's guard (...)" instead of "in
  the page but hidden", and its outcome is harness, as for `text` since 0.4.5.

## 0.4.5: 11 Oct 2026

- A text check that fails only because QAJev's guard hid the text now says so: "hidden by QAJev's guard (danger:
  ...); if it is safe, allow it in the suite: guard: {allow: [...]}". Its outcome is harness, not fail: the guard's
  choice says nothing about the page. ([Troubleshooting](docs/troubleshooting.md))

## 0.4.4: 11 Oct 2026

- `qajev mode` switches where runs go: `qajev mode cloud` sends this Mac's next runs to QAJev Cloud, `qajev mode local`
  keeps them here, `qajev mode auto` uses the cloud when it can, and `qajev mode dashboard` follows the dashboard's
  "Where runs go by default" (looked up once a minute), so a person can switch every agent from the dashboard.
  `qajev mode` alone says where runs go and why. `--project NAME` sets one project's `runner` (`default` removes it).
  MCP: `qa_mode`. `qajev runner` is the same command. ([CLI](docs/cli.md#qajev-cloud-where-a-run-goes))
- Changed: a project's `runner` now wins over this Mac's mode (`QAJEV_RUNNER`). Only `--runner` comes before it.
- Changed: a run on this Mac's dev server (`127.0.0.1` or `localhost`) goes through the cloud's read-only tunnel only
  when the command says `--runner cloud` (MCP `runner: "cloud"`). With `auto` it stays here; with a mode of `cloud` it
  stops with the reason. The tunnel passes no POST or WebSocket, so such a run would fail there for the tunnel's sake.
- `QAJEV_CLOUD_HOLD` (comma-separated words) keeps matching suites, projects and targets on this Mac unless the
  command says `--runner cloud`.

## 0.4.3: 11 Oct 2026

- Fixed: every QAJev browser run inside machine-lane failed at Chrome's start with "Exception occurred in
  preexec_fn". The lane runs at nice 10, QAJev lowered itself by 10 more, and on macOS every os.nice() call at nice
  20 raises EPERM. Now QAJev goes to nice 10 only when it runs above that priority and changes nothing at 10 or
  more; Chrome, Electron, Godot and the Android emulator start without a Python preexec_fn and inherit it. Nice never
  fails a run: a refusal is a warning.
- Runs can go to QAJev Cloud instead of this Mac: `--runner cloud|local|auto` on `check`, `run` (a suite or a
  project) and `smoke`, and `runner` on `qa_check`, `qa_run_suite`, `qa_smoke` and `qa_project_run`. `local` is the
  default and works as before. `cloud` sends the run or fails with the reason; it never runs it here instead. It
  refuses a signed-in run, a run that writes to a dev server on this Mac, and `play`. A read-only run on one
  127.0.0.1 or localhost port goes through QAJev Cloud's read-only tunnel to this Mac; `--serve DIR[:PORT]` ships a
  built site instead. `auto` runs those here, with one line saying why. A cloud failure (no key, no answer, a refused
  upload, a stopped run, a wait that runs out) is never run here either. The default comes from `QAJEV_RUNNER` (the
  shell, then `~/.qajev/.env`), then `runner` in project.toml. `--model` picks the cloud run's model. The run's
  address shows at once; `--json` prints one object with the gate, each test's reason and the report's address.
  `qajev jobs`/`stop` and `qa_job`/`qa_stop` take a cloud run's id (`r_...`). The key comes from `QAJEV_CLOUD_KEY`
  or Keychain (`qajev-cloud-agent`) and is never printed. `qajev doctor` has a `cloud` line.
  ([CLI](docs/cli.md#qajev-cloud-where-a-run-goes), [MCP](docs/mcp.md), [Configuration](docs/configuration.md#qajev-cloud))
- An approved test plan for every run (José, 8 Oct: no run without a test plan the Orchestrator approved). A suite
  names it with `plan:`; `run`, `check`, `smoke` and `play` (and their MCP tools) take `--plan FILE` / `plan`. The
  plan is a Markdown file whose last line is "Approved by the Orchestrator <date> <time> sha256:..." with the sha256
  of the plan text above it (`qajev plan-hash FILE` prints it), so an edit after the approval is not approved.
  `qajev plan` / `qa_plan` show whether it is approved (`test_plan` in the JSON). The report shows the plan's path and
  approval line, or a banner "No approved test plan". A run without one warns; `QAJEV_REQUIRE_PLAN=1` refuses it
  before Chrome starts (exit 3). `qajev doctor` says which rule is on.
  ([Writing tests](docs/writing-tests.md#an-approved-test-plan))
- A virtual gamepad: `pad` hooks in web suites and `pad:` steps in Electron game suites press buttons, push sticks and
  pull triggers, with no real device (I'M HIM supports a controller and key rebinding, but no plan could test it).
  Chrome has no gamepad input, so QAJev puts one virtual pad in the page. It has the standard mapping, is connected,
  and its timestamp goes up on every change, so a game's pad picker takes it. A web page gets it before its own
  code runs. A game gets it at its first pad step, with a `gamepadconnected` event. Home is refused. Suites with a
  bad button or bound are refused before the run (exit 3).
  ([Writing tests](docs/writing-tests.md), [Games](docs/games.md))
- The I'M HIM! adapter reports the game's own QA flags (`window.__qaState`) as state fields. A play step can now stop
  on one: `until: {kaigun_beaten: true}`. It keeps strings up to 200 characters, numbers and booleans, at most 50 of
  them. A flag never replaces one of the adapter's own fields. ([Games](docs/games.md#adapters))

## 0.4.2: 7 Oct 2026

- Fixed: a run now stops when the process that started it ends. A lane stopped a run script for high load, but
  `qajev play` kept going on its own, with its game window at 120% CPU for 20 minutes. Now `check`, `run`, `smoke`
  and `play` see within 2 s that their parent ended. They then stop like Ctrl-C and close their browser or game.
  Runs that launchd starts (nightly) and background jobs are not affected. To keep a run you detached on purpose,
  set `QAJEV_OUTLIVE_PARENT=1`. ([Configuration](docs/configuration.md))
- Game steps can prove where they start and how long a state lasts (im-him plan 40: a cutscene that never opened
  passed the same way as one that played out).
  - `before: {screen, text, state}` on any step but a relaunch must hold when the step starts, or the step fails at
    once and does not run.
  - `lasted: {min, max}` on an `idle` step with `before:` measures how long that state held with no input. QAJev
    looks every half second, and the check passes or fails only when both looks around the end agree. When the
    machine was too slow to tell, the step is harness. The checks after it get no 10 s grace period.
  - `qajev play` and `qajev plan` refuse a bad `lasted:` when the suite loads (exit 3).
  - Fixed: an idle step's time now includes its idle. Plan 40 showed "0 s" after a 45 s idle. The report says
    "idled 45 s, no input from QAJev".
  ([Games](docs/games.md#where-a-step-starts-and-how-long-a-state-lasts))
- A game goal step can be judged by later steps: `judged_by: [step names]`. It passes only when Jev took at least
  one action and every named step passed; a DONE with no action stays unverified, and a judge that did not pass
  leaves it unverified (its own result counts what went wrong). The names must be later steps of the suite, checked
  when it loads (`qajev play` and `qajev plan` refuse the rest, exit 3); `--only` brings a step's judges. For a
  template whose Jev step has no end state of its own (SideGame1, im-him: about 25 scenarios gated INCOMPLETE).
  ([Games](docs/games.md#a-goal-step-judged-by-later-steps))

## 0.4.1: 7 Oct 2026

- `cpu_throttle: N` (suite, project env, `--cpu-throttle N`, MCP `cpu_throttle`): Chrome runs the page's CPU N times
  slower, 1 to 8, default 1, set before the first page loads. A phone pass then measures phone-like speed, not
  this machine's at a phone's size (the Verse QA night). The report says so under Run ("CPU: 4× slower").
  ([Writing tests](docs/writing-tests.md))
- Fixed: three ways a run blamed the product for its own setup.
  - A local dev server that is not answering (connection refused, reset or closed, a name that does not resolve) is
    **harness**: "the server at URL is not answering". Before, every scenario of a run whose app was not started was
    a product **fail** (job 20261007-015517-7910). On a production host it stays a fail with the same reason: the
    site is down.
  - A `js` check whose own code breaks (TypeError, ReferenceError, SyntaxError) is **harness**: "the check failed to
    run: ...". A check that returns false, or throws on purpose, still fails (verse3: 36 "fails", all
    `x.toFixed is not a function`).
  - The guard hid a dice "Roll" button as dangerous; it now hides only a rollback ("Roll back", "Rollback release").
  In CI such runs now exit 2 (INCOMPLETE) instead of 1. ([Reports](docs/reports.md#outcomes))
## 0.4.0: 7 Oct 2026

- Docs: every surface says what 0.4.0 does.
  - `qajev --help`: `--env-file` never reads the current folder's `.env`; `secret` and `account` describe seeded
    test accounts only; `--visible` and `doctor` say what they check.
  - Docs: `qajev plan`, `qa_plan` and `qa_rerun` in the agent prompt, the README and the CLI command list; the
    troubleshooting page (key sources, HTTP 401/403, refused runs, seeded sign-in); report lines for keys, live
    watching and game profiles; and `--game-profile` folders.
  - The site lists all 17 MCP tools.
  - Contributors get an every-surface checklist in the pull request template.
- Fixed: a multiplayer scenario (`clients:`) whose checks all passed and all said what they prove still gated
  INCOMPLETE: the checks judged across clients, and at each snapshot, lost their `says` on the way to the report,
  so the test plan called them NOT DESCRIBED (verse1: 13 of 13 passed). They keep their words now.
- Changed: Jev saying DONE without taking a single action, while a check fails, is now **harness**, not a product
  **fail**: it declared victory on a page it never tried, which says nothing about the page. A goal Jev worked on
  and a page that is wrong still fail. The audit of past verdicts found 45 of 87 such FAILs (52%) were wrong.
  In CI such a run now exits 2 (INCOMPLETE) instead of 1 (FAIL). ([Reports](docs/reports.md#outcomes))
- Every report names the QAJev commit that judged it: `qajev_commit` in `report.json`, beside the version in
  `report.md` and `report.html`, and in `qajev doctor` (`+dirty` when QAJev's own code had uncommitted changes;
  absent for an installed package). The audit of past verdicts could not tell which rules judged a run.
  ([Reports](docs/reports.md))
- Changed (breaking): **production is read-only, destructive is never.** Only a local dev host may change:
  `localhost`, `127.0.0.1`, `[::1]`, `*.localhost` and `*.test`. Every other host is production, staging and
  previews included, with no override.
  - `mode: mutate` or `--allow-destructive` on such a host is refused before Chrome starts, from `check`, `run`, a
    project, `nightly`, `rerun` and the MCP tools: "QAJev never changes a production site: HOST is not a local dev
    host.", **exit code 5** (was 3), outcome `refused` (MCP returns it instead of raising). A play suite that asks
    for `mode: mutate` is refused the same way: games and apps have no write guard.
  - In the page the guard decides per page and per request: a local run that reaches a production page is
    read-only there. On production `DELETE`, `PUT` and `PATCH` never leave the page, a `POST` only when
    `guard.allow_requests` names it (listed in the report as an allowed write), `confirm()` gets "no", `prompt()`
    nothing, `beforeunload` never holds the tab (each listed as dismissed), and Delete or Backspace reach the page
    only inside a text field. `guard.allow` no longer shows a dangerous control on production.
  - Destructive controls (delete, remove, erase, destroy, drop, purge, wipe, cancel, refund, void, revoke,
    deactivate, unsubscribe, archive, reset, empty trash, close or terminate account, uninstall, disconnect) are
    hidden on every host, local too, and `guard.allow` never shows them. A reset, clear or cancel of filters, a
    search, a selection or the form being edited stays, unless the control names an account, order, subscription,
    plan, booking, data, payment or membership; Escape always reaches the page, so a dialog still closes. `--allow-destructive` (suite
    `allow_destructive: true`, project env `allow_destructive = true`, MCP `allow_destructive`) shows them on a
    local dev host; the report then carries a red banner. Hooks may now press `Delete`.
  - `qajev doctor` prints the rule and the local dev hosts.
  - Migrating: a mutating suite against a staging or preview host now exits 5: point it at a local copy, or a
    `*.test` / `*.localhost` name for it. A local suite that used `allow: ["^Delete ..."]` to press a destructive
    control needs `allow_destructive: true` instead. CI that treated exit 3 as "refused" should read 5.
    ([Safety](docs/safety.md))
- **Breaking:** QAJev types a password only for a seeded test account on a local dev host (José, 6 Oct: "local test
  accounts only"). The password comes only from `seed:FILE#KEY`, a fixture that says `"test_account": true` and lists
  the host in `allowed_hosts`, checked when the suite loads and again where the browser is just before it types. A
  `keychain:`, `op://` or `env:` password (and a project's `password_env`) is refused on every host, before a key is
  pressed. For any other account, sign in once yourself in QAJev's window (`qajev browser login --url <sign-in
  page>`); the profile keeps that session. `qajev account add` and `qajev secret set` are refused; `qajev secret
  check` and `qajev account check` stay. ([Signed-in areas](docs/writing-tests.md#signed-in-areas))
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
- Changed (breaking): a `wait_for` timeout is in seconds, at most 300; a suite that says more is refused when it
  loads (exit code 3), and 1000 or more is named as milliseconds (`timeout: 60000` read as almost 17 hours).
  Migrating: a suite with a longer wait now stops at load; give the timeout in seconds (300 at most), and split a
  longer wait into steps.
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
