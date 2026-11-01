# Troubleshooting

**"viewport mismatch: page ... capture ... requested ..." (harness)**

The page and its capture do not agree with the requested device geometry. Before a Chrome web scenario's
checks or hooks, QAJev measures the page, takes a native capture, then measures again. A mismatch or a resize
during capture stops that scenario as harness. This check also runs with `--no-shots`.

Saved, reactive and vision images validate their own geometry at capture time. A correct startup cannot make
a later mismatched frame pass. The full-frame capture stays within the native viewport, and a rejected frame
is not saved or sent to the vision judge. Passing checks do not turn that capture failure into a product PASS.

Inspect the scenario's `viewport` object in `report.json` (each client's object for a clients scenario).
It contains requested geometry, actual `innerWidth` and `innerHeight`, the visual viewport, DPR and capture
pixel dimensions. CSS layout pixels and image pixels differ at a device scale above 1. Mobile pages without
a viewport meta tag may also use a wider layout scaled into the screen; QAJev accounts for that visual scale.
`capture_viewport` records a later frame's measurement or failure. The initial `viewport` record remains intact.

A correct page/capture geometry does not prove that a canvas or app container fills the page. A narrow
container with correct page dimensions is a separate layout question. A geometry failure is not repaired by
resizing the evidence image or by accepting a passing scripted check.

Start with `qajev doctor`: it checks keys, Chrome, ports and load, and says what to fix.

**"Chrome not found"**
Set `QAJEV_CHROME` to your Chrome or Chromium binary, e.g.
`export QAJEV_CHROME=/usr/bin/chromium`.

**"goals need TYPESAFE_API_KEY or an OpenRouter key ... or QAJEV_JEV_PROVIDER=cloudflare ..."**
Add one key to `~/.qajev/.env` ([Getting started](getting-started.md)). `qajev smoke` needs no key. Cloudflare
credentials alone are not used: Clef needs `QAJEV_JEV_PROVIDER=cloudflare` too.

**A key in the file is ignored**
QAJev's own keys come from its files (`--env-file`, `$QAJEV_ENV_FILE`, `~/.qajev/.env`) even when your shell has
another value; it does not read the current folder's `.env`. A key no file sets is taken from the shell. To use
another value on purpose, set it as `QAJEV_<NAME>` (e.g. `QAJEV_OPENROUTER_API_KEY`). `qajev doctor` says where each
key came from ([Configuration](configuration.md)).

**"HTTP 401" or "HTTP 403" from the model**
The decision or text model refused the key. The message names where that key came from: fix it there, or move it
into `~/.qajev/.env`. A project's own `.env` (a deploy token, another service's key) is no longer read, so it cannot
be the cause; an old value pinned as `QAJEV_<NAME>` in your shell can.

**The run says "queued behind ..."**
Another QAJev run has the browser; yours starts when it ends. See who with `qajev jobs` or `qajev top`.

**Many scenarios come out `harness`**
That is QAJev's side, not your product:

