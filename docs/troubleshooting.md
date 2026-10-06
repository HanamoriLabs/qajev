# Troubleshooting

Start with `qajev doctor`: it checks keys, Chrome, ports and load, and says what to fix.

**"Chrome not found"**
Set `QAJEV_CHROME` to your Chrome or Chromium binary, e.g.
`export QAJEV_CHROME=/usr/bin/chromium`.

**"goals need TYPESAFE_API_KEY or an OpenRouter key ... or QAJEV_JEV_PROVIDER=cloudflare ..."**
Add one key to `~/.qajev/.env` ([Getting started](getting-started.md)). `qajev smoke` needs no key. Cloudflare
credentials alone are not used: Clef needs `QAJEV_JEV_PROVIDER=cloudflare` too.

**A key in the file is ignored**
A variable set in your shell wins over the file. `qajev doctor` says when that happens; `unset` the old one.

**The run says "queued behind ..."**
Another QAJev run has the browser; yours starts when it ends. See who with `qajev jobs` or `qajev top`.

**Many scenarios come out `harness`**
That is QAJev's side, not your product:

- *decisions went stale*: the page kept changing (a live counter, a video, an animation). The reason ends with the
  last stale move's own: what was over its target ("covered by div#toast") or what changed ("text: 'Score 41' →
  'Score 42'"); each stale decision says it in the dashboard and in `qajev top --decisions`. Keep `--motion reduce`
  (the default) and start the scenario on a calmer page, or close what covers the target with a `before:` hook.
- *action or time budget spent*: split the goal, or start closer to the target with `url`.
- *Jev never left the start page*: the checks need another page, and Jev only scrolled where it began and clicked
  nothing (often a phone menu it never opened). Say the way there in the goal ("open the menu, then the pricing
  page"). Jev clicking around and not arriving is still a failure: the way may really be missing.
- *cost cap reached*: raise `--cost-cap`, or narrow the goals.

**"sign-in failed: ..." and every scenario is `harness`**
The stored test account could not sign in, so nothing was tested. The reason is the site's own message ("Wrong
email or password") or the password store's: run `qajev secret check REF` to see whether QAJev can read it (a
1Password reference needs `op` signed in, and may wait for Touch ID; `env:` needs the variable set where QAJev runs).
If the site's fields are unusual, name them in the account's `login:` (`email_field`, `password_field`, `next`,
`submit`) and say how to tell it worked (`signed_in`).

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

**The MCP tools do not show up in my agent**
Check that `qajev mcp` runs from the agent's environment (use the full path from `which qajev`), then restart the
agent's session.

**A game will not start**
For Godot, set `QAJEV_GODOT` to the Godot binary if it is not on your PATH. For an Electron project folder, install
its dependencies (so `node_modules/.bin/electron` exists), or set `QAJEV_ELECTRON`.

Still stuck? [Open an issue](https://github.com/hanamorilabs/qajev/issues) with the output of `qajev doctor` and,
if you can, the run's `report.md` (check it for anything private first).
