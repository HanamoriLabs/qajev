# Configuration

Most people only set one API key. Everything else has a sensible default.

## Where settings are read from

Keys and settings are environment variables. QAJev reads them from, in order: `--env-file`, `$QAJEV_ENV_FILE`,
`./.env`, then `~/.qajev/.env`. A variable already set in your shell always wins (`qajev doctor` warns you when a
shell variable overrides a file).

## Keys

| Variable | For | Default |
|---|---|---|
| `TYPESAFE_API_KEY` | Jev's decisions, through TypeSafe | none |
| `OPENROUTER_API_KEY` | Jev's decisions through OpenRouter, and the text helper | none |
| `TEXT_MODEL_API_KEY` | the text helper (what Jev types into fields): any OpenAI-compatible key | the OpenRouter key |
| `TEXT_MODEL_BASE_URL`, `TEXT_MODEL`, `TEXT_MODEL_REASONING` | the text helper's service, model and reasoning level | OpenRouter, `inception/mercury-2.5`, `none` |
| `QAJEV_JEV_PROVIDER` | `auto`, `typesafe` or `openrouter` | `auto`: TypeSafe if its key is set, else OpenRouter |
| `TYPESAFE_MODEL` | Jev's model on TypeSafe | `jev-latest` |
| `QAJEV_OPENROUTER_JEV_MODEL` | Jev's model on OpenRouter | `~typesafe/jev-latest` |

**One OpenRouter key runs everything.** The report says which route each run used. `qajev doctor` shows which
keys are present and whether they work; it never prints their values.

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
  tmp/              throwaway browser profiles and game save folders (deleted after each run)
```

## Browser and machine

| Variable | Is | Default |
|---|---|---|
| `QAJEV_CHROME` | the Chrome or Chromium binary | found automatically |
| `QAJEV_CHROME_FLAGS` | extra Chrome flags, e.g. `--no-sandbox` in Docker or on CI runners that block user namespaces | none |
| `QAJEV_CHROME_START_WAIT` | seconds Chrome may take to start | `60` |
| `QAJEV_CDP_TIMEOUT` | seconds per browser command | `30` |
| `QAJEV_LOAD_HIGH`, `QAJEV_LOAD_OK`, `QAJEV_LOAD_WAIT` | wait before a scenario (`smoke` and `play`: before starting) while the 1-minute load is at or above HIGH, until it drops below OK; at most WAIT seconds per run | `150`, `100`, `600` |
| `QAJEV_LOCK_WAIT` | seconds a run waits in the queue before giving up (exit code 4) | `3600` |

The load gate only matters on shared or busy machines; set `QAJEV_LOAD_HIGH=0` to never wait.

## Games

| Variable | Is | Default |
|---|---|---|
| `QAJEV_GODOT` | the Godot binary | `godot` on your PATH, or the macOS app |
| `QAJEV_ELECTRON` | the Electron binary, for an Electron project folder without its own | the folder's `node_modules/.bin/electron` |

## Nightly

| Variable | Is |
|---|---|
| `QAJEV_NOTIFY_CMD` | a command to run when the nightly run finds a change; it gets `$QAJEV_DIGEST` (the digest file) and `$QAJEV_DIGEST_TEXT` (a one-line summary) |
