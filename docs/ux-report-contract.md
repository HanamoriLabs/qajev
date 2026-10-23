# Deterministic UX report contract

Reports can display deterministic UX findings beside a scenario's verdict. This
change handles saved results and crops; it does not start a detector, browser or
model. The runner supplies the outcome, and the report keeps it.

A deterministic result uses a `ux` object on the scenario. Existing UX note lists
and run-level design-consistency notes still use their existing format.

```json
{
  "mode": "advisory",
  "status": "complete",
  "findings": [{
    "check": "UX-COVER",
    "severity": "advisory",
    "step": 2,
    "element": {"desc": "button Save", "key": "#save", "rect": [270, 768, 96, 52]},
    "message": "Save is covered at 3 of 5 tap points.",
    "data": {"points_covered": 3, "points_total": 5},
    "repeats": 1,
    "screenshot": "ux/save-covered.png"
  }],
  "skipped": [],
  "notes": []
}
```

Markdown and HTML show the count beside the outcome, then the findings under
that scenario. Each row includes its check, severity, step, full message, element
key and crop. HTML crops open in the existing image viewer. JSON keeps the full
result, including measurements and repeated-finding counts.

In advisory mode, a passing result reads `PASS · UX 1 advisory`. Strict mode keeps
the runner's failed outcome and omits the advisory label. `mode: off` adds no UX
summary, section or crop inventory. A completed scan with no findings says
`No UX findings.`

An incomplete scan says `UX scan incomplete`, even when it has no findings. The
report lists skipped checks, their reasons and any unchecked fields. It never
labels an incomplete scan clean. Detector and strict-mode decisions belong to
the runner; the renderer does not infer them from a partial scan.

Crops must be safe run-relative paths under `ux/`. Absolute paths, parent
traversal and URLs cannot become image links. Every accepted crop joins the
report's evidence inventory; a missing file makes publication incomplete.
An absent or rejected crop reads `Not saved`. DOM-derived text is escaped in
Markdown and HTML, and crop URLs are encoded.

The report regression suite covers advisory, strict, off, clean, incomplete and
legacy results, missing crops and hostile text. The fault driver retains a
passing control, each named assertion failure, restored controls, source hashes
and mutation diffs. Run it with the project's test interpreter on a test box:

```sh
nice -n 19 uv run --frozen python tools/check-ux-report-faults.py --output reports/ux-report-proof
```

The output directory must be empty. The driver changes a temporary copy and
checks that the checkout's report sources remain unchanged.
