# QAJev: prompt for AI agents

Copy everything below the line into your agent's instructions (a `CLAUDE.md`, `AGENTS.md`, a system prompt, or the
first message of a session). It works for any agent that can call MCP tools or run shell commands. Adjust the
**Your project** part at the end.

---

## Testing with QAJev

QAJev tests websites, mobile apps and games the way a person uses them. You describe what a visitor wants in
plain words; **Jev**, a small fast model, clicks through a real Chrome to do it; QAJev then judges the result from
the page itself, not from Jev's opinion. Every run writes a report (`report.html` for people, `report.json` for
you).

Use it to check that a change actually works for a user, before you say it does.

### Which tool, when

| You want to know | Use | Costs |
|---|---|---|
| Is anything obviously broken? (errors, broken links, missing titles) | `qa_smoke` / `qajev smoke URL` | free (no model calls) |
| Can a visitor do *one* thing? | `qa_check` / `qajev check URL --goal ... --expect-text ...` | about $0.001–0.01 |
| Do *several* things work, in order? | `qa_run_suite` / `qajev run suite.yaml` | a few cents |
| Does a known product still meet its stored objectives? | `qa_project_run` / `qajev run --project NAME` | a few cents |
| Does a mobile app or a game work? | `qa_play` / `qajev play ios:BUNDLE_ID`, `android:PACKAGE`, `path/to/game` | a few cents |
| Does each test in a suite say what it proves, and is its test plan approved? (lint it before running) | `qa_plan` / `qajev plan suite.yaml` | free |
| Run again only what did not pass | `qa_rerun` / `qajev rerun JOB --failed` | a few cents |
| What did a run find? | `qa_report`, `qa_screenshot` / `qajev report RUN_DIR` | free |
| What is running right now, and can I stop it? | `qa_jobs`, `qa_job`, `qa_stop` / `qajev jobs`, `qajev stop ID` | free |
| Is QAJev set up correctly? | `qa_doctor` / `qajev doctor` | free |

With MCP, call the `qa_*` tools. Without MCP, run the `qajev` command with `--json` and read the JSON it prints.

### How to write a check

1. **Start free.** Run a smoke crawl first. Fix what it finds before paying for goals.
2. **One intention per check, ending with "Stop when ...".**
   Good: `Find what the Pro plan costs per month. Stop when that price is visible.`
   Bad: `Test the pricing page.` (no clear end, nothing to judge)
3. **Always give expectations.** Jev saying "done" is only a hint; the verdict comes from your checks:
   - `expect_text` / `--expect-text`: words the page must show;
   - `absent_text` / `--absent`: words it must not show (e.g. `Something went wrong`);
   - `expect_url` / `--expect-url`: where the visitor should end up;
   - `expect_js` / `--expect-js`: a JavaScript condition, for anything precise (counts, states);
   - `expect_looks` / `--expect-looks`: a plain statement judged from the final screenshot, for what only shows in
     the picture (a cut-off button, a canvas, an image label). Only with Clef as the decision model
     (`QAJEV_JEV_PROVIDER=cloudflare`); `vision` / `--vision` also shows Clef the screen at every decision.
   Make them specific: a word that also appears elsewhere on the page proves nothing.
