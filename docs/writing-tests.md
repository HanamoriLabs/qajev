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
| `visible` | `--visible` | these words are on screen right now, not just somewhere below |
| `absent` | `--absent`, `-a` | the page does not show these words |
| `url` | `--expect-url`, `-u` | the final address contains this |
| `url_regex` | `--expect-url-regex` | the final address matches this pattern |
| `js` | `--expect-js`, `-j` | this JavaScript expression is true in the page (a Promise counts by what it resolves to, within 5 s; a throw, a rejection or no answer fails the check) |
| `fetch` | `--fetch URL[=STATUS]` | a request made from the page answers with that status (default 200) |
| `command` | suite only | a shell command prints the expected output (needs `--allow-commands`) |

Text is compared the way a person reads it: line breaks and runs of spaces count as one space. Use
`ignore_case: true` for labels styled in capitals.

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

## A suite: several scenarios in one file

Start from a template:

```bash
qajev init qajev.yaml     # writes a starter suite
qajev run qajev.yaml
```

A suite in full:

```yaml
name: Shop
base_url: http://localhost:3000
mode: readonly            # readonly (default) or mutate (only on localhost / 127.0.0.1)
device: desktop           # desktop | tall | phone | tablet | WIDTHxHEIGHT
persona: "You are a first-time visitor."
budget: {actions: 20, seconds: 90}   # per scenario (at most 60 actions)
cost_cap_usd: 0.50        # for the whole run; checked before every paid call
settle: 10                # seconds to wait for expectations after Jev stops
motion: reduce            # reduce (default): pages are told the visitor prefers less motion; full: as-is

scenarios:
  - name: the home page loads
    url: /
    expect:
      text: ["Welcome"]
      absent: ["Something went wrong"]

  - name: a visitor finds the Pro price
    url: /
    goal: Find out how much the Pro plan costs. Stop when its monthly price is visible.
    expect:
      url: /pricing
      visible: ["$29 per month"]

  - name: pricing on a phone
    url: /
    device: phone
    goal: Open the pricing page. Stop when the plan prices are visible.
    expect:
      js: document.querySelectorAll('[data-plan]').length === 2

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

Hooks: `js`, `click` (a CSS selector), `fill` (`{selector: text}`), `navigate`, `wait_for` (JavaScript that must turn
true, up to 15 s; a Promise counts by what it resolves to), `key` (`Escape`, `Enter`, `Tab`), `sleep` (up to 30 s), and `command` (a shell command, only with
`--allow-commands`). Hooks refuse to click dangerous controls or fill password fields on a real site.

## Guard options

The guard hides dangerous controls and blocks writes (see [Safety](safety.md)). A suite can tune it:

```yaml
hosts: [accounts.example.com]      # other sites Jev may visit; anything else stops the scenario
guard:
  deny: ["\\bexport all\\b"]       # more button labels to hide (regular expressions, any case)
  allow: ["^Delete draft$"]        # exceptions to the built-in hidden list
  allow_requests: ["/graphql$"]    # read-only mode: let these non-GET requests through
  block_urls: ["*://*/logout*"]    # never load these addresses
  redact_emails: false             # hide e-mail addresses from Jev
```

## Signed-in areas

Two ways in. Jev never types a password in either.

**A stored test account.** The suite names a test account; its password stays in your password store and the file
only says where:

```yaml
account:
  email: qa+shop@example.com          # or a reference, like the password
  password: keychain:qajev/shop-tester # or op://QA/Shop tester/password (1Password), or env:SHOP_TESTER_PASS
  login: {url: /login}
scenarios:
  - name: the order history lists past orders
    url: /account/orders
    expect: {text: ["Order #"]}
```

Before the first scenario, QAJev itself opens the sign-in page in its own tab, types the email, reads the password
from the store and types it into the password field, submits, and checks it got in. Then the scenarios run as usual,
guarded and already signed in. The password is never in Jev's prompt, a report, a screenshot or a log (whatever is
read from a store is redacted from everything QAJev writes), and Jev still cannot type into password fields. If the
site does not let the account in, the run stops with the reason (`harness: sign-in failed: Wrong email or password`)
instead of leaving Jev at a sign-in page.

| `password:` | Where the password lives | Set it up |
|---|---|---|
| `keychain:SERVICE/ACCOUNT` | the macOS Keychain (on Linux, the secret service via `secret-tool`) | `qajev secret set keychain:qajev/shop-tester` (the Keychain prompts for it) |
| `op://VAULT/ITEM/FIELD` | 1Password, read with its `op` command-line tool (Touch ID) | make the item in 1Password; install `op` and turn on its app integration |
| `env:NAME` | an environment variable, e.g. a CI secret | set it in CI or your `.env` |

`qajev secret check REF` says whether QAJev can read it (the length, never the value). Use a test account made
for this, never a real person's.

The quickest way is one command, which saves the password at the Keychain's prompt, writes the account (into a
project with `--project`, or prints the block above for a suite) and signs in once to prove it
([`qajev account`](cli.md#qajev-account)):

```bash
qajev account add shop-tester --email qa+shop@example.com --login-url https://shop.example/login
```

The sign-in page must be `https` (plain `http` only on localhost) and its host one the suite allows; the password
only goes into a password field. `login` also takes `email_field`, `password_field` and `submit` (CSS selectors,
when the defaults do not find them), `next` (the button between the email and password steps of a two-step
sign-in), and `signed_in` to say how success looks (`{url_not: /login}`, `{text: ["Your orders"]}` or `{js: ...}`;
by default: away from the sign-in page with no password field showing).

**Or sign in once yourself** in QAJev's own browser profile; the profile remembers it:

```bash
qajev browser login --profile shop --url https://shop.example/login   # a window opens: sign in, then close it
qajev run account.yaml --profile shop
```

This is the way for sign-ins QAJev cannot do by itself: one-time codes, passkeys, "Sign in with Google".

**When a run meets a sign-in page anyway** (a page that sends signed-out visitors to `/login`, a profile whose
sign-in expired), the scenario ends `harness` with "needs sign-in: ...", not as a product failure, and the report
says which pages asked and what to do (`needs_sign_in` in `report.json`, with a `next_step` an agent can follow). A
scenario that starts on the sign-in page itself is judged as usual. The smoke crawl lists the pages it found behind a
sign-in.

## Games

Games are tested with `qajev play` instead; see [Games](games.md).