- *decisions went stale*: the page kept changing (new words, a list that moved, an animation). A clock time or a
  countdown that ticks (a video's "0:03 / 188:26", "Doors close in 59 s", "5 min"), or a time taken ("in 2.3 s", "in
  412 ms"), does not count: a move whose page changed only there goes ahead. Other numbers are read exactly: a step, a count, a price or a score that changes is
  a change. The reason ends with the last stale move's own: what was over its target ("covered by
  div#toast") or what changed; each stale decision says it in the dashboard and in `qajev top --decisions`. Keep
  `--motion reduce` (the default) and start the scenario on a calmer page, or close what covers the target with a
  `before:` hook.
- *action or time budget spent*: split the goal, or start closer to the target with `url`.
- *Jev clicked one target again and again*: Jev clicked the same thing 3 times in a row, and the page did not move
  on (nothing happened, or it only toggled back and forth). Controls built again with the same names and values, and
  a clock or a time taken that changes, are not the page moving on. The reason names the target and the last
  actions. Check by hand that the control works. If it does, say in the goal what comes after it.
- *Jev never left the start page*: the checks need another page, and Jev only scrolled where it began and clicked
  nothing (often a phone menu it never opened). Say the way there in the goal ("open the menu, then the pricing
  page"). Jev clicking around and not arriving is still a failure: the way may really be missing.
- *cost cap reached*: raise `--cost-cap`, or narrow the goals.
- *cap N s hit*: the test ran past its cap, `budget.seconds` plus 60 s (150 s by default), so QAJev stopped its
  Chrome. Often the page hung, or a dialog or a form held it. The tests after it in its `depends_on` chain are
  skipped. The other tests run in their own Chrome. Raise `budget.seconds` only if the test truly needs longer.
- *a fresh Chrome's chain did not get to its next test within 60 s*: Chrome, the sign-in or the end of the last test
  took longer than `QAJEV_FRESH_START_WAIT`. On a busy machine, raise it.
- *the screenshot shows the page's main canvas as one flat dark colour*: the 3D view (or another large canvas) is not
  in the picture, while the HTML over it is. A headless Chrome started with `--disable-vulkan-surface` leaves every
  WebGL and WebGPU canvas out of its screenshots: remove that flag from the browser you attach with `--cdp-url`. If the
  page really draws nothing there at the end, that is the page's bug: end the test on a drawn frame. If the browser
  cannot show the canvas in a screenshot, give the suite `canvas_capture: {page: ...}`, the canvas's own readback.
- *the looks were not judged: the screen shows the page's main canvas as one flat dark colour*: Clef would only have
  seen a blank 3D view, so QAJev did not ask it. The causes and fixes are the same as for the screenshot line above.
  When the scene is dark on purpose, give the scenario `canvas_dark: "<why>"`.
- *the canvas's own readback (canvas_capture: page) is one flat dark colour*: the page drew nothing into the canvas,
  or the readback is empty. A WebGL or WebGPU canvas needs `preserveDrawingBuffer: true`, or a read in the same frame.
- *canvas_capture: page gave no readback image*: the expression failed, or gave no `data:image/...;base64` URL.
- *canvas_capture asks for a hardware GPU, but the page's WebGPU adapter is software*: the browser gives the page
  SwiftShader or another software adapter. Run the test on a machine with a GPU, with Chrome's GPU flags. The same
  applies to *the browser's GPU is software*.
- *... gives no vendor or architecture ...: the GPU could not be read*: the page's adapter does not say what it is, and
  WebGL names no GPU either (why is in brackets). Run the test on a machine with a GPU.
- *the canvas check could not read the end screenshot*: the flat dark check broke (the error is in brackets), so the
  result says nothing about the canvas either way. The outcome stays. Run the test again; if it repeats, report it.
- *the machine stayed busy past the run's load budget*: the test waited for a quiet machine longer than
  `--load-wait` and its start allowance, so QAJev stopped its Chrome. The test did not run out of its own time. Run
  it again when the machine is quieter, or raise `--load-wait`.

**"a URL with a user name or password is refused"**
The URL holds a user name or password (`https://alice:s3cret@shop.example/`). QAJev never writes down a password,
and the URL would reach the report files. Sign in once with `qajev browser login --url URL`, then run the plain URL
with that `--profile`.

**"sign-in failed: ..." and every scenario is `harness`**
The seeded test account could not sign in, so nothing was tested. The reason is the site's own message ("Wrong
email or password") or the seed fixture's: check that your app's seed wrote the fixture, that it says
`"test_account": true`, and that it lists the host in `allowed_hosts`. QAJev signs in by itself only on a local dev
host, with a `seed:FILE#KEY` password; a `keychain:`, `op://` or `env:` password is refused. If the site's fields
are unusual, name them in the account's `login:` (`open`, `email_field`, `password_field`, `next`, `submit`) and say
how to tell it worked (`signed_in`). "cleared the email and password QAJev typed, twice": the page's scripts started
after QAJev typed (a dev server that builds the page on the first request does this). Open the page once before the
run.

**"hidden by QAJev's guard (danger: ...)" (harness)**
The text is on the page, inside a control QAJev's guard hid because it holds a risky word ("billing", "delete",
"sign out"...), so no test presses it by mistake. The result is harness: it says nothing about the page. If that
control is safe to show (a plain link whose name holds the word), allow it in the suite:
`guard: {allow: ["billing, signed in"]}`.

**"hook failed: refused: the guard hid this control (danger: Sign out); your selector matches 2 elements …"**
A hook's selector found a guarded control first, such as a Sign out button in a sidebar before the form's Save.
QAJev clicked nothing. Make the selector match only the control you mean, for example `form#agreement
button[type=submit]` or a `data-qa` attribute. QAJev never clicks a later match on its own.

**"refused: QAJev never changes a production site ..." (exit code 5)**
The run asked to change a site that is not a local dev host (`mode: mutate` or `--allow-destructive`), so nothing
started. Run it read-only, or against a local copy (`localhost`, `*.test`, `*.localhost`). There is no override
([Safety](safety.md)).

**"needs sign-in: ..."**
The run met a sign-in page, so what is behind it was not tested. Give QAJev a way in: sign in once with
`qajev browser login --profile NAME --url LOGIN_URL` and run with `--profile NAME`, or, on a local dev site, use a seeded
test account ([Signed-in areas](writing-tests.md#signed-in-areas)). With a profile, sign in to it again: its
session expired. If that page should be public, that is the bug.

**A scenario is `stuck`**
Jev found no way forward. Open the screenshot: a visitor would often be stuck too (a hidden menu, two buttons
with the same name, content far below). If the target is far down a long page, start closer to it.

If the reason ends with **"guard hid: ..."**, the way forward may be a control QAJev's guard held back, with the
words that made it ("'Shop Show clothes and gear you can buy' (danger: buy)": the guard reads the tooltip too).
When that control is safe, let it through with a narrow `guard: allow:` entry ([Guard options](writing-tests.md#guard-options)).

**Jev keeps re-typing a field**
Give the value in the goal, and make it different from the field's placeholder. For long forms, use a `fill` hook
and let Jev do the rest.

**`fail` but the page looks right**
Read the failed check: text checks compare what a person reads (hidden elements do not count), and `text` passes
anywhere in the page while `visible` needs it on screen. Use `ignore_case: true` for labels styled in capitals.

**The machine is busy and scenarios are skipped**
QAJev waits while the load is very high, then skips ("machine busy"). Tune `--load-high`, or set
`QAJEV_LOAD_HIGH=0` to never wait.

**A run was killed and left a browser open**
The next run cleans it up; or run `qajev browser reap`.

**`browser/daemon transport lost`**
The connection closed before QAJev could confirm the current action or check. That scenario is harness and
the remaining scenarios say `not run: transport lost`; they do not use the failed session. Earlier finished
results stay in the report. QAJev does not automatically replay the scenario, its hooks or its actions.

Check the browser and system logs for a crash, a killed process or a memory limit before running again.
A restarted GPU process can lose the page's WebGPU resources even when Chrome stays open. Restore a known
test state, then start a new QAJev process explicitly. A response timeout or an ordinary rejected CDP command
does not by itself prove that the transport is gone.
`Target closed` and `Session with given id not found` can describe a closed popup or a detached target on a
usable browser connection. They keep normal command-error handling, without invalidating the whole transport
or replaying the rejected command. Socket, EOF, browser shutdown and WebSocket-loss errors still stop reuse.
The daemon's own `cdp_disconnected` and `Client is stopping` errors also invalidate that connection.

**"the browser stopped answering" (harness)**
A browser command timed out twice in a row (`QAJEV_CDP_TIMEOUT`, 30 s each), with no page dialog to answer. That
test ends at once; before, every later command waited as long again. The reason gives the machine's load at both
timeouts. Often a page script never stopped, or the machine was too busy. The next test tries the browser again.
The tests that `depends_on` it are skipped, as after any test that did not pass.

**"the page crashed at <URL>; last step: <step>" (fail)**
The page's renderer crashed (Chrome's "Aw, Snap"), so the test fails: a crash is the product's fault. The step says
what the test was doing when QAJev heard of it: opening the page, a hook, Jev's last action, or the checks. Run it
again with `cache: cold` (or `--cold-cache`) to see whether it happens only on a first visit. Add `console_keep`
patterns for the page's own start-up lines, so they stay in the report however much the page logs. A multiplayer
test names the client, an Electron step names the step, and a sign-in page that crashes fails the tests it
blocked. A renderer that hangs instead of ending sends no crash event: that test still grades harness, "the
browser stopped answering". In headless Chromium on Linux, `Page.crash` and `chrome://crash` hang it that way.

**The MCP tools do not show up in my agent**
Check that `qajev mcp` runs from the agent's environment (use the full path from `which qajev`), then restart the
agent's session.

**A game will not start**
For Godot, set `QAJEV_GODOT` to the Godot binary if it is not on your PATH. For an Electron project folder, install
its dependencies (so `node_modules/.bin/electron` exists), or set `QAJEV_ELECTRON`.

Still stuck? [Open an issue](https://github.com/hanamorilabs/qajev/issues) with the output of `qajev doctor` and,
if you can, the run's `report.md` (check it for anything private first).
