# Safety

QAJev lets a model click around real websites, so safety is built in and on by default. None of this depends on
the model behaving well: the rules are enforced in the page and in QAJev itself.

## What QAJev never does

- **Let Jev type passwords, one-time codes or payment details.** Those fields are disabled in the page. Hooks
  refuse to fill them on any site that is not your own machine.
- **Sign in on its own initiative, or type a person's password.** Either the person signs in once in QAJev's own
  browser profile (`qajev browser login --url <sign-in page>`), and the profile keeps that session; or a suite names
  a seeded **test** account on a local dev host (`localhost`, `127.0.0.1`, `*.test`, `*.localhost`) whose password
  comes only from the app's seed fixture (`password: seed:FILE#KEY`, a JSON fixture that says
  `"test_account": true` and lists the host in `allowed_hosts`). Then QAJev itself, not Jev, signs in before the
  scenarios: only on that host, checked again where the browser is when it types, only into a password field, with
  the value redacted from everything it writes. A password from the Keychain, 1Password or an environment variable
  is refused, on every host ([Signed-in areas](writing-tests.md#signed-in-areas)). So is a URL that holds a user name
  or password (`https://alice:s3cret@shop.example/`, with or without the scheme), in a check, a smoke, a suite or a project, before Chrome starts
  and before anything is written or sent to QAJev Cloud: the URL would reach the report files.
- **Change a production site.** Production is read-only. Destructive is never. Only a **local dev host** may
  change: `localhost`, `127.0.0.1`, `[::1]`, `*.localhost` and `*.test` (names that never reach the internet).
  Every other host is production, staging and previews included, and there is no override:
  - A run that would change it (`mode: mutate`, or `--allow-destructive`) is **refused before Chrome starts**, from
    every entry point (`check`, `run`, a project, `nightly`, `rerun`, the MCP tools): "QAJev never changes a
    production site: HOST is not a local dev host.", exit code 5, outcome `refused`.
  - In the page, the guard decides per page and per request, so a local run that reaches a production page is
    read-only there. Buttons named like writes (save, submit, invite...) are hidden. `DELETE`, `PUT` and `PATCH`
    never leave the page; a `POST` only when the suite's `guard.allow_requests` names it, and every one let
    through is listed in the report. Other writes are blocked and listed.
  - The page's `confirm()` gets "no", `prompt()` gets nothing, and a "leave this page?" (`beforeunload`) never
    holds the tab; each is recorded in the report. Delete and Backspace reach the page only inside a text field.
- **Show destructive controls.** Delete, remove, erase, destroy, drop, purge, wipe, cancel, refund, void,
  revoke, deactivate, unsubscribe, archive, reset, empty trash, close or terminate account, uninstall and
  disconnect are hidden from Jev on **every** host, in every mode, local included. A reset, clear or cancel of
  something harmless stays (filters, a search, a selection, the form being edited), and so does drag-and-drop
  wording ("drag to move", "drag and drop", "drop a file here"), unless the control also names an account, order,
  subscription, plan, booking, data, payment or membership. "Drop table" or "Drop all" stays hidden. A to-do row's
  own title is shown too: in a list item with a checkbox, the one control whose every name (its text, aria-label and
  title) equals the checkbox's label (a task "Buy paint" beside "Toggle Buy paint"), unless it reads as a purchase
  ("Buy 500 credits $10", "Checkout", a price). A control named by aria-labelledby, or holding a child whose alt,
  aria-label or title carries a danger, destructive or purchase word, is never such a title. On production a title
  with a destructive word stays hidden. The row's
  other controls, such as its Delete, follow the usual rules. Escape always reaches the page, so a
  dialog whose only way out is a bare "Cancel" still closes. `guard.allow` never shows them.
  Only `--allow-destructive` (suite `allow_destructive: true`, project env `allow_destructive = true`, MCP
  `allow_destructive`) does, on a local dev host only, and the report then carries a red banner.
- **Press dangerous buttons.** Sign out, close all, delete account or data, revoke keys, billing, upgrade or
  downgrade, pay, buy, checkout, subscribe, start a trial, leave or transfer, "danger zone" and more are hidden
  from Jev before it sees the page. Links to other sites (store badges, social links) stay on the page as
  visitors see them, in screenshots and for your checks, but they are inert: Jev is not offered them and a click
  on them does nothing.
- **Download files.** Every download is refused; the smoke crawl checks download links with a lightweight `HEAD`
  request instead of fetching the file.
- **Listen.** The microphone and camera are denied; speech recognition is replaced by a fake that hears nothing
  (or the text you give it).
- **Use your own browser.** QAJev starts its own Chrome with its own profile, never your everyday one, and
  closes it after the run.
- **Spend past the cap.** Every run has a hard cost cap (default $1), checked before every paid call.

## How it is enforced

1. The new tab opens on a blank page. The **guard** is installed for every page **before** the site's first byte
   loads. Writing requests are blocked from that moment. The guard marks the page's own controls (disables,
   hides) once the page has loaded, and on a page React hydrates (Next.js and the like), once React has taken each
   one over, at most 5 s later: marking them sooner makes React report "attributes didn't match", which would
   read as the site's bug. A warning like that about only the guard's own attributes is a harness note, not a
   finding. The guard judges a control by every name it has, as the browser builds it: its text, aria-label and
   title, the text its `aria-labelledby` points to, its children's alt, aria-label, title and `aria-labelledby`, and
   an inline svg's title and description. It judges the control again when any of them changes, elsewhere on the
   page too.
2. Before **every** action Jev takes, and every control a hook clicks, QAJev proves the guard is still active on
   the live page and has judged every control on it. If it is missing, or still waiting after 12 s, the scenario
   stops (a harness outcome).
3. Jev may only stay on the site's own hosts (plus any you list); leaving them stops the scenario.
4. Secrets from your environment are removed from reports, and addresses in Markdown reports lose their query
   strings (which often carry tokens).

## Being a good citizen

- On public sites the smoke crawl reads `robots.txt`, skips what it disallows, and loads at most one page per
  second (or slower if the site asks).
- QAJev's Chrome says it is QAJev: every page it drives sends the browser's own user agent with
  `QAJev/<version>` on the end (on a phone too), and keeps the browser's own client hints. A site, or a page
  that counts its visitors, can tell a QAJev visit from a person's. The smoke crawl's own requests say `QAJev`.
- QAJev is not a load-testing tool. Only test sites you own or have permission to test.

## Games and apps

QAJev starts its own copy of the game with a throwaway save folder (or, for an Electron game with
`--game-profile`, a kept test folder under `~/.qajev/game-profiles/`, never a real save), sends input into the game only, and never
offers Jev quit, purchase or delete-save actions. A game or an app has no in-page write guard, so `qajev play` is
read-only: a play suite that asks for `mode: mutate` is refused (exit 5). See [Games](games.md).

## Reporting a security problem

See [SECURITY.md](../SECURITY.md).
