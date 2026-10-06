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
  is refused, on every host ([Signed-in areas](writing-tests.md#signed-in-areas)).
- **Change data on a real site.** Tests are read-only by default: buttons named like writes (save, submit,
  delete, invite...) are hidden, and form posts and other writing requests are blocked in the page and listed in
  the report. `mode: mutate` is allowed **only** on `localhost` / `127.0.0.1`, with no override.
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
   finding.
2. Before **every** action Jev takes, and every control a hook clicks, QAJev proves the guard is still active on
   the live page and has judged every control on it. If it is missing, or still waiting after 12 s, the scenario
   stops (a harness outcome).
3. Jev may only stay on the site's own hosts (plus any you list); leaving them stops the scenario.
4. Secrets from your environment are removed from reports, and addresses in Markdown reports lose their query
   strings (which often carry tokens).

## Being a good citizen

- On public sites the smoke crawl reads `robots.txt`, skips what it disallows, and loads at most one page per
  second (or slower if the site asks).
- QAJev is not a load-testing tool. Only test sites you own or have permission to test.

## Games

QAJev starts its own copy of the game with a throwaway save folder, sends input into the game only, and never
offers Jev quit, purchase or delete-save actions. See [Games](games.md).

## Reporting a security problem

See [SECURITY.md](../SECURITY.md).
