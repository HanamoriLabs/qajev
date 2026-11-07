# Plan: <what this run proves, in one line> (<D Mon YYYY>)

<!--
Copy this file to tools/qa/plans/<YYYY-MM-DD>-<name>.md in your project, fill in every section, delete the hints,
and send the path to your approver. Never add the approval line yourself. Guide: docs/test-plans.md.
-->

## Why

<Who asked for this run, and their words. What changed, or what question the run answers.>

## Target

<The exact address the run opens, and what it is: a local dev server (http://localhost:3000, with how it starts and
its seed data), a staging site, or a public production site (read-only).>
<What is not covered, and why.>

## When

The runs start only when all of these hold. I check each one right before each run:
- <The target answers, for example `curl -sI http://localhost:3000/` gives 200.>
- <No other run of this plan is going.>
- The approver's line is the last line of this file.

## The runs

<Each run as the exact command or MCP call, with its suite and this plan. One per line, in order.>

1. `qajev run tools/qa/<name>.yaml --json --plan tools/qa/plans/<this file>`

<One at a time, or what may run side by side.>

## What each test proves

| Test | Claim: a pass means | Fault it catches |
|---|---|---|
| <test name, as in the suite> | <what a person can do, in plain words> | <what breaks it: the bug this test would catch> |

<For each new check: the planted fault (expect_fail) that proves it can fail, or why there is none.>

## Expected result

<The gate you expect, and why. What a FAIL would mean: a real finding to report, not a reason to rerun.>

## Cost and load

<Model: none (smoke), Jev or Clef. The cost cap per run (cost_cap_usd or --cost-cap) and in total. The runner:
this computer or QAJev Cloud, and its machine minutes.>

## Data and accounts

<What the runs may change: nothing (read-only), or only the local dev data, reseeded after. Any seeded test account,
by its fixture reference only (seed:FILE#KEY). Never a real password.>

## The stop rule

Stop, change nothing, and tell the approver at once if:
- a run gives HARNESS or INCOMPLETE twice;
- a run touches any host but the target;
- <anything else that would make the result mean something else>.

A stopped run is not run again without a new approval line.

## The result

<Who gets the result, and what it holds: each run's id, gate, its report_html or report_url, and the findings.>
