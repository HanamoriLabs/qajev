# Model budgets and uncertain costs

`--cost-cap` limits the model allowance for a run. It does not cover runner minutes,
Cloud credits or payment fees. QAJev reserves an upper bound **before each HTTP
attempt**, including retries. Concurrent threads and the run's worker processes
share the allowance. A zero or exhausted budget makes no paid request.

The ledger uses integer microdollars. Reservations round up; caps round down.
Provider-reported valid dollar usage settles the attempt. Otherwise valid token
counts use the known route's input and output prices. Missing, invalid or
unpriceable usage keeps the full reservation as **unknown liability**. Timeouts,
invalid responses and upstream errors also keep it: an error does not prove the
provider did not charge. Retrying requires another reservation. Settlement is
idempotent. A worker that exits cannot release its held allowance. Network timeouts,
HTTP 502 and bad JSON never auto-replay an uncertain request. The transport retains
only the dependency's explicit 429/529/503 retry statuses (at most three attempts),
with a fresh full reservation for each. All failed-attempt liability stays held.

The existing run/report fields stay available. New reports additionally give
`actual_usd` (supplier-reported), `estimated_usd` (token-priced), `unknown_usd`,
`reserved_usd`, `cost_basis` and `reservation_basis`. `usd` is their total accounted
liability, including uncertain and in-flight amounts; it is not an invoice. A real
reported zero differs from absent usage. Supplier charges are rounded up to the
next microdollar. Historical reports retain their original descriptions.

## Known bounds

Official supplier documentation checked on 16 October 2026:

| Exact supplier route | Input context bound | USD per million input / output | Reserved USD per attempt |
| --- | ---: | ---: | ---: |
| TypeSafe `/v1/systemone`, default context | 65,536 | 0.042 / 0 | 0.002753 |
| Workers AI `@cf/cloudflare/clef` | 65,536 | 0.24 / 0 | 0.015729 |
| Workers AI `@cf/cloudflare/clef-flash` | 24,576 | 0.038 / 0 | 0.000934 |
| Workers AI `@cf/cloudflare/clef-omni` | 64,000 | 0.15 / 0 | 0.009600 |
| Workers AI `@cf/google/gemma-4-26b-a4b-it` | 256,000 | 0.10 / 0.30 | 0.030516 with 16,384 output tokens |

TypeSafe describes its default context as “64k”; QAJev uses 65,536 conservatively.
These are liability bounds, not average request costs. Input context includes
images when the route supports them. The transport does not add image, box or
long-context trial headers. Long-context body flags are refused. Gemma's request
gets an explicit `max_tokens` of at most 16,384; a smaller caller limit stays smaller.
Bounds depend on suppliers enforcing their published limits and prices. A charge
larger than its bound is recorded and stops further attempts; QAJev cannot undo
an unexpected supplier charge.

Sources: [TypeSafe models](https://docs.typesafe.ai/models.md),
[Clef](https://developers.cloudflare.com/workers-ai/models/clef/index.md),
[Clef-flash](https://developers.cloudflare.com/workers-ai/models/clef-flash/index.md),
[Clef-omni](https://developers.cloudflare.com/workers-ai/models/clef-omni/index.md),
[Gemma](https://developers.cloudflare.com/workers-ai/models/gemma-4-26b-a4b-it/index.md).

## Other models and overrides

An unknown model, host, price or context bound fails before the paid request.
OpenRouter IDs do not inherit TypeSafe's price or context limits. No automatic
paid qualification occurs. To use an unqualified route, an operator must explicitly
supply a conservative per-attempt USD upper bound with `QAJEV_MODEL_MAX_USD`.
It must include maximum input, output, images and any reasoning charges for every
route used in that run. This is labelled `operator_estimate`, not verified pricing,
and a cap based on that estimate depends on the estimate being correct.

`--usd-per-call` remains accepted as a TypeSafe reservation floor. It can increase
the documented bound; it cannot lower it. `QAJEV_MODEL_MAX_USD` likewise cannot
lower a known bound. Free local decisions, such as a Clef choice with only one
observed option, make no HTTP request and need no reservation.

## Cloud negotiation

Before submission the CLI reads `/api/v1/config`. Only an exact
`model_budget_enforced: true` enables the top-level `model_budget_usd` submission
field. This carries the effective CLI, suite or project model cap. The server may
enforce a lower service allowance; the client cannot enlarge it.

An explicit CLI or MCP `cost_cap`, or an authored suite/project `cost_cap_usd`,
refuses **before creating a Cloud run** if the
server does not confirm enforcement, or capability discovery fails. No local
fallback occurs. A default call without an explicit cap stays compatible with an
older server and prints a warning that enforcement is unconfirmed. This compatibility path does
not establish a new hard server budget.

The QAJev-owned Cloud job adapter may provide `verified_model_bounds` only after
authenticated job metadata confirms enforcement and the same job/base origin.
The mapping's keys are exact final decision/text URLs and its values are positive
integer microdollars. No body/model name, prefix, other job or redirect may select
that bound. A native documented bound cannot be lowered. Missing metadata supplies
no trusted bound, so a future strict runner refuses paid setup rather than invent
one. This CLI change must ship with that adapter contract; the installed runner
remains unchanged until its normal release gate.

The authenticated Worker's normalized `cost_basis` and `cost_uncertain` survive
settlement. A token-priced estimate never becomes supplier-reported dollars.
`conservative_unknown` or uncertain usage keeps the client's whole reservation,
even if the Worker also returns a numeric `cost_usd`. A validated `local_free`
response is zero. A missing or invalid Cloud basis stays unknown.
