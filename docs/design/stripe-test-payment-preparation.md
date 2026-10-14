# Stripe test-payment hook: integration preparation

Runner amendment (José, 14 Oct 10:36): QAJev Cloud (`--runner cloud`) replaces the local runner for the next website capture; the Orchestrator must re-approve this plan before any run.

Preparation only. No Stripe source code, test payment, dispatcher restart or cloud run is authorized by this
document. The canonical design note is `tools/qa/design/2026-10-13-stripe-test-payment.md` in the main
`qajev-public` checkout. Its redteam verdict requires four design changes before code, and the Orchestrator
must confirm that the gate is open.

## File ownership

| Surface | Allowed work after the gate opens |
|---|---|
| `qajev/suite.py` | Validate the suite-only declaration and hook, approved-plan permission, and staging/dev origin restriction. Reject production at load. |
| `qajev/session.py` | Dispatch the fixed hook with the local runner; keep enabled card fields and the pay button out of Jev's observation. |
| `qajev/guard.py`, `qajev/js/guard.js` | Own-session init allowance; exact pay-time requests and approved frame hosts during the hook; restore controls and network policy on every exit. |
| `qajev/report.py`, `qajev/report_html.py` | Mask typed fields, truncate the session ID, record the item and total, and keep the screenshot after the single submit. |
| New hook module | Keep fixed-card filling, exact top-page/session checks, amount/item verification and one-click enforcement together. Create no module before GO. |
| `qajev/mcp_server.py` | Server note and tool help text only. Never `_dispatch` or `_run_report`; alternatively wait for #142 and the MCP fix to merge. |
| `docs/games.md` | Wait for #142 to merge. Other documentation is available for this task. |
| `CHANGELOG.md` | Add under Unreleased; preserve qajev's entries during integration. |

Do not modify `cli.py`, `handoff.py`, `jobs.py`, `mackeys.py`, `native.py`, `openurl.py`, `plan.py`, `config.py`
or `tools/qa/twinbox/`. Notify qajev before any request involving CLI or MCP ownership. Update the working
branch from the lead's merged base before implementation to preserve its concurrent work.

Dependency check: [#142](https://github.com/HanamoriLabs/qajev/pull/142) merged at 2026-10-13 18:37:25 UTC,
head `3b980f213e8e5693f07a01cd11f26d5a38cdf340`. The stated wait on that PR is satisfied. The MCP fix has not
been checked here; the restricted note/help-only option remains available after the design gate opens.

## Design inputs still needed

1. A read-only observation of the real hosted checkout must name its card-frame hosts, or establish that the
   fields are not framed. Only listed hosts under the exact Stripe top page may be used.
2. The Orchestrator or José must record the exact requests from one staging test payment. The hook's pay-time
   allowance must use those paths and the page's own test session. qajev2 performs no payment action.
3. Amend the amount condition to use the total due and one declared line item, or explicitly declared lines.
   Matching the same price somewhere else on the page does not qualify.
4. Record the enforceable staging/dev check. The suite's declared app/base origins can be checked at load
   against an explicit non-production allowlist. The final list and mechanism must be in the design note.

The local runner, staging/dev restriction and exactly one submit remain mandatory. A double-click mutation
must fail a named test. The remaining design faults cover top-page/host/session checks, frame scope, total
due, card visibility, request scope, restored controls and disallowed runners. Tests use isolated local
fixtures and never reach Stripe.

Existing preparation baseline: Ruff clean, basedpyright zero errors, and 902 tests passed with 100 browser
tests skipped on the original `727a7ed` worktree. This documentation-only update does not validate new behavior;
implementation will require the complete gates again after the design and merge dependencies are settled.
