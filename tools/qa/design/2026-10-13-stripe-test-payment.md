# QAJev: pay on a Stripe test-mode checkout (design note)

Runner amendment (José, 14 Oct 10:36): QAJev Cloud (`--runner cloud`) replaces the local runner for the next website capture; the Orchestrator must re-approve this plan before any run.

Status: amended for redteam's conditional GO; waiting for the Orchestrator to approve this updated note.
Tonight is note-only: no checkout, frame probe, payment or POST recording, sandbox included. The two real-page
observations below wait for one morning local run under a plan the Orchestrator approves. No Stripe code until
the updated note is approved and those observations are recorded.

## Why

José's Twinbox journey (scenarios 17-26) goes through Stripe Checkout: Continue to payment, pay, and the app's
window unlocks by itself. Today QAJev cannot pay there, and that is right for every other page:

1. The guard disables card fields off loopback (guard.js `FIELD`). `guard.allow_secret_fields` is refused unless every
   host is loopback (suite.py).
2. A `fill` hook refuses a secret field off loopback (session.py `run_hook`).
3. The Pay or Subscribe button is a danger control ("pay"), hidden from Jev.
4. Production is read-only: a POST leaves the page only when `allow_requests` names it.

A 5th one showed on 13 Oct (run r_mv6tgcrpdf56e587): QAJev cannot even read a Stripe Checkout page today. Checkout
draws itself only after `POST https://api.stripe.com/v1/payment_pages/<session>/init`. The read-only guard blocks it,
and the page shows only Stripe's "Something went wrong". That is the reason for the narrow allowance below.

The exception opens these 5, only on Stripe's test mode. The init request opens for the page's whole visit, because
reading needs it. The other 4 open only during one hook. Everything else stays as it is.

## The shape

A suite declares what it expects to pay for, and one hook pays:

```yaml
base_url: https://twinbox-staging.purple-leaf-9f51.workers.dev  # approved staging app origin
test_payment:
  provider: stripe                                  # the only value
  item: "Twinbox Hobby: 3,700 credits a month"       # the one declared line item
  amount: "$10.00"                                  # the total due, not any price on the page
scenarios:
  - name: Stripe checkout pays with the test card
    url: ...                                        # or reached from the step before
    before:
      - test_pay: stripe                            # the only hook that may pay
    expect: {url: "..."}                            # the success page the suite expects
```

- **Jev never pays.** The fields stay disabled and the button stays hidden for Jev, also on this page. Only the
  `test_pay` hook types and clicks, as fixed code. No model decides it.
- **The card comes from QAJev, never the suite.** The hook types Stripe's published test card 4242 4242 4242 4242, a
  future expiry (12/34), CVC 123, and postal code 10001, from constants in QAJev. A suite that puts a card number,
  CVC or expiry in any `fill` is refused, as today.
- **The email**, if Stripe asks for it, comes from the suite's test account (its seed fixture with
  `"test_account": true`). It is not a secret field.

## The 6 conditions

The hook checks all 6 on the live page, at the moment it acts, after any redirect. If one fails, it types nothing and
the step ends as **refused**, with the condition that failed.

1. **Top page and card frames.** The top page is HTTPS on exactly `checkout.stripe.com` at the default HTTPS
   port. Not a subdomain, not a look-alike, and not Stripe inside a frame of another site. Card inputs may be in
   that top document or in frames whose exact hosts are recorded and approved in the morning evidence below.
   Each permitted card frame must belong to this Stripe top page; other frame hosts are refused. If the observed
   inputs are not framed, record that and keep the hook limited to the top document. Frame hosts are unresolved
   now: an example such as `js.stripe.com` in the review grants no permission.
2. **Test session.** The path is `/c/pay/cs_test_…` (or `/pay/cs_test_…`). A `cs_live_` anywhere in the URL refuses.
   The suite parser also refuses a scenario URL with `cs_live_`, before Chrome starts.
3. **Test card only.** The values are QAJev's constants above. Nothing else can reach a card field.
4. **Declared and approved.** The suite has `test_payment:`, and the run follows an approved plan file whose text
   names `test_payment`. Without either, the suite is refused at load time (exit 3).
