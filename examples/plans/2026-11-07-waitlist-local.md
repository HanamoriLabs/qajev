# Plan: a visitor can join the waitlist once, on the local dev server (7 Nov 2026)

## Why

PR #87 adds a waitlist to the shop, and its reviewer asked for proof before the merge: "Show that a visitor can join,
and that the same email cannot join twice." The run answers that, on the PR's head, before the merge.

## Target

The shop's dev server on this computer: `npm run dev` in the repository, at http://localhost:3000, on its seed data
(`npm run seed`, an empty waitlist). It is a local dev host, so the runs may change its data. Nothing else.

Not covered: the confirmation email (the dev server sends none), and the phone layout.

## When

The runs start only when all of these hold. I check each one right before the run:
- `curl -sI http://localhost:3000/waitlist` gives 200, and the server runs the PR's head commit (`git rev-parse HEAD`).
- `npm run seed` ran just before, so the waitlist is empty.
- `qajev plan examples/plans/waitlist.yaml` exits 0: every test says what it proves, with no ABSENT ONLY and no
  PRESSES KEYS.
- The approver's line is the last line of this file.

## The runs

1. `qajev run examples/plans/waitlist.yaml --json` (the suite names this plan with `plan:`).

One run. Its 4 tests run in order, on desktop only (`device: desktop`): they share one email, so a phone copy of a
join would find it taken.

## What each test proves

| Test | Claim: a pass means | Fault it catches |
|---|---|---|
| the waitlist form opens | the home page leads to the form | a missing or broken link |
| join the waitlist | a visitor can join, and the page confirms the right email | a form that does not send, or saves another email |
| a taken email is refused | the same email cannot join twice, and the visitor is told why | a duplicate entry, or a silent refusal |
| planted - a taken email is let through | the "no second confirmation" check really fails when a duplicate gets in | a check that passes whatever happens |

The planted test uses the dev server's `?plant=dup-email` switch (PR #87 adds it, on the dev server only). It must
fail on "no second confirmation shows", and on that check only.

## Expected result

Gate PASS: 3 tests pass, and the planted test fails as expected (graded pass). A FAIL on a real test is a bug in
PR #87 that I report to its author. A FAIL on the planted test means the check proves nothing: I fix the check, and
the plan needs a new approval.

## Cost and load

Jev with a cap of $0.60 for the whole run (`cost_cap_usd`). This computer only, one browser, about 3 minutes.

## Data and accounts

The runs add waitlist entries to the local dev data only. I run `npm run seed` after the run. No account, no
password: the waitlist needs neither.

## The stop rule

Stop, change nothing, and tell the approver at once if:
- the run gives HARNESS or INCOMPLETE twice;
- the run reaches any host but localhost:3000 (QAJev's report lists each request);
- the dev server is not on the PR's head commit.

A stopped run is not run again without a new approval line.

## The result

One comment on PR #87 and one message to the approver: the gate, the report_html path, and each failed check with
its reason.
