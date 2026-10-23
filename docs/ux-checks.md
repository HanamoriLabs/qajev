# Deterministic UX findings

QAJev runs these web checks in each test and in `smoke`, without model calls.
They can find a usable-looking page whose main control is covered, a phone form
whose keyboard hides Save, a control that shifts after a tap, the wrong keyboard
for a date or number, a small tap target, or a primary label that is cut off.

The default is `ux: advisory`. Findings have crops and appear next to the test's
verdict in JSON, Markdown, HTML and the Cloud run page. Advisory findings do not
change a test's verdict or exit code. `ux: strict` fails a test with findings;
`ux: off` runs no UX checks and adds no UX section. Run flags override scenario
options, which override suite options, which override defaults.

```yaml
ux: advisory
ux_checks: [cover, keyboard, shift, input-kind, tap, clip]
ux_keyboard_scope: typed
ux_keyboard_ratio: 0.45
ux_keyboard_main: reachable
ux_keyboard_press_return: false
ux_settle_ms: 750
ux_shift_px: 8
ux_tap_min_px: 44
ux_max_findings: 10
```

The equivalent flags use hyphens, for example `--ux strict`,
`--ux-keyboard-scope all`, `--ux-checks cover,tap,clip` and
`--ux-keyboard-press-return` (or `--no-ux-keyboard-press-return`). The web MCP tools
accept the same option names.

| Finding | What it measures |
|---|---|
| UX-COVER | A center tap or at least two corner taps hit another element. |
| UX-KEYBOARD-FOCUS | Less than 90% of the typed field remains visible. |
| UX-KEYBOARD-MAIN | The main button is hidden and cannot be reached by finger scrolling. |
| UX-KEYBOARD-DISMISS | No Done control, usable Return hint or app tap-outside handler closes the keyboard. |
| UX-SHIFT | Another control moves more than 8 CSS pixels after an action. |
| UX-INPUT-DATE | A date label, value or hint uses a plain text field. |
| UX-INPUT-NUMBER | A money/count field opens a full keyboard. |
| UX-TAP | A tap area is smaller than 44 CSS pixels (with a 0.5 pixel rounding allowance). |
| UX-CLIP | A primary label has ellipsis or text is partly hidden by hard clipping. |

Keyboard checks use a real viewport resize before typing, then restore metrics,
scroll and focus before the requested step. The default checks typed fields;
`ux_keyboard_scope: all` also checks visible fields on each new screen. Desktop
widths over 500 pixels skip keyboard checks. `ux_keyboard_main: visible` requires
the main button to remain visible without scrolling. A matching `enterkeyhint`
is trusted unless `ux_keyboard_press_return` requests a guarded real Enter test.

Add `data-qa-main` to identify the main action. A Done control can use
`data-qa-dismiss-keyboard`. To suppress a known case, add
`data-qa-ux-ignore="UX-TAP UX-CLIP"` (or `all`) to the element or an ancestor.
React Native Web uses `dataSet={{ qaMain: true }}` and equivalent attributes.

The scan stops after 300 controls or 200 ms of scan work. If it stops early,
the UX result says **scan incomplete** and lists unchecked element keys and
descriptions. It never calls that scan clean. A later scan does not erase an
earlier step's incomplete coverage. Strict mode fails an incomplete scan.
Controls changed by QAJev's safety guard are also listed as unchecked. The UX
checks never unhide or enable guarded controls to measure them.
Static findings are deduplicated per test, retain their first crop, and count
repeats; each finding ID has its own configured cap.

Crops keep the control's red outline and the orange outline of a cover or
layout-shift cause. A shift crop also includes the dashed red outline of the
previous position. Keyboard findings keep the resized viewport and mark a main
button that is below it. Temporary outlines are removed before the next step.

These checks cover the DOM of the current web page. They do not inspect canvas,
WebGL, cross-document frames or native iOS/Android keyboard behavior. A web pass
does not prove native keyboard avoidance or native `hitSlop`.

The supplied regression pages live in `tests/fixtures/ux`. Browser-free tests
run in ordinary pytest. Rendered tests are opt-in and run on an approved Linux
box with an isolated headless Chrome profile:

```sh
nice -n 19 python tools/prove-ux-browser.py --chrome /usr/bin/chromium \
  --headless --out reports/ux-rendered-proof
```

The proof tool closes its own Chrome after each batch, uses no user profile or
model, refuses macOS browser runs, and stops if load1 exceeds 60.
