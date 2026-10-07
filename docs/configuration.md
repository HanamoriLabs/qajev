# Configuration

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

| Variable | Is | Default |
|---|---|---|
| `QAJEV_CHROME` | the Chrome or Chromium binary | found automatically |
| `QAJEV_CHROME_FLAGS` | extra Chrome flags, e.g. `--no-sandbox` in Docker or on CI runners that block user namespaces | none |
| `QAJEV_CHROME_START_WAIT` | seconds Chrome may take to start | `60` |
| `QAJEV_OUTLIVE_PARENT` | `1`: a run keeps going after the process that started it ends. By default `check`, `run`, `smoke` and `play` stop, like Ctrl-C, and close their browser or game | unset |
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
