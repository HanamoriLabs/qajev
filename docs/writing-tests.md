# Writing tests

A QAJev test is a **scenario**: where to start, what a visitor wants (the **goal**), and how you will know it
worked (the **expectations**). Jev tries to reach the goal; your expectations decide the verdict. Jev saying
"done" is never proof on its own.

## One scenario from the command line

```bash
qajev check https://shop.example \
  --goal "Find what the Pro plan costs per month. Stop when that price is visible." \
  --expect-text '$29 per month' --expect-url /pricing
```

Leave out `--goal` to only check a page as it loads (no model calls):

```bash
qajev check https://shop.example/pricing --expect-text 'Pro' --absent 'Something went wrong'
```

## Expectations

| Expectation | CLI flag | Passes when |
|---|---|---|
| `text` | `--expect-text`, `-t` | the page shows these words (anywhere in the page) |
| `visible` | `--visible` | a person sees these words right now: in the viewport, not covered by anything drawn over them (an overlay, a dialog), and not cut short by their box (an ellipsis, `overflow: hidden`); a failure says which |
| `absent` | `--absent`, `-a` | the page does not show these words |
| `url` | `--expect-url`, `-u` | the final address contains this |
| `url_regex` | `--expect-url-regex` | the final address matches this pattern |
| `status` | suite only | the page answered with this HTTP status, such as 404 for a removed page (that status is then not also filed as a finding) |
| `js` | `--expect-js`, `-j` | this JavaScript expression returns exactly `true` in the page (a Promise counts by what it resolves to, within 5 s). Anything else fails: a string fails with that string as the reason, so `cond || 'why it failed'` reads well in the report; `false` and `null` fail as such; any other value (a number, an object, an element) fails as the wrong type; a throw, a rejection or no answer fails with the error. What came back is kept in the report, pass or fail |
| `fetch` | `--fetch URL[=STATUS]` | a request made from the page answers with that status (default 200) |
| `command` | suite only | a shell command prints the expected output (needs `--allow-commands`) |
| `effect` | suite only | off/on PNG captures of a named region have at least the required fraction of changed pixels |
| `looks` | `--expect-looks` | Clef, looking at the final screenshot, judges this statement true (needs Clef: see below) |

Text is compared the way a person reads it: line breaks and runs of spaces count as one space. Use
`ignore_case: true` for labels styled in capitals.

## Check that an effect draws

`expect.effect` compares rendered pixels without a model call. Give it explicit off and on actions, a named rectangle
in viewport CSS pixels, and `min_changed`, a fraction greater than 0 and at most 1. A pixel counts as changed when
the largest red, green or blue difference is greater than `tolerance` (an integer from 0 to 255, default 8).
The measured fraction must meet or exceed the minimum. Identical captures fail with zero changed pixels.

```yaml
devices: [desktop]
scenarios:
  - name: the shadow draws
    about: Enabling the shadow changes the ground next to the subject.
    url: http://127.0.0.1:3000/
    expect:
      effect:
        name: subject shadow
        region: {name: ground next to subject, x: 40, y: 40, width: 400, height: 200}
        off:
          - js: document.querySelector('#subject').style.boxShadow = 'none'
        on:
          - js: document.querySelector('#subject').style.boxShadow = '20px 20px 8px #222'
        tolerance: 8
        min_changed: 0.01
```

Use your application's state actions and `wait_for` hooks for a game or shader. Keep the camera, time and other
features fixed between captures; an off/off control helps select a useful tolerance and minimum. Place the region
over the visible effect, away from labels and controls that also change. The check measures the changed region;
the two state actions isolate the feature being tested. A true feature flag alone cannot satisfy it.

The state actions support the same hooks as `before` and `after`. Nested commands require `--allow-commands`;
existing host and read-only guards still apply. The final state is on. Ordinary page checks and `looks` then read
that final state. A scenario with multiple clients cannot use this check.

The report and MCP result keep full counts, fraction, thresholds and region bounds, with off, on and difference
PNGs. Magenta marks changed pixels in the difference image. These three required evidence images are retained
even with `--no-shots`, which disables optional end screenshots. Invalid or missing PNGs, unequal dimensions,
out-of-viewport regions and oversized evidence are harness errors, so they cannot pass. Each capture must fit the
comparison's 16,384-per-axis/16,777,216-total-pixel limit and the 32 MiB evidence-file limit.

Use `qajev run` or `qa_run_suite` with the same suite. `qajev plan` describes the comparison before it runs. A planted
fault can name the comparison's exact plan words with `expect_fail`, as for other checks. Report downloads keep all
three PNGs; Cloud publication needs the matching CLI and evidence-uploader versions on its runner.

## A canvas the screenshot cannot show: `canvas_capture`

QAJev reads the end screenshot of each web test. When the page's main canvas is one flat dark colour in that picture,
the test is harness (see [Reports](reports.md)). Some browsers leave a WebGL or WebGPU canvas out of their screenshots
while the page draws. Then give the suite (or one scenario) the canvas's own readback:

```yaml
canvas_capture:
  page: "document.querySelector('canvas').toDataURL()"   # a data:image/...;base64 URL, or a Promise of one
  gpu: hardware                                          # optional: the page's WebGPU adapter must be a real GPU
```

At the end of the test QAJev runs `page` itself, saves the image in the report (`shots/<test>-canvas.png`) and grades
that image, not the screenshot. A readback that is one flat dark colour is harness: the page drew nothing. An empty
canvas's readback is transparent, and QAJev reads it as black. If the expression fails, gives nothing or gives no image,
the test is harness too. With `gpu: hardware`, QAJev also reads the page's WebGPU adapter. No adapter, a fallback
adapter, or a software one (SwiftShader, llvmpipe, lavapipe...) makes the test harness. A failure stays a failure, with
the note. For a WebGL or WebGPU canvas, draw with `preserveDrawingBuffer: true`, or read the canvas in the same frame
as the draw, else the readback is empty. QAJev saves the readback even with `--no-shots`.

