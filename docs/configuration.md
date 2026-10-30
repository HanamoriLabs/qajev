# Configuration

Parallel scenario workers use fresh Python processes. Trusted provider integrations can register an importable
worker bootstrap to restore their model route before any scenario executes. It carries routing metadata only;
authorization is read inside the worker. This preserves dashboard routing for Cloud runs with `--jobs` above one
when the companion Cloud runner supports the bootstrap. Local provider selection, flags and report fields are
unchanged.

Most people only set one API key. Everything else has a sensible default.

## Where settings are read from

Keys and settings are environment variables. QAJev reads its own files, in order: `--env-file`, `$QAJEV_ENV_FILE`,
then `~/.qajev/.env`. It never reads the current folder's `.env`: a project's file is the project's, and can hold
production secrets or a token for something else (a Cloudflare deploy token there once made Clef answer 401).

QAJev's own keys (the `TYPESAFE_*`, `OPENROUTER_API_KEY`, `TEXT_MODEL*` and `CLOUDFLARE_*` variables below) come from
those files even when your shell has another value; to use another one on purpose, set it as `QAJEV_<NAME>`, e.g.
`QAJEV_OPENROUTER_API_KEY`. A key no file sets is taken from the shell, as in CI. Any other setting: the shell wins
over the files. `qajev doctor` and each report's header say where each of QAJev's keys came from (names, never
values), and a key the model refused (HTTP 401 or 403) points you there.

Three settings come only from the shell, never from an env file: `QAJEV_HOME`, `QAJEV_ENV_FILE` and
`QAJEV_TEST_DOMAINS` (QAJev reads that one straight from `~/.qajev/.env`). An env file that moved `QAJEV_HOME` would
point every QAJev process it starts at another `.env`, without your settings.

## Keys