5. **The right purchase and total due.** Before it types, the checkout has exactly one line item matching the
   suite's `item`, with quantity one. Additional items or another quantity refuse; this first shape does not
   declare multiple lines. Read the amount payable from Stripe's total element or the amount paired with
   "Total due today", and require it to equal the declared `amount`, including its currency. A line price,
   subtotal, crossed-out price or matching text elsewhere is not evidence of the total. An absent, ambiguous or
   different total refuses. The page must also show Stripe's visible test badge: "Sandbox" or "TEST MODE", in
   any case. On 13 Oct Stripe drew "Sandbox" on a
   cs_test_ session (run r_mv6tzaaxe006d1be), so a check for "TEST MODE" alone fails on a correct page. The badge
   never replaces condition 2: the hook also checks, by name, that the page's URL is
   `https://checkout.stripe.com/c/pay/cs_test_…`, so a live session can never pass whatever the badge says. A
   different plan or price refuses.

6. **The form is ready (the Orchestrator, 13 Oct 23:40).** On run r_mv6uekfwdc41189d the picture showed the
   checkout faded, as if Stripe dimmed the form or was mid-fade. The hook never acts on a faded form. It waits up to
   20 s until the card number field is enabled, visible, not covered, and its form is fully opaque (computed
   `opacity` 1 up its parents, including its containing frame). Not ready in time: the step ends as **stuck**, with the line "Stripe's checkout form
   was not ready to take input within 20 s", and nothing is typed.

José confirmed the approach on 13 Oct at 23:39: "This is all sandbox, so in sandbox in stripe we can use the stripe
test card."

## What opens while the page is open

- `POST https://api.stripe.com/v1/payment_pages/<the page's own cs_test_ session>/init`, and nothing else. QAJev
  takes the session id from the page's URL, so the allowance can never name another session. A `cs_live_` page gets
  none. This requires the declared/approved suite and its local, non-production context. Stripe's telemetry
  (r.stripe.com, m.stripe.com) stays blocked.

## What opens, during the hook only

- Only card fields in the verified top document or the exact approved card-frame hosts may be enabled. The
  morning read-only frame check determines which case applies before any code. Every such frame must sit under
  the validated Stripe top page; an unlisted host refuses. No frame is authorized by a guess from its name.
- The hook clicks the submit button by its own selector (`button[type=submit]` inside the checkout form), and only once.
- Pay-time POSTs use only HTTPS `api.stripe.com` and the exact paths recorded in the morning evidence and
  approved by the Orchestrator. Session-specific path templates are built from this page's own validated
  `cs_test_` ID, as for init. A different test session, a broad `/payment_pages/cs_test_` prefix allowance, or an
  unrecorded path is refused. If the trace needs a path without a session segment, name it exactly and record
  how it belongs to this checkout before approval. No pay-time endpoint is approved yet; `/confirm` and
  `/v1/payment_methods` in the review are examples, not permissions.
- On success, refusal, timeout or error, restore the previous request policy and every affected field/control,
  including allowed frames. Card fields must again be disabled and the pay button hidden before Jev observes
  the page. Nothing in the pay-time allowance survives the hook.

## What stays closed

- A CAPTCHA or a 3-D Secure challenge ends the step as **stuck**. QAJev never solves or skips one. The 4242 card does
  not ask for 3-D Secure.
- No live keys and no `cs_live_` session, on any runner.
- No other payment page. PayPal, Paddle, Stripe Elements on another site, and an app's own card form stay refused.
- Jev, `qa_check` and the CLI get no flag for this. Only a suite can declare it. The MCP server says so in one line.

## The report

It lists "Test payment: Stripe test mode, card ending 4242, session cs_test_…" with the session cut to 14 characters,
the item and total due it saw, and each field the hook typed, named but masked. The picture after the pay click is kept.

## Tests and planted faults

Each fault must make a named test fail:

1. The hook acts on `checkout.stripe.com.evil.test`, or on another host with the same path.
2. The hook acts on a `cs_live_` session.
3. The hook types when the amount differs ($100.00), or the item differs.
4. The hook types when neither the "Sandbox" nor the "TEST MODE" badge shows.
5. A suite `fill` with a card number reaches the field.
6. The suite loads without `test_payment:`, or with a plan that does not name it.
7. The POST allow outlives the hook, or allows another path.
8. The init allowance lets through another session's init, or telemetry.
9. Jev's own observation shows the pay button or an enabled card field.
10. The hook types into a faded form, or into a card field that is disabled or covered.
11. After the hook, a card field stays enabled, or the pay button shows to Jev.
12. The pay-time POST lets through another `cs_test_` session's path.
13. A card field in a frame from a host not approved in the morning evidence is typed into.
14. The page is `checkout.stripe.com` inside a frame of another site, not the top page.
15. The item and $10.00 show, but the total is another amount.
16. The suite's app or base URL is production or outside the explicit allowlist below.
17. The hook runs on another runner.
18. The submit is clicked twice.