The readback runs in QAJev's own Chrome tab only. A scenario with `clients` cannot have `canvas_capture`, and a
suite-level `canvas_capture` cannot reach one: such a suite is refused when it loads, so give `canvas_capture` to each
scenario that needs it. In a device browser (`real_devices`), no readback runs, so that copy of the scenario is
harness.

## What only a look at the screen tells (Clef)

Write one visible claim per `looks` entry. Each entry gets its own model question and probability; claims are not
averaged together. Existing strings still use the full screenshot. For small features and relative claims, give an
explicit crop and a labelled reference instead:

```yaml
expect:
  looks:
    - claim: The left hand closes around the grip.
      crop: {name: left hand, x: 100, y: 240, width: 160, height: 160}
    - claim: The right hand closes around the grip.
      crop: {name: right hand, x: 300, y: 240, width: 160, height: 160}
    - claim: The scene has finished materials rather than placeholder blocks.
      crop: {name: scene, x: 0, y: 0, width: 640, height: 360}
      reference: {path: references/blockout.png, role: blockout}
    - claim: The enabled panel has a visibly stronger glow.
      crop: {name: enabled panel, x: 320, y: 0, width: 320, height: 240}
      different_from:
        crop: {name: disabled panel, x: 0, y: 0, width: 320, height: 240}
        tolerance: 8
        min_changed: 0.05
```

Crops use integer **source-frame pixels**, not viewport CSS pixels. Choose them from the full-size screenshot for
that device. A crop must fit the frame. A structured claim uses a lossless, unmarked PNG capture on the web;
read-only guards remain active. The model receives only that claim's crop and its explicitly labelled reference or
comparison panel. Split a sentence such as “both hands hold the grip” into two claims and two crops; QAJev does
not guess where to split prose or where the hands are.

`different_from` compares both crops from the same captured frame. Their pixel dimensions must match. It uses the
same RGB tolerance and changed-fraction rule as `effect`. An identical or too-similar panel fails before a model
call, even if a model would pass it. Select a threshold with an unchanged control and a visible passing example.

A reference's `role` is `expected` (a known target for the relative/quality claim) or `blockout` (an unfinished
negative example). The model is told which it is. An exact pixel match to a block-out fails without a model call;
other quality judgments still depend on the model. References must be explicit relative PNG/JPEG/WebP files below
the suite or project-config folder, outside hidden folders, with no symlinks or traversal. Cloud inputs include
only references of selected checks. Inline suites have their temporary suite folder as their base; use a suite
file when it needs reference files.

Reports retain the exact crop/reference/panel PNGs, difference image, bounds, comparison metrics and probability.
These required images survive `--no-shots`, MCP output and Cloud uploads/downloads. Missing/invalid image evidence
cannot pass. Existing image limits apply; crops decode at most 32 MiB and 16,777,216 pixels, then each model image
must fit Clef's 4 MiB limit and the images together must fit its 8 MiB combined limit. Each claim costs its own
model request, within the run's existing cost cap.

The same structured entries work in a native `steps:` suite's ordinary check/goal/play, pad, keys or relaunch
steps. `qajev plan` validates and names them before the run. Put a visual claim on a separate check step after a
`js`, handoff, renderer-crash or `open_url` action; those actions cannot judge it. Real-device browser conversion
does not yet carry `looks` and marks that leg skipped with a reason.

These checks do not establish a measured model false-pass rate. Calibrate claims and references against known
passing and failing frames before relying on them as a release gate.