| Variable | For | Default |
|---|---|---|
| `TYPESAFE_API_KEY` | Jev's decisions, through TypeSafe | none |
| `OPENROUTER_API_KEY` | Jev's decisions through OpenRouter, and the text helper | none |
| `TEXT_MODEL_API_KEY` | the text helper (what Jev types into fields): any OpenAI-compatible key | the OpenRouter key |
| `TEXT_MODEL_BASE_URL`, `TEXT_MODEL`, `TEXT_MODEL_REASONING` | the text helper's service, model and reasoning level | OpenRouter, `inception/mercury-2.5`, `none` |
| `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` | the decisions by Cloudflare's Clef instead of Jev (Workers AI; a token with Workers AI read access) | none |
| `QAJEV_JEV_PROVIDER` | `auto`, `typesafe`, `openrouter` or `cloudflare` | `auto`: TypeSafe if its key is set, else OpenRouter; Clef only when set to `cloudflare` |
| `TYPESAFE_MODEL` | Jev's model on TypeSafe | `jev-latest` |
| `QAJEV_OPENROUTER_JEV_MODEL` | Jev's model on OpenRouter | `~typesafe/jev-latest` |
| `QAJEV_CLEF_MODEL` | Clef's model on Cloudflare: `clef-flash` or `clef` | `clef-flash` |
| `QAJEV_REQUIRE_PLAN` | `1`: refuse a run without an approved test plan ([Writing tests](writing-tests.md#an-approved-test-plan)) | off: such a run warns |
| `QAJEV_PLAN_APPROVER` | the name an approval line must carry ("Approved by ...") | `the Orchestrator` |

**One OpenRouter key runs everything.** The report says which route each run used.

**Jev or Clef.** The decision model can be TypeSafe's Jev or Cloudflare's open Clef models, which answer the same
questions in the same form. To use Clef, set the two Cloudflare variables and `QAJEV_JEV_PROVIDER=cloudflare` (or pass
`--jev-provider cloudflare`); `QAJEV_CLEF_MODEL=clef` picks the larger model. `auto` never picks Clef: Cloudflare
credentials are often set for other tools, and QAJev only sends pages to Workers AI when you choose it. QAJev bridges the two differences
between the APIs itself: Clef refuses a choice with a single option (QAJev answers it: it is certain) and wraps its
answer differently. The report, `qajev doctor` and `qajev top` name the model deciding (Jev, Clef or Clef-flash), and
Clef's decisions are priced from its own token counts. The report counts and lists the decisions under that name;
elsewhere "Jev" stays the name of the tester's role, whichever model plays it. The text helper is unchanged: forms
still need its key. `qajev doctor` shows which keys are present and whether they work; it never prints their values.

**Clef reads images.** Only Clef can use screenshots: `looks` expectations (statements judged from the final
screenshot) and `vision` (Clef sees the screen with every decision). See
[What only a look at the screen tells](writing-tests.md#what-only-a-look-at-the-screen-tells-clef). With Jev, a run
that asks for either is refused before it starts. A game run (`qajev play`) uses vision by default with Clef, when the
game has a window; with Jev it plays from the labels. Screenshots go to Workers AI with the decisions, so choose Clef only
for sites and games you may send there. In our tests on QAJev's demo pages, both Clef models chose drawn buttons
correctly with vision and judged every looks statement right; Clef-flash is cheaper and was as accurate there.

## Where things are kept

| Variable | Is | Default |
|---|---|---|
| `QAJEV_HOME` | QAJev's own folder: browser profiles, jobs, the queue, nightly digests | `~/.qajev` |
| `QAJEV_OUT` | where runs without a project are written | `./qajev-runs` |
| `QAJEV_PROJECTS` | extra folders with project files (`:`-separated) | none (always `~/.qajev/projects`) |
| `QAJEV_REPORTS` | the cross-project reports index | `~/.qajev/reports` |
| `QAJEV_PROFILE` | the default browser profile | `default` |
| `QAJEV_DEVICES` | the devices every website test runs on | `desktop,phone` |
| `QAJEV_REAL_DEVICES` | also run website tests in real device browsers: `ios`, `android` | off |
| `QAJEV_IOS_DEVICE`, `QAJEV_ANDROID_AVD` | the simulator to clone / the virtual device to boot | the first iPhone / the first AVD |

What is inside `~/.qajev`:

```
~/.qajev/
  .env              your keys
  projects/         central project files (<name>.toml)
  profiles/<name>/  browser profiles you signed in to
  jobs/<id>/        every run as a job (kept 7 days)
  run.lock          the queue: who has the browser now
  nightly/          nightly digests
  runs/             runs started over MCP without a project
  game-profiles/    Electron game save folders kept between runs (--game-profile NAME)
  tmp/              throwaway browser profiles and game save folders (deleted after each run)
```

## Browser and machine

Linux viewport captures activate QAJev's test tab immediately before reading its pixels. A minimized test window
can become visible during capture, allowing Chromium to present a fresh surface. The capture keeps its requested
format, viewport dimensions, device scale and crop. React frames retain their individual files and measured capture
times; the existing hook and run limits still apply.

| Variable | Is | Default |
|---|---|---|
| `QAJEV_CHROME` | the Chrome or Chromium binary | found automatically |
| `QAJEV_CHROME_FLAGS` | extra Chrome flags, e.g. `--no-sandbox` in Docker or on CI runners that block user namespaces; disabled features are combined with the managed service policy | none |
| `QAJEV_CHROME_START_WAIT` | seconds Chrome may take to start | `60` |
| `QAJEV_OUTLIVE_PARENT` | `1`: a run keeps going after the process that started it ends. By default `check`, `run`, `smoke` and `play` stop, like Ctrl-C, and close their browser or game | unset |
| `QAJEV_CDP_TIMEOUT` | seconds per browser command | `30` |
| `QAJEV_LOAD_HIGH`, `QAJEV_LOAD_OK`, `QAJEV_LOAD_WAIT` | wait before a scenario (`smoke` and `play`: before starting) while the 1-minute load is at or above HIGH, until it drops below OK; at most WAIT seconds per run | `150`, `100`, `600` |
| `QAJEV_LOCK_WAIT` | seconds a run waits in the queue before giving up (exit code 4) | `3600` |
| `QAJEV_HOLD_FILES` | hold files, separated like `PATH`, that another job writes while it needs this machine alone. A browser run does not start while one of them exists and its first line names a live process (`<job> <pid> <time>`) that is not the run or a process it runs under. A file whose process is gone is ignored. A file that names no process is honoured. The wait counts toward `QAJEV_LOCK_WAIT`, and the report says how long the run waited and for whose hold | none |

The load gate only matters on shared or busy machines; set `QAJEV_LOAD_HIGH=0` to never wait.

## QAJev Cloud

| Variable | Is | Default |
|---|---|---|
| `QAJEV_RUNNER` | this Mac's mode: where `check`, `run`, `smoke` and `play` go: `local` (this Mac), `cloud` (QAJev Cloud, never this Mac), `auto` (the cloud when it can, else this Mac, saying why) or `dashboard` (follow the dashboard's "Where runs go by default"). `qajev mode` sets it. `--runner` wins, then a project's `runner` ([CLI](cli.md#qajev-cloud-where-a-run-goes)) | `local` |
| `QAJEV_CLOUD_KEY` | the QAJev Cloud agent key. Without it QAJev reads macOS Keychain, service `qajev-cloud-agent`. Never printed | Keychain |
| `QAJEV_CLOUD_URL` | the dashboard (https only) | `https://cloud.qajev.com` |
| `QAJEV_CLOUD_HOLD` | comma-separated words (any case): a suite path, project or target that holds one stays on this Mac unless the command says `--runner cloud` | none |
| `QAJEV_LOCAL_RUNS` | `off`: this machine runs no QAJev test. Every browser run here (check, suite, smoke, play, nightly) is refused before Chrome starts and says to use `--runner cloud`. Read from `~/.qajev/.env` at every run; an `off` there wins over the shell | on |
| `QAJEV_CLOUD_WAIT` | seconds a cloud run is waited for before QAJev gives up (exit `4`); the run goes on in the cloud | `7200` |
| `QAJEV_CLOUD_CREDIT_WAIT` | seconds a cloud run that waits for credits is followed before QAJev says so and returns (exit `4`); the run stays queued | `60` |

`qajev doctor` has a `cloud` line: whether the key is found (never its value), the default runner and where it came
from, and whether the dashboard answers.

### Managed Chrome services

QAJev starts its managed Chrome with background updating, sync, extensions, network-time checks and
search-eligibility requests disabled. It also disables on-device model downloads and speculative browser
preconnections. Its private preferences disable password services and dictionary downloads.

Chromium's policy token service can still start cloud messaging with background networking disabled.
QAJev sets the check-in and registration endpoints to the internal URL `chrome://qajev-disabled`.
Its raw TLS push channel uses `https://127.0.0.1:0`, an unusable loopback port, with no fallback endpoint.
Messaging retries cannot contact a remote service. Browser push messaging is unavailable in these managed
profiles; page navigation and local form input still work normally.

Extra `--disable-features=FEATURES` or `-disable-features=FEATURES` entries in `QAJEV_CHROME_FLAGS` are combined
with the managed defaults into one switch. Use the equals syntax; either bare form is refused before Chrome starts.
Extra flags cannot override the managed `--gcm-checkin-url`, `--gcm-registration-url` or `--gcm-mcs-endpoint`,
including Chromium's single-dash forms and quoted arguments with surrounding whitespace.
Chromium gives disabled features precedence over `--enable-features`; caller enables cannot restore the managed
disabled features. Native Windows Chrome is not supported; Windows installs use Linux Chrome inside WSL.

Parallel managed launches refuse caller overrides of `user-data-dir`, `remote-debugging-port` and
`remote-debugging-pipe`, including single-dash forms and quoted whitespace. Use `--profile` or `QAJEV_HOME`
to choose profile storage. Outside parallel isolation, a caller `--user-data-dir` selects another profile
and bypasses the preferences QAJev prepared for its owned profile.

Use `qajev browser status` to find the running profile names. After updating, restart an already running managed
profile with `qajev browser stop --profile NAME` before
using it again. QAJev preserves unrelated profile preferences and asks for this restart instead of reusing
a process that started without the service policy. Externally supplied `--cdp-url` browsers keep their own
launcher settings.

## Games

| Variable | Is | Default |
|---|---|---|
| `QAJEV_GODOT` | the Godot binary | `godot` on your PATH, or the macOS app |
| `QAJEV_ELECTRON` | the Electron binary, for an Electron project folder without its own | the folder's `node_modules/.bin/electron` |

## Nightly

| Variable | Is |
|---|---|
| `QAJEV_NOTIFY_CMD` | a command to run when the nightly run finds a change; it gets `$QAJEV_DIGEST` (the digest file) and `$QAJEV_DIGEST_TEXT` (a one-line summary) |
