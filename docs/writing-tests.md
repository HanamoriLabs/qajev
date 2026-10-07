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
| `looks` | `--expect-looks` | Clef, looking at the final screenshot, judges this statement true (needs Clef: see below) |

Text is compared the way a person reads it: line breaks and runs of spaces count as one space. Use
`ignore_case: true` for labels styled in capitals.

## What only a look at the screen tells (Clef)

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
      looks: ["The 'Sign up now' button is fully visible and its words are readable, not cut off"]  # ...and legible
  - name: pricing from a drawn menu
    url: /plans
    vision: true                        # the menu buttons are pictures
    goal: Open the pricing page. Stop when the page heading says Pricing.
    expect: {url: /pricing}
```

Write a `looks` statement the way you would brief a person looking at a screenshot: one plain fact, and specific
(`The Pro plan costs $29 per month`, not `the prices look right`). A looks check is one Clef call per run of the
scenario (all its statements together), on top of its decisions. `vision` goes on a scenario or on the whole suite,
and makes every decision larger: a screenshot is about 1,000 extra input tokens, about $0.0001 with Clef-flash and
$0.00024 with Clef. Both need Clef: with Jev, which reads text only, QAJev refuses the run and says what to set.

## Desktop and phone

Every website test runs **twice by default: on desktop and on a phone** (a 390×844 screen with touch and an
iPhone user agent, like a browser's device mode). The phone copy of a scenario is named `... (phone)` in the report,
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
cost_cap_usd: 0.50        # for the whole run; checked before every paid call
settle: 10                # seconds to wait for expectations after Jev stops
motion: reduce            # reduce (default): pages are told the visitor prefers less motion; full: as-is
cpu_throttle: 1           # run the page's CPU this many times slower (1 to 8), e.g. 4 for a phone-like phone pass

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
2 s for a splash or a settling panel; a target still covered fails naming what covers it), `fill`
(`{selector: text}`), `navigate`, `wait_for` (JavaScript that must turn
true, within `timeout` seconds: 15 by default, at most 300, e.g. `{js: "window.ready", timeout: 60}`; a Promise counts by what it resolves to; a wait, unlike a check, holds on any truthy value), `key` (a real key press, below), `sleep` (up to 30 s), `reload` (`reload: true`: the same page again, keeping its
cookies and storage), and `command` (a shell command, only with `--allow-commands`). Hooks refuse to click dangerous controls or fill password fields on a real site.
A scenario's checks judge the page as it is after its `before:` hooks, so a `wait_for` there is what they see.
Its `after:` hooks run after the checks: a result an `after:` hook writes can never change the verdict, so a
hook that plays the page and records a result belongs last in `before:`. `expect` js and hook js run in separate
JavaScript worlds: they share the page's DOM (so a hook can leave its result in a `data-` attribute), not its
window globals.

A `key` hook presses real keys, as a player does: the page gets trusted `keydown` and `keyup` events. Jev clicks and
types but cannot press a game's keys, so a game's real-key test drives them with hooks. A chord adds Shift, Ctrl, Alt
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
report. A suite can tune it:

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
from the fixture and types it into the password field, submits, and checks it got in. Then the scenarios run as
usual, guarded and already signed in. The password is never in Jev's prompt, a report, a screenshot or a log (it is
redacted from everything QAJev writes), and Jev still cannot type into password fields. If the site does not let the
account in, the run stops with the reason (`harness: sign-in failed: Wrong email or password`) instead of leaving Jev
at a sign-in page.

The sign-in page must be `https` (plain `http` only on localhost), on a local dev host the fixture's `allowed_hosts`
names, checked again where the browser is just before it types; the password only goes into a password field.
`login` also takes `email_field`, `password_field` and `submit` (CSS selectors, when the defaults do not find them),
`next` (the button between the email and password steps of a two-step sign-in), and `signed_in` to say how success
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
  email: seed:dev_support/qa_test_user.json#email          # FILE#KEY, relative to this suite (or the project's repo)
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

## Games

Games are tested with `qajev play` instead; see [Games](games.md).