Every check above reads the page. Some bugs only show in the picture: a button cut off by a box too small for it,
text the same colour as its background, a price drawn on a canvas, a label that is an image. With Clef as the decision
model (Cloudflare's open models, which read images: see [Configuration](configuration.md#keys)), two things use the
screenshot:

- **`looks`**: statements about what the screen shows, each judged by Clef from the final screenshot. A statement
  passes when Clef gives it a probability of 0.5 or more; the report shows that probability next to each one.
- **`vision: true`** (`--vision`): Clef sees the screen with every decision, not only the page's text. Use it when the
  way forward is only in the picture: drawn or image-only buttons, canvas apps, icons without labels.

```yaml
scenarios:
  - name: the sign-up button can be read
    url: /join
    expect:
      visible: ["Sign up now"]          # the words are on screen...
      looks: ["The 'Sign up now' button is fully visible", "The button's words are readable"]  # ...and legible
  - name: pricing from a drawn menu
    url: /plans
    vision: true                        # the menu buttons are pictures
    goal: Open the pricing page. Stop when the page heading says Pricing.
    expect: {url: /pricing}
```

Write a `looks` statement the way you would brief a person looking at a screenshot: one plain fact, and specific
(`The Pro plan costs $29 per month`, not `the prices look right`). Each looks claim is one Clef call, on top of the
scenario's decisions. `vision` goes on a scenario or on the whole suite,
and makes every decision larger: a screenshot is about 1,000 extra input tokens, about $0.0001 with Clef-flash and
$0.00024 with Clef. Both need Clef: with Jev, which reads text only, QAJev refuses the run and says what to set.

## Desktop and phone

Every website test runs **twice by default: on desktop and on a phone** (a 390×844 screen with touch and an
iPhone user agent, like a browser's device mode; it ends in `QAJev/<version>`, as the desktop one does). The phone copy of a scenario is named `... (phone)` in the report,
and costs the same again. The smoke crawl checks every page on both too.

| To | Do |
|---|---|
| test one device only | `device: desktop` on the suite or a scenario, or `--device desktop` |
| choose the devices | `devices: [desktop, phone, tablet]` in a suite or project, or `--devices desktop,tablet` |
| change the default everywhere | `QAJEV_DEVICES=desktop,phone,tablet` |

For a phone's real browser (Chrome on an Android emulator; iOS Safari is not readable yet), see
[Mobile](mobile.md): it catches what only a real device shows, and is a separate run.

## Writing good goals

- **One intention per scenario, ending with "Stop when ...".** Say what the visitor sees when they are done.
  - Good: `Find the refund policy. Stop when the number of days for a refund is visible.`
  - Too vague: `Check the help pages.`
- **Always add expectations, and make them strict.** A word that also appears in the menu proves nothing. Prefer a
  precise `js` check or a `fetch` of the side effect.
- **Give every value Jev must type**, and make it different from the field's placeholder (Jev may read a
  placeholder as already filled in).
- **Start close to the target.** Jev does not scroll far on its own; give a `url` near what you are testing.
- **Name things uniquely in your product.** When two buttons share a name, Jev gets confused, and so do people.
  That is a real finding.
- **Personas help.** `--persona "You are on your phone and have never seen this site."` changes how Jev reads the
  page.
- **Say what the test proves.** `about` (`--about` on the command line) is what a pass means and why it matters, in
  plain words: "a first-time visitor can find the Pro price without signing in". The report and the dashboard show
  it under the test's name, so whoever reads the result knows what was, and was not, proven.
- **Say what each check proves.** A text, an address or an HTTP status says itself ("the page shows “Paid”"). A
  `js`, `url_regex`, `fetch` or `command` check does not: give it `says`, its words in plain language
  (`--says` with `--expect-js` on the command line). The report and the dashboard open with a **test plan**: each
  test's `about` and its checks in these words, a box for each, ticked as the run goes. A test without an `about`,
  or with a check without words, is **NOT DESCRIBED**: the plan flags it and the gate cannot be PASS (it is
  INCOMPLETE), because its pass would not say what it proved. `qajev plan FILE` shows the plan and lists them
  without running anything ([CLI](cli.md#qajev-plan)).

## Planted faults: a test meant to fail

A planted fault proves a check can catch a fault: you break the page on purpose, and the check must fail. Mark such
a test with `expect_fail`, naming the one check it plants against by its `says` text (or its words as `qajev plan`
prints them). `expect_fail: true` works only on a test with exactly one check. The test must say in `about` what it
plants.

```yaml
- name: wrong price
  url: /pricing?plant=price
  about: plants a wrong Pro price, so the price check must catch it
  expect:
    text: ["Pro"]
    js: "document.querySelector('.pro .price').textContent === '£10'"
    says: { js: "the Pro plan costs £10" }
  expect_fail: "the Pro plan costs £10"
```

On the command line: `qajev check URL ... --says "the Pro plan costs £10" --expect-fail "the Pro plan costs £10"`
(a bare `--expect-fail` names the test's only check).

The test keeps its real outcome: a caught planted fault has `outcome: "fail"` and `graded: "pass"`.
The gate uses `graded` this way:

| The planted test | Grade used by the gate | Reason |
|---|---|---|
| fails on the named check, and every other check passes | `"pass"` | "failed as expected on the planted check: ..." |
| passes | `"fail"` (gate FAIL) | "expected to fail, but every check passed: the planted fault was not caught" |
| fails on another check, or before the named check ran (a page that did not load) | `"fail"` (gate FAIL) | "failed, but not on the planted check: ..." |
| stuck or harness, or the run broke off (a crash, a time-out) | `"stuck"` or `"harness"` (gate INCOMPLETE) | never a catch |

So a planted fault that stops being caught, or breaks somewhere else, turns the gate red. The report counts the
planted tests at the top ("N test(s) expected to fail") and tags each one with the check it names, so no real
failure can hide under the tag unseen. A scenario with `clients` cannot be planted. An `expect_fail` that names no
check, or more than one, is refused before the run (exit 3), and `qajev plan` refuses it too.

## An approved test plan

Before a run, write down what it will prove and have it approved. The plan is a Markdown file in the project, for
example `tools/qa/plans/2026-10-08-checkout.md`: why the run exists, each test and what a pass means, the fault each
test catches, and what you expect. The approver adds one line at the end:

```text
Approved by the Orchestrator 8 Oct 2026 10:34 sha256:<qajev plan-hash FILE>
```

The sha256 is of the plan text above that line, so a plan edited after its approval is **not approved** until it is
approved again. The last approval line counts: an addition below an earlier approval gets its own line, which then
covers the whole plan. Text below the last approval line is not approved.

A suite names its plan with `plan:` (from the suite's folder, or a folder above it up to the project root: the folder with `.qajev/` or `.git`, never above it); a
check, a smoke or a play takes `--plan FILE` ([CLI](cli.md#the-approved-test-plan-on-a-run)). `qajev plan FILE`
checks it without running anything. A run without an approved plan warns today; `QAJEV_REQUIRE_PLAN=1` refuses it.
`QAJEV_PLAN_APPROVER` changes the name the approval line must carry (default: `the Orchestrator`).

```yaml
name: checkout
plan: tools/qa/plans/2026-10-08-checkout.md
```

## A suite: several scenarios in one file

Start from a template:

```bash
qajev init qajev.yaml     # writes a starter suite
qajev run qajev.yaml
```

A suite in full:

```yaml
name: Shop
about: a visitor can see what we sell and what it costs   # what the whole run proves
base_url: http://localhost:3000
mode: readonly            # readonly (default) or mutate (a local dev host only: localhost, 127.0.0.1, [::1],
                          # *.localhost, *.test; any other host is refused, exit 5)
allow_destructive: false  # true shows delete, remove, refund, cancel... (a local dev host only; red report banner)
device: desktop           # desktop | tall | phone | tablet | WIDTHxHEIGHT
persona: "You are a first-time visitor."
budget: {actions: 20, seconds: 90}   # per scenario (at most 60 actions)
browser: fresh            # fresh (default): a new Chrome for each test; shared: one Chrome for the whole suite
cost_cap_usd: 0.50        # for the whole run; checked before every paid call
settle: 10                # seconds to wait for expectations after Jev stops
motion: reduce            # reduce (default): pages are told the visitor prefers less motion; full: as-is
cpu_throttle: 1           # run the page's CPU this many times slower (1 to 8), e.g. 4 for a phone-like phone pass
color_scheme: dark        # light | dark: the page's prefers-color-scheme answers so (default: the browser's own)

scenarios:
  - name: the home page loads
    url: /
    expect:
      text: ["Welcome"]
      absent: ["Something went wrong"]

  - name: a visitor finds the Pro price
    about: the Pro price is one click from the home page, with no sign-in
    url: /
    goal: Find out how much the Pro plan costs. Stop when its monthly price is visible.
    expect:
      url: /pricing
      visible: ["$29 per month"]

  - name: pricing on a phone
    about: both plans fit a phone screen
    url: /
    device: phone
    goal: Open the pricing page. Stop when the plan prices are visible.
    expect:
      js: document.querySelectorAll('[data-plan]').length === 2
      says: {js: the page lists both plans}   # also url_regex, fetch, command: what each proves, in plain words

  - name: dark mode follows the system
    about: on a system set to dark, the site draws itself dark without a toggle
    url: /
    color_scheme: dark        # a scenario's own wins over the suite's and over --color-scheme
    expect:
      js: getComputedStyle(document.documentElement).colorScheme === 'dark'

  - name: a visitor sends feedback
    url: /feedback
    mode: mutate
    goal: >-
      Send feedback as Ada Lovelace, email ada@example.test, with the message
      "The pricing page is clear." Stop when a thank-you message is visible.
    expect:
      text: ["Thanks, Ada Lovelace"]
      fetch:
        - {url: /api/feedback, status: 200, contains: "ada@example.test"}

  - name: the thank-you page links home   # no url: continues in the same tab, after the step above
    goal: Go back to the home page. Stop when the home page is visible.
    expect: {url: /}
```

A scenario **without a `url`** continues in the same tab and depends on the one before it: if that one did not
pass, this one is skipped.

**Each test gets a fresh Chrome** (`browser: fresh`, the default). A form left open, a dialog or a hung page in one
test cannot hold up the next one. A `depends_on` chain keeps one Chrome, because its tests share a tab on purpose.
A fresh Chrome costs about 2-3 s for each test. It uses the same profile, so a sign-in kept in the profile still
holds. A suite's `account` signs in again in each fresh Chrome, because a new Chrome has no session cookie.

Each test has a cap: its `budget.seconds` plus 60 s, so 150 s by default. A test still running at its cap is
stopped with its Chrome and graded `harness`, "cap 150 s hit". The tests after it in its chain are skipped. A wait
for a quiet machine does not count toward the cap. A test still in that wait after the run's load budget
(`--load-wait`) is stopped and graded `harness`, "the machine stayed busy". Each step before a test (Chrome up, the sign-in, the step
between two tests) has 60 s, set by `QAJEV_FRESH_START_WAIT`.

`browser: shared` runs the whole suite in one Chrome, as QAJev did before. QAJev also uses one Chrome, and the report
says why, when the run attaches to a Chrome with `--cdp-url`, when the profile's Chrome is already open, or with
`--jobs 2` or more on a kept profile (one Chrome at a time can use a profile folder).

Useful options when running a suite:

```bash
qajev run qajev.yaml --only "a visitor sends feedback"   # one scenario (plus what it depends on)
qajev run qajev.yaml --headless --ephemeral              # invisible, and a throwaway browser profile
qajev run qajev.yaml --cost-cap 0.10                     # a lower cap for this run
qajev run qajev.yaml --jobs 2                            # independent chains in parallel
```

## Hooks: direct steps before or after Jev

For long forms or setup, drive the page directly and let Jev do the decisions:

```yaml
  - name: rename a tab
    mode: mutate
    before:
      - fill: {"#tab-name": "QAJev test tab"}   # ${ENV_VAR} is expanded
      - click: "button[type=submit]"
    goal: Check the new name is in the list. Stop when you see it.
    expect: {text: ["QAJev test tab"]}
    after:
      - key: Escape
```

Hooks: `js`, `click` (a CSS selector: scrolled into view, then clicked once it is on top and still, waiting up to
2 s for a splash or a settling panel; a target still covered fails naming what covers it), `click_at` (a real
click at a point: `{selector: "iframe#view", x: 120, y: 80}` is CSS pixels from the element's top-left corner, and
without `selector` the pixels are the viewport's. It reaches into an iframe, such as a WebGL canvas, and a point that
something else covers, or that lies outside the element, fails naming why. When the point is known only at run time,
`{selector: "iframe#view", js: "window.findBench()"}` takes it from an expression that returns `{x, y}` (it may await).
The expression runs on the top page: it can read a same-origin frame through `contentWindow`, not a cross-origin
one), `fill`
(`{selector: text}`: a date, time or month field gets its value set, in the form Chrome stores it, such as
`2027-06-30`; a fill whose text never reaches the field fails, saying so), `navigate`, `wait_for` (JavaScript that must turn
true, within `timeout` seconds: 15 by default, at most 300, e.g. `{js: "window.ready", timeout: 60}`; a Promise counts by what it resolves to; a wait, unlike a check, holds on any truthy value), `key` (a real key press, below), `sleep` (up to 30 s), `reload` (`reload: true`: the same page again, keeping its
cookies and storage), and `command` (a shell command, only with `--allow-commands`). Hooks refuse to click dangerous controls or fill password fields on a real site.
A scenario's checks judge the page as it is after its `before:` hooks, so a `wait_for` there is what they see.
Its `after:` hooks run after the checks: a result an `after:` hook writes can never change the verdict, so a
hook that plays the page and records a result belongs last in `before:`. `expect` js and hook js run in separate
JavaScript worlds: they share the page's DOM (so a hook can leave its result in a `data-` attribute), not its
window globals.

A `key` hook presses real keys, as a player does: the page gets trusted `keydown` and `keyup` events. Jev clicks and
types but cannot press a game's keys, so a game's real-key test drives them with hooks. `qajev plan` flags a goal that
asks Jev to press a key (PRESSES KEYS). A chord adds Shift, Ctrl, Alt
or Meta to one key, e.g. `key: Shift+A` or `Ctrl+Shift+KeyK` (the page sees `event.shiftKey` and the rest; react
policies take chords too). With Ctrl or Meta, the browser's and the system's own shortcuts (Q, W, R, T, N, L, P:
quit, close, reload, a new tab or window, the address bar, print) are refused.

```yaml
- key: Escape                                       # once
- key: Backquote                                    # a key's code works as its name (here: `)
- key: {press: [f, j], repeat: 15, interval_ms: 30} # f, j, f, j... 15 times: alternating fast
- key: {press: Space, hold_ms: 1200}                # held down for 1.2 s, then released
```

The keys are plain key names: a letter, a digit, punctuation (`` ` - = [ ] ; ' , . / \ ``), `Space`, `Enter`,
`Escape`, `Tab`, `Backspace`, `Delete` and the four arrows, each optionally with Shift, Ctrl, Alt or Meta as a chord
(above). A browser or system shortcut is refused, so a hook cannot quit, close or reload the browser. On a
production page, Delete and Backspace reach the page only inside a text field ([Safety](safety.md)). Bounds: `hold_ms` up to 5000, `repeat` up to 200, `interval_ms` up to 2000, and one hook at most
30 s in all. A suite with a key outside them is refused before it runs.

A `pad` hook works a gamepad, with no real device. Chrome has no gamepad input of its own, so QAJev puts one virtual
pad in the page before the page's code runs: `navigator.getGamepads()` returns it, with the W3C standard mapping,
`connected` true, and a timestamp that goes up on every change. The page also gets a `gamepadconnected` event. A
scenario without a `pad` hook keeps the browser's own `getGamepads`. Only a page that reads the Web Gamepad API sees
the pad (not Steam Input or a native module).

```yaml
- pad: A                                               # press A once (held 80 ms)
- pad: {press: [A, DpadDown], repeat: 3, interval_ms: 100} # a sequence, 3 times
- pad: {stick: left, x: 1, y: 0, hold_ms: 500}         # left stick right for 0.5 s, then centred
- pad: {trigger: RT, value: 1, hold_ms: 200}           # pull RT fully for 0.2 s
```

The buttons are `A B X Y LB RB LT RT View Menu LS RS DpadUp DpadDown DpadLeft DpadRight`. `Home` is refused: it opens
Steam's or the system's overlay. Sticks are `left` and `right`, with `x` and `y` from -1 to 1 (`y` 1 is down).
Bounds: `hold_ms` from 34 (two frames) to 5000, `repeat` up to 200, `interval_ms` up to 2000, and one hook at most 30 s.
A suite with a pad input outside them is refused before it runs.

A `react` hook plays in real time: every `every_ms` (default 50) it runs a small policy in the page, and sends the
keys the policy returns. It is for a cue too short for anything slower, such as a 0.6 s flash: the policy reads what the
player can see, waits a human reaction time, and answers.

```yaml
- react:
    every_ms: 50          # 20 to 1000
    for_s: 60             # at most 180; without `until` it simply runs this long
    until: "!!document.querySelector('.verdict')"   # ends the hook; never true in time = the hook fails
    js: |
      (() => {
        const v = game.view(), now = performance.now();
        if (v.flash && !window.seenAt) window.seenAt = now;          // the cue showed
        if (window.seenAt && now - window.seenAt >= 300) {           // a person reacts after 0.3 s
          window.seenAt = 0;
          return [{down: v.flashKey}, {shot: 'flash'}];              // hold its key; keep the frame
        }
        return v.locked ? {up: v.flashKey} : null;
      })()
```

A policy returns nothing, or one action or a list of them: a key name or `{press: k}` (down and up), `{down: k}`,
`{up: k}`, and `{shot: label}` for a screenshot of that moment (in the report as a frame, at its time). In a tick,
the keys are sent first and the frames taken after, so a frame never delays a press; the report logs when each
held key went down and up, and when each frame began and how long it took (`at_s`, `took_s`). The keys are
the same allow-list as `key`, at most 10 actions a tick. A key held 5 s is released (and the report says so), and
every key still down is released when the hook ends, fails or the policy throws. Keep the reaction delay in the
policy, as above: then the test shows how fast a player had to be.

## Guard options

The guard hides dangerous controls and blocks writes (see [Safety](safety.md)). Destructive controls (delete,
remove, refund, cancel, archive, reset...) are hidden on every host; `allow` never shows them, only
`allow_destructive: true` does, and only when every host is a local dev host. It reads everything a person could
read on a control: its text, `aria-label`, `title` (the tooltip) and an input's value. A stuck or harness result
names what it held back ("guard hid: 'Shop Show clothes and gear you can buy' (danger: buy)"), and so does the
report: each hidden control with the rule that hid it and its page (`guard_hidden_controls` in the JSON). In the
report's pictures a dashed box labelled "hidden by guard" marks where each one was, so a missing button never reads
as the page's bug. A suite can tune it:

```yaml
hosts: [accounts.example.com]      # other sites Jev may visit; anything else stops the scenario
guard:
  deny: ["\\bexport all\\b"]       # more button labels to hide (regular expressions, any case)
  allow: ["^See plans and subscribe$"]  # exceptions to the built-in hidden list (on a local dev host; on
                                       # production only to a read-only rule, never to a dangerous control)
  allow_requests: ["/graphql$"]    # read-only: let these writes through, listed in the report (production: POST only)
  block_urls: ["*://*/logout*"]    # never load these addresses
  redact_emails: false             # hide e-mail addresses from Jev
```

With `block_urls`, each test's result lists every request the page or any frame in it sent to a blocked address:
`block_list_requests` in `report.json`, each with its `method`, `url`, the `frame` (the document that sent it) and
`blocked` (Chrome stopped it). An empty list says nothing tried. A write that the read-only guard stopped in the page
never reaches the network, so it is not in that list: it is in `blocked_writes`, which now holds the writes stopped
inside frames too, each with its `frame`. Together they show a GET (information) apart from a save (a POST). The report
lists the first 20 as "Block list" lines.

QAJev reads and guards every frame in the page. A frame on another site runs apart from the page (Chrome's site
isolation): from 0.4.12, QAJev attaches to it before it runs and puts the read-only guard and `block_urls` in it, so its
writes are stopped and listed, with its `frame`, like the page's own. **Versions 0.4.11 and earlier did not:** such a
frame could send writes and reach blocked addresses, even in a readonly run.

A page's Web Workers (and a frame's) get `block_urls` before they start, so a worker's request to a blocked address
never leaves. The read-only guard itself lives in pages and frames, not in workers. **Versions 0.4.12 to 0.4.14 held
every worker at its start:** a page that waits on a worker (a stream player choosing its video pipeline, say) waited
forever. 0.4.15 lets each worker run at once.

If QAJev cannot guard such a frame (attaching failed, or the frame started before the guard), it names it in
`frames_not_observed`. The report says "Frame not observed: <address>. QAJev's read-only guard and block_urls do not
reach it, so it may send writes", and the test's outcome is harness in every mode, because the run is not proven
read-only. A frame whose document has not loaded yet (a video still starting) has no address of its own: QAJev names
it by its iframe's `src`, read from the page's DOM. One with no web `src` either is "a frame with no address yet", and
stays not observed. A suite can accept one such frame by its exact host, with the reason:

```yaml
guard:
  unguarded_frames:
    - {host: www.youtube-nocookie.com, why: town screen live stream}
```

An accepted frame that QAJev could not guard is still listed (`frames_accepted` in `report.json`; "Frame <address>:
accepted: not guarded (<why>)" in the report), and it does not make the test harness. One QAJev guards needs
nothing. Any other frame on another site still does. Host names
are exact: no wildcard, scheme, port or path. For a frame with no address yet, the host is its iframe's `src`, which is
what the page says the frame loads, not proof of where it goes: page script can change it, or send the frame elsewhere.
**An accepted host is a reviewed exception.** The Orchestrator approves
each one. The agent that wrote the suite never adds one on its own.

Each test also lists what its frames said and asked for: `frame_console` in `report.json` (each frame's console
log, info, warn and debug lines, its errors, exceptions and rejections) and `frame_requests` (each request a frame made
that failed, or still waited after a second, with its state, such as "pending 38 s" or "failed:
net::ERR_CONNECTION_REFUSED"). Each item names its `frame`. The report shows the first 20 of each as "Frame console"
and "Frame request" lines. A framed tool that stays on "loading…" then shows whether it waits on a request or on its
own work.

## Dev servers on this machine, from QAJev Cloud: `tunnel:`

A suite whose site runs on this machine's dev servers can still run in QAJev Cloud. `tunnel:` names each port and the
command of the dev server that answers it. The dispatcher on this machine opens one tunnel per run to those ports. It
carries WebSockets, and writes when the suite writes on local data.

```yaml
base_url: http://127.0.0.1:8871
hosts: [127.0.0.1:8871, localhost:8787]
mode: mutate                # writes need data: local
tunnel:
  ports:
    8871: {command: "node serve-folder.mjs"}
    8787: {command: "wrangler dev"}
  data: local               # fixture accounts only, no production data
  test_keys: [STRIPE_SECRET_KEY]   # env keys the dev server may hold, when they are test keys
```

- **Each port names its command**: the words of the dev server's real command line, with its script or subcommand.
  `node` alone, or `npx --yes`, is refused. Before the run starts, this machine checks that exactly that process
  holds the port, that it runs from inside the project's git checkout, and that it is not a tool, a debugger or a
  database. Any other process on the port, or a port the suite uses but does not name, refuses the run.
- **Only `127.0.0.1` and `localhost`**, ports 1024 to 65535, at most 8. Run the suite from its git checkout.
- **Writes need `data: local`.** The machine also refuses a dev server bound to remote resources or holding live
  credentials. `test_keys` names keys whose test values it may hold.
- **Where it goes.** With `tunnel:`, `--runner auto` and `qajev mode cloud` send the suite to the cloud too. Without
  it, a run on one port still goes only with `--runner cloud`, read-only. A project env takes the same `tunnel`
  table (see [Projects](projects.md)).
- Each run's tunnel ends with the run, or at its time limit plus 10 minutes.

## Multiplayer: several players at once

A scenario with `clients:` opens several players, each in a browser context of its own: its own cookies, storage,
cache and service workers, as if on different machines. Steps drive them together, then the checks hold on every
player and **across** them: same roster, same chat in the same order, a player who reloads comes back as itself. No
Jev and no model calls: it costs nothing.

```yaml
device: desktop                    # one device: otherwise the whole room runs again on a phone
scenarios:
  - name: five players share one room
    about: five players join one room and see the same roster and every chat once, in order
    url: http://127.0.0.1:8765/play?room=qa-{run}&player={client}
    clients: 5                     # p1..p5; or [host, guest]; or [{name: host, url: ...}, ...]
    state: "({id: game.id, players: game.players, chat: game.chat})"
    steps:
      - all: {wait_for: {js: "game.ready", timeout: 20}}     # every player, at once: a barrier
      - all: {js: "game.say('hello')"}
        stagger: 100               # one after another, 100 ms apart (jitter: 50 adds ±50 ms at random)
      - snapshot: chat             # every player's state, once `until` holds on each, and when it did
        until: "game.chat.length >= 5"
        timeout: 5
        expect:
          - check: every player got the same chat, in order
            js: "clients.every(c => JSON.stringify(c.state.chat) === JSON.stringify(clients[0].state.chat))"
      - client: p3                 # one player (or a list)
        reload: true               # the same page again; its storage stays
    expect:
      text: ["Room qa-"]           # holds on every player, reported per player: "[p3] page shows ..."
      across:                      # judged over every player's state, as `clients` and `snapshots`
        - check: everyone sees the same roster
          js: "clients.every(c => JSON.stringify(c.state.players) === JSON.stringify(clients[0].state.players))"
    settle: 5                      # checks retry for up to 5 s, so the room can settle
```

- **The address**: `{client}` is the player's name, `{i}` its number (1 to N), `{run}` a token new for each run, so
  each run gets a fresh room.
- **Steps**: `all` acts on every player at the same instant, unless `stagger` or `jitter` spreads them out (in ms;
  `seed:` on the scenario repeats a jitter). An offset is the earliest a player acts: on a busy machine a player can
  start late (tens of ms, more under heavy load), never early, so measure delays with snapshots, not offsets. `client` acts on some. A step is a hook (`js`, `click`, `fill`, `key`,
  `wait_for`, `sleep`, `navigate`, `reload`); a failed hook names its player and stops the scenario
  (`harness`, naming the steps that never ran: a scenario that stopped early never passes).
- **`state`** is a JS expression read on every player. `across` checks and a snapshot's `expect` see `clients`,
  one `{name, url, at, state}` per player (`at` is when it got there, in ms on the machine's clock), and
  `snapshots.<name>` for every snapshot so far. Compare `at` across players to measure how long a move took to
  reach everyone. They run in a blank tab of QAJev's own: the page cannot change them. Each check gets its own copy
  (one that sorts `clients` does not change what the next one sees), whatever the states' size.
- **The report** shows each player's screenshot, when each player acted in each step, and when each reached each
  snapshot (ms after the first, or "timed out").
- The guard is armed for every player as in any scenario: `mode: readonly` blocks writing requests (WebSockets pass);
  `mode: mutate` works on a local dev host only. A scenario with clients has no goal, persona, or before/after hooks.

## Signed-in areas

Two ways in. Jev never types a password in either, and QAJev types one only for a seeded test account on your own
machine (since 0.4.0).

**On any real site, sign in once yourself** in QAJev's own browser profile; the profile keeps that session:

```bash
qajev browser login --profile shop --url https://shop.example/login   # a window opens: sign in, then close it
qajev run account.yaml --profile shop
```

This works for every kind of sign-in: a password, one-time codes, passkeys, "Sign in with Google". QAJev never sees
or stores the password.

**A seeded test user on a local dev host** (below) is the only account QAJev signs in by itself. Its password comes
only from your app's seed fixture (`password: seed:FILE#KEY`). A `keychain:`, `op://` or `env:` password is refused
when the suite loads, on every host, with a message that points to `qajev browser login`: a Keychain entry cannot
show that it belongs to a test account, and the fixture's `"test_account": true` can.

Before the first scenario, QAJev itself opens the sign-in page in its own tab, types the email, reads the password
from the fixture and types it into the password field, submits, and checks it got in. It types only once the page has
loaded, and checks the fields still hold what it typed just before it submits: a page whose scripts start late can
clear them. It types them again once, then stops with the reason. Then the scenarios run as
usual, guarded and already signed in. The password is never in Jev's prompt, a report, a screenshot or a log (it is
redacted from everything QAJev writes), and Jev still cannot type into password fields. If the site does not let the
account in, the run stops with the reason (`harness: sign-in failed: Wrong email or password`) instead of leaving Jev
at a sign-in page.

The sign-in page must be `https` (plain `http` only on localhost), on a local dev host the fixture's `allowed_hosts`
names, checked again where the browser is just before it types; the password only goes into a password field.
`login` also takes `email_field`, `password_field` and `submit` (CSS selectors, when the defaults do not find them),
`open` (the button that shows the sign-in form, such as "Sign in with email": a CSS selector, `{text: "Sign in"}`
for a button found by its visible text, or a list of them clicked in order; a step whose control does not show while the
email field is already there is skipped), `next` (the button between the email
and password steps of a two-step sign-in), `tab` (`own`, the default: QAJev signs in in a tab of its own, which then
closes; `same`: in the scenarios' own tab, for an app that keeps one writer per site and must not see a second tab), and
`signed_in` to say how success
looks (`{url_not: /login}`, `{text: ["Your orders"]}` or `{js: ...}`; by default: away from the sign-in page with no
password field showing).

**A seeded test user on a local dev host** signs in by itself, second factor included. Your app's seed creates the
user and writes its secrets to a JSON fixture that marks itself as a test account:

```json
{"test_account": true, "allowed_hosts": ["localhost", "127.0.0.1"], "email": "qa-test@example.test",
 "password": "...", "totp_secret_base32": "...", "session": "..."}
```

```yaml
account:
  name: qa-test
  email: seed:dev_support/qa_test_user.json#email          # FILE#KEY, relative to this suite (or the project's repo);
                                                           # KEY may be a dotted path: #A.email in {"A": {"email": ...}}
  password: seed:dev_support/qa_test_user.json#password
  totp: seed:dev_support/qa_test_user.json#totp_secret_base32  # the code after the password: TOTP, SHA1, 6 digits, 30 s
  login: {url: /login}
```

Or skip the form with a session cookie the seed minted (no password needed):

```yaml
account:
  email: qa-test@example.test
  cookie: {name: app-session, value: "seed:dev_support/qa_test_user.json#session"}  # http_only: true by default
  login: {url: /dashboard, signed_in: {text: ["Your orders"]}}  # a page that shows the user is signed in
```

`totp` and `cookie` take only a `seed:` reference: the secret comes from the app's own fixture, never from a person's
keychain, 1Password or environment. QAJev refuses them unless all of these hold:

- the sign-in page is a local dev host: localhost, 127.0.0.1, `*.localhost` or `*.test`, written plainly (an
  address a browser could read as another host, with a backslash, `user@` or control characters, is refused);
- the email is at a reserved test domain: `example.test`, `*.test`, `example.com`, `*.example`;
- the fixture says `"test_account": true` and its `allowed_hosts` list names the host (the list is required).

The page must still be on that host when the code is typed, or after the cookie's page loads. QAJev types the code
into the page's one-time code field: `autocomplete="one-time-code"`, a `000000` placeholder, or a field named exactly
`otp`, `totp`, `code`, `mfa_code`, `verification_code` and the like (never `postcode` or `coupon_code`);
`login.code_field` and `login.code_submit` when these do not find it. No secret or code is written anywhere, and an
email read by reference is left out of the results. The report says only how it signed in: "as seeded test user …
on localhost …; TOTP from seed: yes".

**When a run meets a sign-in page anyway** (a page that sends signed-out visitors to `/login`, a profile whose
sign-in expired), the scenario ends `harness` with "needs sign-in: ...", not as a product failure, and the report
says which pages asked and what to do (`needs_sign_in` in `report.json`, with a `next_step` an agent can follow). A
scenario that starts on the sign-in page itself is judged as usual. The smoke crawl lists the pages it found behind a
sign-in.

### A seeded test account in QAJev Cloud

A suite (or project env) whose account comes from the app's seed fixture can run in QAJev Cloud, through the full
tunnel to this machine's dev servers (see "Dev servers on this machine, from QAJev Cloud"):

- the suite has `tunnel:` with `data: local`, and every host it opens is this machine's `127.0.0.1` or `localhost`;
- each secret of the account is `seed:FILE#KEY`, from a fixture that says `"test_account": true` and lists its
  `allowed_hosts`. `keychain:`, `op://` and `env:` references, and `--profile` sign-ins, stay on this machine.

QAJev sends one small copy of each fixture, with only the keys this run's account uses (not the fixture's other users),
and a copy of the suite whose `seed:` paths point at it. The file on this machine is not changed. On QAJev Cloud only the
run's own job reads the copy, and it is deleted when the run ends. The cloud machine hides its values in every event,
report file and result it sends. The run writes through the tunnel, because signing in posts a form.

## Games

Games are tested with `qajev play` instead; see [Games](games.md).
## Complete reports

QAJev renders the Markdown, HTML and JSON before publishing a finished report. Each file is replaced atomically;
the final JSON is published after its images and human reports. A render or download failure preserves the last
complete local copy. Progress reports remain marked `partial`; a late progress update cannot replace a finished
report.

A report that names a missing or empty image says `Report incomplete` and lists the image. A would-be PASS becomes
INCOMPLETE. Existing product failures remain failures. Detailed Markdown keeps the complete check reason; compact
summary tables still use short text. Human report times use MYT on every runner.

Cloud report downloads keep the final shot, every multiplayer client's shot, reaction captures, effect images and
claim evidence. A required frame that is missing fails the download with its path and leaves the previous report
intact. A report that already declares missing evidence must have a failing or incomplete gate. Unsafe paths
outside the report folder are never downloaded.
## Measured JavaScript results

A JS check can return an explicit result with its measured values. `ok` must be a boolean, `says` a string,
and `evidence` a JSON object. Ordinary objects still fail; QAJev never grades an object by truthiness.
Promises are awaited, and the expression runs once per page read.

```yaml
expect:
  js: |
    (() => {
      const evidence = {pixels: window.wallPixels, minimum: 8, ratio: window.panelDifference, threshold: 0.3};
      return {
        ok: evidence.pixels >= evidence.minimum && Number.isFinite(evidence.ratio) && evidence.ratio >= evidence.threshold,
        says: 'Wall pixels ' + evidence.pixels + ' (need 8); panel difference ' + evidence.ratio + ' (need 0.3)',
        evidence
      };
    })()
  says:
    js: At least eight wall pixels and a finite panel difference of at least 0.3
```

`report.json` keeps the returned JS value and failure reason, plus the structured `evidence` and
`evidence_type: js`. Known secrets and local filesystem paths are cleaned. These measurements are data:
keys such as `crop` or `reference` never become image links or downloadable artifacts. HTML and Markdown
show the values as escaped text; long returned values can be opened in the HTML report.

Normal diagnostics stay complete. Each reason, returned value and page-error finding keeps up to 64 KiB of
UTF-8 text after privacy cleaning, plus a visible `[truncated N characters]` marker when needed. Whole
characters are retained. An evidence object whose serialized JSON exceeds 64 KiB is replaced by
`{"_qajev_truncated": "[truncated N characters]"}`; smaller objects keep their numbers and JSON types.

Before CDP transfer, the browser also enforces a loose 1 MiB item bound. A larger diagnostic is omitted in
full and replaced by `[truncated N characters before transfer; exceeds 1 MiB]`. No raw prefix is sent, so
an uncleaned secret or path cannot be cut in half. Oversized structured JS results keep their boolean `ok`,
with bounded `says` and evidence plus `_qajev_source_truncated` metadata; this limit does not change the
check's verdict. Ordinary diagnostics still reach Python whole for privacy cleaning and the 64 KiB report cap.

Scenarios also retain page console log, info, warning and error lines under `page_console`. Objects are serialized
so their numbers remain visible. One scenario keeps at most 50 lines of 500 characters, after privacy cleaning;
later warnings and errors can replace the oldest log/info line in a full buffer. Routine lines never evict
warnings or errors. This applies across page reads and navigation as well as within one document.
`truncated: true` marks a shortened line and `page_console_omitted` counts omitted lines. Navigation within a
scenario retains its diagnostics, while the next scenario starts its own console record. These diagnostics
do not add a new pass/fail rule; existing console-error findings keep their verdict behavior.