4. **Give every value Jev must type**, and make it differ from the field's placeholder.
5. **Use a start `url` close to the target.** Jev does not scroll far on its own.
6. **Say what each test catches**: `fails_when:` on every test, the broken state it catches ("the order is not
   saved"). A claim about motion or the network needs a check over time, not one still moment (`qa_plan` flags it).
7. **Name the approved test plan.** A run follows a plan the Orchestrator approved: a Markdown file in the project
   (`tools/qa/plans/<date>-<name>.md`) whose last line is "Approved by the Orchestrator <date> <time>
   sha256:<qajev plan-hash FILE>". Give it as `plan:` in a suite or `plan` on any run tool (`--plan FILE`). A plan
   edited after its approval is not approved. Without one the run warns; with `QAJEV_REQUIRE_PLAN=1` it is refused.

Every website test also runs in a phone view by default: a scenario's phone copy is named `... (phone)`. A
failure that only shows on the phone is a real mobile bug; report it as such. To also test in a phone's real
browser (Chrome on an Android emulator), pass `real_devices: ["android"]` / `--real-devices android`: it is
slower, so only when the person asks or a bug may be device-specific. Those copies are named `... (Android Chrome)`
and only run read-only scenarios. iOS Safari (`ios`) cannot read page content yet: its copies come out `harness`;
never report that as a site bug.

### How to read the result

Each scenario has one outcome:

| Outcome | Meaning | What you do |
|---|---|---|
| `pass` | every expectation held | nothing |
| `fail` | the product is wrong (a check failed, or the page did not load; a local dev server that is not answering, or a `js` check whose own code throws a TypeError, ReferenceError or SyntaxError, is `harness` instead) | read `reason` and the failed `checks`; fix the product |
| `stuck` | Jev found no way forward | often a real usability problem; look at the screenshot (`qa_screenshot`) before blaming the product |
| `harness` | a problem on QAJev's side (time or action budget, cost cap, a page that never stops changing, Jev saying DONE without taking a single action) | **says nothing about the product**; do not report it as a bug; retry, or narrow the goal |
| `unverified` | no expectations were given | add expectations and run again |
| `skipped` | a step it depends on did not pass, or the machine was too busy | fix the dependency first |

The run's **gate** is `PASS`, `FAIL` or `INCOMPLETE`. **Findings** (S1 worst to S3) are extra problems seen along
the way: script errors, failed requests, HTTP errors, blank screens. Report findings separately from outcomes.

When you tell the person the result, include: the gate, each non-pass outcome with its reason, the findings, the
cost, and the path of `report_html` so they can open the report.

### Long runs

Pass `background: true` (MCP) or `--background` (CLI) to get a job id at once. Follow it with `qa_job(job)` /
`qajev jobs ID` (it shows the scenarios finished so far and what Jev is doing now), and stop it with
`qa_stop(job)` / `qajev stop ID`. Only one browser run executes at a time on a machine; others wait in a queue
and say what they are waiting for. Never start a second copy of a run because the first is queued.

### Rules

- **Never type, ask for or write down passwords, one-time codes or payment details.** QAJev disables those fields
  anyway. If a test needs a signed-in account, ask the person to sign in once:
  `qa_browser(action="login", profile=NAME, url=LOGIN_URL)` / `qajev browser login --profile NAME --url LOGIN_URL`,
  then run with that `profile`. Only on a local dev site (localhost, 127.0.0.1, *.test) can a seeded test account
  sign in by itself: an `account:` block with `password: seed:FILE#KEY` (and, for a second factor, `totp:` or a
  `cookie:` value from `seed:` too), from the app's fixture that says `"test_account": true` and lists the host in
  `allowed_hosts`. A `keychain:`, `op://` or `env:` password is refused.
- **A result with `needs_sign_in` means runs met a sign-in page**, not that the product failed (those scenarios are
  `harness`). Ask the person how QAJev should get in, as its `next_step` says: they sign in once (above); on a
  local dev site a seeded test account (`password: seed:FILE#KEY`) can sign in by itself. Never ask for a password.
  Then run again.
- **Production is read-only. Destructive is never.** Only a local dev host may change: `localhost`,
  `127.0.0.1`, `[::1]`, `*.localhost`, `*.test`. Every other host is production, staging and previews too. A run
  that would change it (`mode: mutate`, `--allow-destructive`) is refused before it starts: exit code 5, outcome
  `refused`. Do not work around it (another host name, a tunnel, a proxy): tell the person, and run read-only or
  against a local copy.
- **Dangerous and destructive buttons are hidden from Jev** (sign out, pay, billing, "close all"; delete, remove,
  refund, cancel, archive, reset...). Do not try to work around that. Only `--allow-destructive` shows the
  destructive ones, on a local dev host.
- **Respect other people's sites.** Crawl public sites politely (the smoke crawl already reads robots.txt and waits
  a second between pages). Do not load-test with QAJev.
- **Mind the cost cap.** Every run has one (default $1). Keep it low for exploratory runs (`cost_cap` /
  `--cost-cap 0.10`).
- **A `harness` outcome is not a product bug.** Say so plainly if you report it.
- **A game's QUIT, exit and delete-save buttons are hidden from Jev.** To test a normal quit on purpose, pass
  `allow: ["QUIT"]` and `expect_closed: true` (`--allow QUIT --expect-closed`).

### Your project

<!-- Edit this part for your project. -->
- The site under test: `http://localhost:3000` (local, changes allowed) and `https://example.com` (production,
  read-only).
- Stored objectives: `qajev run --project my-project --suite core` (see `.qajev/project.toml`).
- After changing anything a user can see, run the core objectives and include the report path in your summary.
