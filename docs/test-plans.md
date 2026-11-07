# Test plans: what an agent writes before a QAJev run

A test plan says, before anything runs, what a run will prove, how, and when it stops. A person or a lead agent
approves it. The run then names the plan, and its report shows the approval. So a PASS means what the plan said it
would mean, and nobody spends model money or cloud credits on a run they did not agree to.

This page is for agents. It covers the steps, the sections of a plan, what an approver checks, the approval line, and
the mistakes that make a plan not approved. Copy [the template](../examples/plans/TEMPLATE.md) to start. Three full
examples follow at the end.

## Two things called "test plan"

- **The suite's test plan.** QAJev builds it from your suite: each test's `about` and each check in plain words. It
  opens every report, and `qajev plan suite.yaml` (MCP `qa_plan`) prints it without running anything. See
  [Writing tests](writing-tests.md#writing-good-goals).
- **The approved test plan.** It is a Markdown file that you write, and that your approver approves. It says why the
  run exists, the exact runs, what each test proves, the fault each one catches, what you expect, the cost, and the
  stop rule. This page is about this one.

They work together: the plan file names the suite, and the suite names the plan file (`plan:`).

## The steps

1. **Write the suite first, and lint it.** Every test has an `about`, and every `js`, `url_regex`, `fetch` or
   `command` check has `says`. Run `qajev plan suite.yaml` (MCP `qa_plan`) until it says every test says what it
   proves, with no ABSENT ONLY and no PRESSES KEYS. For a one-off `qajev check`, give `--about` and `--says`.
2. **Copy the template** to `tools/qa/plans/<YYYY-MM-DD>-<name>.md` in the project you test. Keep it inside that
   project: a suite finds its plan only from its own folder up to the project root.
3. **Fill in every section** (below), and delete the template's hints. Write the exact commands, not "run the
   suite".
4. **Name the plan in the run.** Put `plan: <path>` in the suite, or give `--plan <file>` (MCP: `plan`) on
   `qajev run`, `check`, `smoke` and `play`.
5. **Send the plan's path to your approver**, and wait. Never write the approval line yourself, not even to record an
   approval someone gave you in a chat. Run nothing before the line is there.
6. **Check it after the approval:** `qajev plan suite.yaml` prints `Plan file: ..., approved: ...`. For a run without
   a suite: `qajev plan-hash <file>` prints `approved: <the line>` on stderr.
7. **Run exactly what the plan says**, in its order, with its "When" checks before each run.
8. **Report as the plan's "The result" section says**: each run's id, gate, report and findings.
9. **Change anything, and the plan needs a new approval.** That covers another target, another command, a new test,
   a rerun after a stop, or an edit to the text.

## What goes in a plan

| Section | What it holds |
|---|---|
| Title | What the run proves, in one line, and the date |
| Why | Who asked, in their words, and what changed |
| Target | The exact address, and what kind it is: a local dev server (and how it starts), staging, or production (read-only). What is not covered |
| When | The checks you make right before each run: the target answers, it runs the right commit, nothing else runs, the approval line is there |
| The runs | Each run as the exact command or MCP call, in order, each naming the plan |
| What each test proves | A table: the test, the claim (what a pass means), and the fault it catches. For a new check, its planted fault, or why there is none |
| Expected result | The gate you expect. What a FAIL would mean: a finding to report, not a reason to rerun |
| Cost and load | The model (none for a smoke), the cost cap, the runner (this computer or QAJev Cloud), and the time |
| Data and accounts | What the runs may change. A seeded test account by its reference only (`seed:FILE#KEY`), on a local dev host only |
| The stop rule | When you stop, change nothing and tell the approver |
| The result | Who gets the result, and what it holds |

## What an approver checks

Write the plan so that each of these is easy to see. An approver refuses a plan that misses one.

- Each run is an exact command. Its target is named, and the target is the one the "Why" asks about.
- Production is read-only. Changes (`mode: mutate`) happen on a local dev host only: `localhost`, `127.0.0.1`,
  `[::1]`, `*.localhost` or `*.test`.
- No password, code or card number is in the plan or the suite. A sign-in uses the person's own sign-in
  (`qa_browser(action="login")`) or a seeded test account reference on a local dev host.
- Each test has a claim and a fault it catches. A test that cannot fail proves nothing. A new check comes with a
  planted fault (`expect_fail`) where one can be made.
- Each run has a cost cap, and the runner is named (QAJev Cloud: `--runner cloud`).
- The stop rule says what makes you stop. A stopped run is not run again without a new approval.
- The result goes to someone, with the run ids and the reports.

## The approval line

The approver runs `qajev plan-hash <file>` and adds one line at the very end of the plan:

```text
Approved by the Orchestrator 7 Nov 2026 18:14 sha256:<the 64 hex digits that plan-hash printed>
```

- The sha256 is of the text above the line (line ends as `\n`, with trailing blank lines and spaces dropped). So any
  edit above the line makes the plan **not approved**: "the plan changed after its approval".
- The last approval line counts, and it covers everything above it. To approve an addition, the approver adds a new
  line at the end. `qajev plan-hash` on a file with text after its last approval prints the sha256 for that new line.
- Text after the last approval line is not approved, and it makes the whole plan not approved.
- The line must start with `Approved by the Orchestrator`. A team with another approver sets `QAJEV_PLAN_APPROVER`
  (for example `QAJEV_PLAN_APPROVER="the QA lead"`), and the line then starts `Approved by the QA lead`.

## What a run does with the plan

- The report and the dashboard show the plan's path and its approval line. Without an approved plan they show a
  banner, "No approved test plan".
- A run without an approved plan prints `qajev: warning: no approved test plan: ...` and runs. With
  `QAJEV_REQUIRE_PLAN=1`, it is refused before the queue and before Chrome (exit 3, "needs an approved test plan").
  `qajev doctor` says which rule is on.
- A QAJev Cloud run (`--runner cloud`) takes the plan file with it, and the cloud machine checks it again
  ([CLI](cli.md#qajev-cloud-where-a-run-goes)).

## Mistakes that make a plan not approved

- An edit after the approval, even one word or a typo fix. Ask again.
- An approval line written by the agent that runs the plan.
- The plan sits outside the project. A suite's `plan:` is looked up from the suite's folder up to the project root
  (the folder with `.qajev/` or `.git`), never above it.
- Notes added under the approval line. Put the result in a message or a report, not in the plan.
- One plan reused for a new run with another purpose. Write a new plan: it is cheap, and it says what this run is
  for.

## Examples

Each one is a complete plan, ready to copy. None has an approval line: a copied example is never approved by
itself.

- [A first-time visitor can find a price, in QAJev Cloud](../examples/plans/2026-11-07-pricing-cloud-check.md): a
  read-only smoke and one `qajev check` of a public production site.
- [A waitlist on a local dev server](../examples/plans/2026-11-07-waitlist-local.md), with its suite
  [waitlist.yaml](../examples/plans/waitlist.yaml): changes on a local dev host, a refused duplicate, and a planted
  fault that proves the duplicate check can fail.
- [A game's first level](../examples/plans/2026-11-07-game-first-level.md): a Godot build played from the title
  screen to the end of level 1, with a frame rate floor.
- [The template](../examples/plans/TEMPLATE.md): every section, with hints.

The examples' sites, PRs and builds are made up. Change them to your own.
