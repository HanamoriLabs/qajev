# CLI/MCP model-budget proof

Runtime source: `4c78a16b5b056d1e1d9408e9fb8cdfb626e28bf9`.
Local build only. No push, installed-runner sync, paid provider/browser call,
Stripe operation, GitHub Actions, deployment or restart.

- **1,149 pass, 101 skip**, zero errors/failures: full Python suite through the
  stock heavy lane, `--patient`, one pytest worker. The budget race explicitly
  starts two worker processes. The opt-in live checks remain skipped.
- **127 focused checks pass** for reservations, providers and child-process
  binding; **154 pass** for Cloud submission, suites and projects.
- **11/11 planted faults caught by their named behavioral checks**, including
  hidden HTTP retries, missing usage becoming free, thread/process liabilities,
  repeated settlement, discarded worker liability, uncertain Cloud dollars being
  reported as actual, and automatic replay of an uncertain Clef response.
- Changed Python passes Ruff; the full `qajev` package passes basedpyright;
  `git diff --check` passes.

The initial 14 reservation regressions failed on the old check-then-charge ledger.
The authored suite/project-cap checks failed before strict negotiation. The two
lost/502 response checks failed before removing the generic Clef retry. Those
checks now pass. All provider transports and dashboard fixtures are fake or
loopback; none contacts a paid provider.

The first broad check exposed an unrelated test-environment mismatch: the reused
development environment imported installed 0.4.31 in child processes while the
source is 0.4.33. The final full gate uses an isolated, ignored test-only venv
bound to this worktree; the installed CLI/live runner was not changed.

Only 429/529/503 have the dependency's bounded transport retry loop, with a new
reservation each time. Lost connections, HTTP 502 and invalid HTTP JSON are
uncertain and never auto-replay. A valid HTTP 200/JSON reply with an invalid field
value may get a new bounded semantic attempt; prior billing remains accounted.

Unknown routes require an explicit operator upper bound, labelled as an estimate,
or authenticated exact-job Cloud bounds. An operator estimate does not establish
verified supplier pricing. Current Cloud calls with authored caps refuse before
enqueue unless the server explicitly confirms enforcement. Default legacy calls
stay compatible and warn that enforcement is unconfirmed.

The Cloud guard, its additive migration and authenticated adapter remain separate
review/enablement dependencies. This code does not enable public tenants or change
credit packs/tariffs. The installed demo source stays frozen.

[Machine-readable evidence](verification.json) records source hashes, each mutant,
and local raw-log/JUnit paths. [Budget contract](../../model-budgets.md) records
prices, bounds and settlement semantics.
