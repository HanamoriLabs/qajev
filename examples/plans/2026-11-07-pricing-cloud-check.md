# Plan: a first-time visitor can find the Pro price on shop.example.com (7 Nov 2026)

## Why

Sam (product), 7 Nov 14:10: "The new pricing page is live. Can a new visitor find the Pro price without signing in?"
The pricing page moved from /pricing to /plans today (PR #212), and the old link in the footer now redirects.

## Target

https://shop.example.com, the public production site. Read-only: no sign-in, no form, no POST. QAJev keeps
production read-only by itself, and these runs ask for nothing more.

Not covered: the signed-in checkout, and the price in other currencies.

## When

The runs start only when all of these hold. I check each one right before each run:
- `curl -sI https://shop.example.com/plans` gives 200.
- No other run of this plan is going (`qajev cloud-runs --hours 1`).
- The approver's line is the last line of this file.

## The runs

In QAJev Cloud, one after the other. Each names this plan:

1. `qajev smoke https://shop.example.com/ --max-pages 10 --runner cloud --json --plan tools/qa/plans/2026-11-07-pricing-cloud-check.md`
2. `qajev check https://shop.example.com/ --goal "Find out how much the Pro plan costs per month. Stop when its monthly price is visible." --expect-url /plans --expect-text "Pro" --expect-js "document.querySelector('[data-plan=pro] .price')?.textContent.trim() === '£29'" --says "the Pro plan shows £29 per month" --about "a first-time visitor can find the Pro price without signing in" --runner cloud --cost-cap 0.25 --json --plan tools/qa/plans/2026-11-07-pricing-cloud-check.md`

Run 2 starts only after run 1 has ended.

## What each test proves

| Test | Claim: a pass means | Fault it catches |
|---|---|---|
| smoke (run 1) | up to 10 pages load with no HTTP error, no script error, no broken link or image | the old /pricing link that 404s, a broken redirect, a missing image |
| Pro price (run 2) | a first-time visitor finds the Pro price from the home page, on /plans, and it is £29 a month | a hidden or renamed link, the price missing or wrong, a page that needs a sign-in |

No planted fault: the price check is a plain text match on the page that Sam named. A wrong price fails it.

## Expected result

Run 1: PASS, or FAIL with findings (each finding is a real site problem, which I report). Run 2: PASS. A FAIL with
the price check failed means the page shows another price: a real finding for Sam, not a reason to rerun.

## Cost and load

Run 1 makes no model calls. Run 2 uses Jev, with a cap of $0.25. Both use the team's QAJev Cloud credits: about 5
machine minutes in all.

## Data and accounts

Nothing changes: production is read-only. No account, no password.

## The stop rule

Stop, change nothing, and tell the approver at once if:
- a run gives HARNESS or INCOMPLETE twice;
- a run is refused by QAJev Cloud (I report the reason, and I never rerun it on this computer);
- the site asks for a sign-in or shows a cookie wall that blocks the page.

A stopped run is not run again without a new approval line.

## The result

One message to the approver and to Sam: each run's id, gate, report_url, and each finding with its page.
