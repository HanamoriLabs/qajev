# QAJev UX checks: spec and fixtures

This folder holds the spec for the advisory UX checks and six fixture pages.
qajev2 builds the checks from `SPEC.md` and uses the pages as regression tests.

## Files

| File | What it is |
|---|---|
| `SPEC.md` | The spec: 5 checks, 9 finding ids, algorithms, limits, report format, options |
| `ux-covered.html` | Fixture: a floating button covers Save |
| `ux-keyboard.html` | Fixture: the keyboard hides Save, and the keyboard has no way to close |
| `ux-shift.html` | Fixture: a photo tray pushes "Take the next photo" down 160 px |
| `ux-input-kind.html` | Fixture: a date and a money value in plain text boxes |
| `ux-tap-target.html` | Fixture: a 24x24 close button and a label cut off with an ellipsis |
| `ux-clean.html` | Fixture: does everything right, must give 0 findings |

Each page is plain HTML, CSS and JS with no external files. Each one has a
comment at the top with the planted fault and the exact expected findings.

## How to run them

- Viewport: 390x844, `mobile: true`, `deviceScaleFactor` 3. Set it before the
  page loads. `ux-keyboard.html` reads the height once at load.
- Serve the folder over HTTP on a local dev host (for example
  `http://127.0.0.1:PORT/ux-covered.html`).
- Options: the defaults (`ux: advisory`, `ux_keyboard_scope: typed`) unless the
  table says otherwise.
- The steps are in plain words. Map them to the suite format.

## Expected findings

| Page | Steps | Count | Expected findings |
|---|---|---|---|
| `ux-covered.html` | open | 1 | `UX-COVER` on `button "Save"`. Coverer `button "Settings"`, fixed, 64x64 px. 3 of 5 tap points covered. |
| `ux-keyboard.html` | type "Comic Fiesta" into "Show name" | 2 | `UX-KEYBOARD-MAIN`: `button "Save"` (a `div role="button"`) hidden, top 768 px, visible area ends at 464 px. `UX-KEYBOARD-DISMISS`: no Done control, no `enterkeyhint`, tap outside does not blur. |
| `ux-shift.html` | tap "Take photo" | 1 | `UX-SHIFT`: `button "Take the next photo"` moved 160 px down (dx 0, dy 160). Cause: `div "Photos"`, 160 px tall, above it. |
| `ux-input-kind.html` | open | 2 | `UX-INPUT-DATE` on `input[text] "First day"` (value 2026-11-22, label found by the near-label rule). `UX-INPUT-NUMBER` on `input[text] "Table fee (RM)"` (money, `inputmode="decimal"`). |
| `ux-tap-target.html` | open | 2 | `UX-TAP` on `button "Close"`, 24x24 px. `UX-CLIP` on `button "Print sticker sheet for all selected cards"`, ellipsis, shows under 100% (about 50%, depends on the font). |
| `ux-clean.html` | 1. type "Comic Fiesta" into "Show name". 2. type "Bring the PSA slabs" into "Notes". 3. tap "Take photo". | 0 | None. Step 1 passes the dismiss check by `enterkeyhint="done"`. Step 2 passes it by tap outside (a textarea has no Return path). Step 3 adds the tray below the buttons, so nothing moves. |

## Extra cases for the same pages

| Page | Options | Expected |
|---|---|---|
| `ux-keyboard.html` | `ux_keyboard_scope: all` | The same 2 findings. Each one lists both fields: "Show name" and "Venue". |
| `ux-covered.html`, `ux-input-kind.html`, `ux-shift.html`, `ux-tap-target.html` | `ux_keyboard_scope: all` | The same findings as the main table. No `UX-KEYBOARD-*` finding. |
| `ux-clean.html` | `ux_keyboard_scope: all` | 0 findings. |
| `ux-covered.html` | `ux: strict` | The test fails with the reason `ux: 1 findings`. |
| `ux-clean.html` | `ux: strict` | The test passes. |
| any page | `ux: off` | No UX section in the report. |
| `ux-covered.html` | viewport 1280x800 | `UX-KEYBOARD` is skipped with the note "viewport is not a phone". `UX-COVER` still fires (the floating button follows the 390 px column). |

## What each page proves

- `ux-covered.html`: the hit test with 5 points, and the coverer description.
  The page cannot scroll, so the "scroll it out from under" retest must still
  say covered.
- `ux-keyboard.html`: an `overflow: hidden` container does not count as
  scrollable, even though script could scroll it. The main button is found by
  name on a React Native Web style `div role="button"`. The tap-outside probe
  blocks the browser's own blur, so only an app handler counts.
- `ux-shift.html`: the tapped control is not tracked, the move is measured in
  content position, and the cause is the added node above.
- `ux-input-kind.html`: one label comes from `aria-label`, the other from the
  near-label rule (a sibling `div`, no `<label>`).
- `ux-tap-target.html`: the 44 px minimum, and an ellipsis on a primary label.
- `ux-clean.html`: each check has a real pass path, so false positives show up
  here first.
