# Named PNG and JSON case attachments

Local implementation, 16 October 2026. **Unreleased.** The installed CLI, Cloud runner image and dispatcher are unchanged. Independent review, the held full suites and separate release approval still apply.

An ordinary browser scenario can request a named lossless PNG and a separate full JSON snapshot. Both are opt-in per case. Existing suites keep their current JPEG screenshots and short check previews.

```yaml
name: Scene export
base_url: http://127.0.0.1:8833
device: desktop
mode: readonly
scenarios:
  - name: final scene
    about: The scene document loads and keeps its final image and canvas dimensions.
    expect:
      status: 200
      js: document.readyState === 'complete'
      says:
        js: The document finished loading.
    capture:
      png: final-screen
      json:
        name: final-state
        js: >-
          ({title: document.title,
            canvases: Array.from(document.querySelectorAll('canvas'),
              c => ({width: c.width, height: c.height}))})
```

Set `capture.json.js` to the app state expression you need. The example reads the document title and canvas dimensions. Expressions may return a promise. QAJev evaluates the expression once and serializes that result using JSON semantics. Product assertions stay in `expect`.

## Capture behavior

- Names contain 1–64 letters, digits, `_` or `-`. Start with a letter or digit; omit the extension and directory. Unknown keys and malformed capture specifications are refused before a browser starts.
- Capture runs at the existing end-of-case screenshot point: after checks and `after` hooks, before the next case. Put a reset before the next case if you need the state before a reset preserved.
- PNG capture calls Chrome's PNG encoder directly, with no JPEG quality setting or JPEG conversion. A named PNG becomes the case's main screenshot. The default JPEG, model image and live-frame behavior stays unchanged.
- JSON is a full attachment, never the value of a JavaScript assertion. The exporter freezes one serialized value and transfers its UTF-8 bytes in small chunks. Values longer than 200 characters or a daemon message are preserved. Unicode and the end of the value remain intact.
- Existing known-secret redaction still applies. Full means no truncation; it does not remove credential masking. The Cloud runner's later seed masking updates the attachment manifests to match the delivered files.
- A JSON or PNG attachment is at most 50 MiB, matching the existing Cloud per-file limit. The existing 500 MiB total-run limit still applies on the Worker. Oversized JSON gives a plain capture error and publishes no partial or cut snapshot. Unserializable values, failed screenshots and incomplete transfers also give a capture error.
- Capture failure makes the case HARNESS, with the reason. Other successfully captured attachments remain available. A failed product check still keeps its capture when export succeeds. Capture does not turn a failed product assertion into PASS.
- `--no-shots` conflicts with an explicit PNG capture and gives an error. JSON-only capture works with screenshots off. Multiplayer `clients` and `real_devices` captures are not supported by this first version and are refused explicitly.

## Files and reports

Files use `artifacts/<case-name-and-hash>/<name>.png` or `.json`. The case hash includes the device-suffixed case name. Two cases whose display names sanitize the same way cannot overwrite one another's exports. Existing targets and symbolic links are refused.

Each scenario's `artifacts` list in `report.json` contains only metadata: `name`, `kind`, `path`, `bytes` and `sha256`. The state body stays in its JSON file. JavaScript check previews keep their existing 200-character limit.

Both `report.md` and `report.html` link the attachments. The Cloud runner uploads only explicitly listed, confined PNG/JSON attachments, checks their kind, size and digest, and publishes `report.json` last. Unlisted files are not exported. Report downloads fetch the attachments as bytes, verify their manifests and keep the relative links working offline. Missing or damaged required attachments make the download fail rather than claim completion.

## Release requirements

This change needs a coordinated CLI and qcloud uploader release. The prepared runner image must use the new CLI, and the dispatcher must ship the new uploader package. Those changes remain held for José's word. No release, image refresh, dispatcher restart, main push or paid acceptance run has occurred.

No Cloudflare Worker deployment or database migration is needed for this feature: the existing file endpoint already stores and serves PNG and JSON with SHA256 checks and the current size limits. The Worker source is unchanged.

Local proof uses a fake screenshot transport, real PNG fixture bytes, an offline JavaScript engine, fake file downloads and local temporary files. It is not a browser or Cloud acceptance run. Only focused light-lane tests and the three approved focused mutants run before tonight's restart; full suites remain held.
