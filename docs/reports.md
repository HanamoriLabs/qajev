# Reading a report

Every run writes a folder with:

| File | For |
|---|---|
| `report.html` | people: one page, opens in any browser, works offline, light and dark themes |
| `report.md` | people and chat: the same content as text |
| `report.json` | programs and AI agents |
| `shots/*.jpg` | the screen at the end of each scenario |

The CLI prints the folder when the run ends. `qajev report <folder>` prints the Markdown again, and
`qajev report <folder> --html` rebuilds the page from `report.json`.

![A report](images/report-overview.png)

## The gate

At the top: the verdict for the whole run.

| Gate | Means | Exit code |
|---|---|---|
| **PASS** | every scenario passed | `0` |
| **FAIL** | at least one scenario failed: the product is wrong somewhere | `1` |
| **INCOMPLETE** | nothing failed, but something could not be judged (stuck, harness, skipped...) | `2` |

The exit codes make QAJev easy to use in CI. Other exit codes: `3` a mistake in the suite or options, `4` no
browser available, `130` stopped.

## Outcomes

Each scenario ends with one outcome. The most important idea in QAJev: **a failure of the product and a failure of
the test tool are kept apart.**

| Outcome | Means | What to do |
|---|---|---|
| **pass** | every expectation held | nothing |
| **fail** | the product is wrong: a check failed, or the page did not load | read the reason and the failed checks |
| **stuck** | Jev looked for a way forward and found none (QAJev also scrolls and looks again, twice) | look at the screenshot: often a real usability problem |
| **harness** | QAJev's side: time or action budget used up, cost cap reached, a page that never stops changing, a model error | says nothing about the product; retry or narrow the goal |
| **unverified** | the scenario had no expectations | add some |
| **skipped** | a scenario it depends on did not pass, or the machine was too busy | fix that first |

`--strict` counts **stuck** as a failure.

A run with a stored test account says who signed in, under **Run** ("Sign-in: as qa+shop@example.com (account
shop-tester), in 3.1 s"). When the sign-in fails, that line gives the site's reason and every scenario is
**harness**: nothing was tested.

## Why a scenario ended as it did

Each scenario explains itself: the reason, each check with what was found instead, where Jev ended up, and what the
page said.

![A failed scenario, explained](images/report-scenarios.png)

A **stuck** scenario says what QAJev tried (here: the page cannot scroll, so the note at the bottom is out of reach,
which a visitor would hit too):

![A stuck scenario](images/report-stuck.png)

## The smoke crawl's report

A smoke crawl lists every page it visited, its outcome, and the findings per page:

![A smoke crawl's report](images/report-smoke.png)

## Findings

Findings are problems noticed along the way, separate from the outcome (a scenario can pass and still have
findings):

| Severity | Examples |
|---|---|
| **S1** | the page itself answered with a server error (HTTP 5xx); a game froze (soft-lock) or crashed |
| **S2** | an uncaught script error, a missing page (HTTP 4xx), a blank page, a blank screen or spinner over 10 seconds, a broken image or link, form fields without labels, buttons without names, a request or script the page's Content Security Policy blocked (`blocked by CSP`, other hosts' tags and pixels included), a game's own error reports |
| **S3** | smaller problems: a failed request for a secondary file, console errors, what a report-only Content Security Policy would block (`CSP violation (report-only)`), a missing title, language, heading or description, images without alt text, slow loading, layout shifts, tap targets under 24 px |

Repeated findings are grouped (`×3`). In a project, a finding you listed as `[[known]]` is shown as known, with
your note, and not raised again.

## Jev's steps

For each scenario the report lists what Jev did, and for every screen **how sure Jev was** of its next step
(the probability of its choice, and the runner-up). A screen where Jev was unsure ("one obvious next step?
unclear") often points to a confusing page.

## Cost

The report shows what the run cost: Jev's decisions, the text helper's calls, and the cap. Typical numbers: a
decision costs about $0.0005 with TypeSafe (less through OpenRouter), so a scenario of ten steps costs well
under a cent.

## Games

A game's report adds, for each real-time play step: frames per second (median and the slowest 10%), frame time,
memory at the start, peak and end, the game's own numbers at the end (score, level...), Jev's decisions with the
time they were made, and a timeline sampled every half second.

![A game's play step](images/report-play.png)