The tests use a local copy of a Stripe-like checkout page, served under a `.test` host that a test maps to
`checkout.stripe.com`. No test reaches Stripe.

## Estimate

3 hours for code, tests and docs, after the updated note is approved and the morning evidence is recorded.
All surfaces in the same PR: the suite key, the hook, the
guard, the report, docs, the agent prompt, llms.txt and the CHANGELOG.

## The Orchestrator's additions (14 Oct)

7. **Local runner only, for now.** José moved QAJev to this Mac on 14 Oct at 01:10. The hook refuses on any other runner until he says otherwise.
8. **One click.** A planted fault that clicks the submit button twice must fail a named test.
9. **Staging or dev only, checked at load and by the hook.** `test_payment` requires the suite's `base_url`.
   Compare its parsed origin (scheme, exact host and effective port) with this initial Twinbox allowlist:
   - `https://twinbox-staging.purple-leaf-9f51.workers.dev` (HTTPS, port 443).
   - `http://127.0.0.1:8787` (the registered local dev environment).

   A missing base URL, user info, an unlisted origin, `twinbox.dev` or any production origin refuses at load
   (exit 3). Explicit non-Stripe HTTP(S) scenario/hook URLs must also use an allowed app origin. The hook
   rechecks the validated suite origin before acting; Stripe's test URL or badge does not replace this check.
   The run uses the suite's marked test account. Extending the app allowlist requires an updated approved design.

Design approved by the Orchestrator 14 Oct 2026 01:51 sha256:6677e62f1228f85e14e14f2da2a0cb98b85d2e6a63f699ceba6eda6536a2eb2a.
That approval names the earlier revision. This amendment still needs the Orchestrator's approval; the
conditional redteam verdict below does not open the code gate by itself.

## Morning evidence and proposed local plan — not approved to run

No checkout or payment runs tonight, including sandbox. The Orchestrator approves one morning local run and
its operator before it starts. Jev never pays; the Orchestrator or José records the real staging test-payment
requests. QAJev Cloud and Namespace machines are not used.

| Required observation | Current record |
|---|---|
| Real card inputs are in the top document or frames | Not observed; pending the morning run. |
| Exact permitted card-frame hosts and their Stripe top-page relationship | Unset. Record `none: top document only` if no frames are used; otherwise name each observed host. |
| Exact pay-time POST paths and their binding to the page's own test session | Unset. Init is specified above; it does not authorize any pay-time path. |
| Updated-note approval after these observations are written | Pending Orchestrator approval. |

Proposed sequence for that one run:

1. Confirm this Mac's local runner, the allowlisted Twinbox staging/dev origin, the marked test account and a
   plan naming `test_payment`. Use one run at a time; no production app or live Stripe session.
2. Open the intended `cs_test_` checkout. Before typing, inspect the top page and card-input frame hosts
   read-only. Record the line item, quantity, total due and test badge. Stop on a failed condition.
3. Only the operator named in the approved plan performs one test-card submit, once. Record the request
   method, exact HTTPS host/path, and whether each path is bound to the checkout's own session. Telemetry is
   not added to the allowlist. Stop on CAPTCHA or 3-D Secure; no second click or automatic retry.
4. Write the observed frame hosts and required request templates into this note. Use `<own cs_test_ session>`
   instead of a full session ID; omit keys, tokens, card-field values and request bodies. The Orchestrator
   reviews the completed record and approves the updated note before implementation begins.

This sequence is a proposal for approval, not permission to run it. qajev2 makes no checkout or payment action
tonight and writes no Stripe code.

## redteam's review (14 Oct 2026, 03:2x)

**Verdict: GO for the design, with 4 changes to make in this note before any code.** Everything else holds:
- Jev never pays.
- The card comes from QAJev's constants.
- The init allowance is pinned to the page's own session.
- `cs_live_` is refused at load and at the hook.
- The badge never replaces the URL check.
- Telemetry stays blocked.
- A CAPTCHA or 3-D Secure ends as stuck.

