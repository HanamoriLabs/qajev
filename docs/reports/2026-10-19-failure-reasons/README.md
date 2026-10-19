# Failure reasons — 19 October

Status: PASS, built and verified; not released. Updated: 2026-10-19 17:55 MYT. Owner: qajev2.

The candidate extends CLI branch `fix/js-runtime-evidence-qajev2` from `94a16cfd43466bd9bf789f7f83786ab5fe883b6a`. It does not change the installed release.

## Behavior

- Literal `true` passes. A returned string fails with that string as its reason. Existing Boolean, error and string behavior is preserved.
- A nonempty named object passes only when every value is literal `true`. Every failing part is listed, for example `parts not true: "total"=35; "photo"=false`. `1`, `"true"`, nested objects and arrays cannot pass as true.
- Empty objects fail with `returned an empty object: no named checks`. An empty object contains no named assertions.
- Each failing part keeps its name, including parts beyond the short stored value preview. Individual values are bounded and redacted. Registered secrets and sensitive string fields are not emitted.
- Page and across-client checks share the verdict and transport policy. Undefined/functions/bigints and unread accessors remain visible failed parts. A custom `toJSON` cannot turn a non-true object into a passing part. Accepted objects do not run optional failure explanations.
- An unexpected command exit shows the last 400 characters of nonblank stderr, falling back to stdout. Registered secrets are removed before clipping. Expected nonzero exits and successful stdout checks still pass.

## Evidence

- Red proof before the implementation: **21 failed, 6 passed**. Box job `qajev2-20261019-174303-47053`.
- Focused proof after the implementation: **139 passed**. Box job `qajev2-20261019-174653-73548`.
- New planted faults: **10/10 caught**. Existing diagnostic faults: **8/8 caught**.
- Full regression: **1,139 passed, 105 skipped**, zero failures, 236.75 seconds. Box job `qajev2-20261019-174858-85592`, exit 0, completed at 17:54:31 MYT.
- Ruff and `git diff --check` pass.

The new mutants reject all-true parts, accept truthy values, drop later failure names, discard stdout, restore the stderr prefix, redact after clipping, use client truthiness, drop nonserializable parts, pass reason strings and expose sensitive part values. Each is caught by its named regression.

Report integration checks JSON, Markdown and HTML for both named JavaScript failures and command stdout reasons. Node exercises the actual page-side evaluator without a browser or external site.

All tests use workload-gpu through `boxtest --box-only`, with network disabled for the test command. No Mac build/browser, personal browser, paid QAJev Cloud run, model call or GitHub Actions run was used. No PR, release, image refresh, deploy, migration or dispatcher restart is authorized by this proof.

Raw logs and source hashes are in `/Volumes/Acer SSD/Development/qajev/qajev2/proof/failure-reasons-2026-10-19/`. `state.json` records the exact files and job IDs.