1. **Card fields in a frame: settle it on the real page, and write the answer here.** Stripe's hosted Checkout may draw its card inputs in frames that Stripe serves from another host of its own (for example `js.stripe.com`). Condition 1 and "What opens, during the hook only" then make the hook refuse every time. Record the frame hosts the real page uses (a read-only look, no typing). If the card fields are in frames, name the exact frame host or hosts the hook may type into. Require each frame to sit inside the `checkout.stripe.com` top page, and refuse any other frame host. The guard must restore those frames' fields too. If they are not in frames, write that here, and keep the rule as it is.

2. **Pin the pay-time POST to the page's own session, like the init.** "Paths with `/payment_pages/cs_test_`" lets any test session through, not only this page's. Allow exactly the paths this page's session needs (for example `/v1/payment_pages/<own cs_test_>/confirm`), built from the page's URL as the init allowance is. Before code, record the POSTs one real test payment makes. José or the Orchestrator does this on a staging checkout, by hand or with QAJev's network log. Write them here. If a POST outside `payment_pages` is needed (for example `/v1/payment_methods`), name it exactly, for the hook's window only.

3. **The amount is the total due, not any text on the page.** Condition 5 matches `item` and `amount` anywhere in the page text. A checkout with the Hobby line at $10.00 and an add-on, for a $110.00 total, passes. Read the total ("Total due today" or Stripe's total element), and refuse unless it equals `amount`. The item must be the only line item, or each line must be declared.

4. **"Staging or dev only" (addition 9) needs a check the hook can make.** The hook sees only Stripe's page. Name the mechanism. One way: the suite's app or base URL must be on a list of non-production hosts (for Twinbox, staging and dev), checked at load. Another: the checkout session's success URL host must be on that list. A suite that points at twinbox.dev production is refused at load (exit 3).

**Faults to add to the list**

11. After the hook, a card field stays enabled, or the pay button shows to Jev.
12. The pay-time POST lets through another `cs_test_` session's path.
13. A card field in a frame from a host not named under point 1 is typed into.
14. The page is `checkout.stripe.com` inside a frame of another site, not the top page.
15. The item and $10.00 show, but the total is another amount.
16. The suite's app or base URL is production (point 4).
17. The hook runs on another runner (addition 7).
18. The submit is clicked twice (addition 8, already named: keep it in this list).

redteam

### redteam's recheck of the amended text (14 Oct 2026, 04:1x)

**Verdict on the text: GO.** All 4 changes are now conditions the hook checks, not prose:

1. **Frames (condition 1, "What opens during the hook").** Card inputs may be only in the top document or in frame hosts recorded and approved from the morning evidence. Any other host refuses, and no host is permitted by name alone. Faults 13 and 14 cover it.
2. **Pay-time POST.** Only HTTPS `api.stripe.com`, with the exact recorded paths, each built from this page's own validated `cs_test_` id. A broad prefix, another session or an unrecorded path refuses. Faults 7 and 12 cover it.
3. **The total (condition 5).** Exactly one line item matching `item`, quantity one, and Stripe's total element equal to `amount`. An absent or ambiguous total refuses. Fault 15 covers it.
4. **Staging or dev (addition 9).** An origin allow list for `base_url` (scheme, exact host and port), checked at load (exit 3) and again by the hook. Fault 16 covers it.

The restore of the guard state after the hook is also now explicit: success, refusal, timeout or error, with allowed frames included. Fault 11 covers it.

Two small additions to make with the code. They do not block this text:
- Add faults for condition 5's new parts: the item with **quantity 2**, and a **second line item** whose total still reads $10.00.
- "Including its currency": write which currency the hook expects (for example USD, from Stripe's currency code or the element's currency text). "$10.00" alone is also the CAD or AUD symbol. The fault: the same $10.00 in another currency.

The code gate stays as the note says. The frame hosts and the pay-time paths are open until the one approved morning capture is written here. Then the Orchestrator approves the updated note.

redteam

Updated note text approved by the Orchestrator 14 Oct 2026 04:09 (redteam text GO before it). Code stays gated: first the one morning local capture run under a plan the Orchestrator approves, with the frame hosts and pay-time requests recorded here, then the Orchestrator GO. Add with the code: quantity-2 and second-line-item faults, USD stated with an other-currency fault (redteam).
Runner amendment approved by the Orchestrator 14 Oct 2026 11:01: the capture runs on QAJev Cloud. The capture plan still needs its own plan-hash approval before any run.
