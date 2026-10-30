# Observe local runs from a registered machine

The observer publishes existing CLI jobs and surviving reports. It starts no browser, model call or suite. It does not change local browser slots, RAM admission, queue timing or scenario limits. Registered visibility requires the separately reviewed Cloud registry API; ordinary Mac sync remains supported.

## Human operator setup

After server deployment and client installation are approved, register a machine in the Cloud Runners page. Choose a machine label and project allowlist. Save its one-time observation credential yourself in an owned regular file, mode 0600, outside repositories and report roots. No agent reads, copies, prints or transfers this credential. Do not put it in shell profiles, arguments or service environment values.

First take a local inventory with explicit roots. This command needs no credential and sends nothing:

```sh
qajev observe --home /data/qa-home --project example-project \
  --report-root /data/qa-reports --state-dir /data/qa-observer --inventory
```

The first inventory snapshots report/evidence bytes locally even though it sends nothing. It is not a size-only dry run. The default total outbox cap is **512 MiB**. `--max-outbox-mib` sets another positive MiB budget after checking free disk. The cap includes retained blobs and the SQLite database; database writes also reserve rollback-journal headroom. Inventory prints `blob_bytes`, `outbox_bytes` and `max_outbox_bytes`. At the cap, observation stops before the next blob or database growth, prints the retained byte counts, exits with a configuration failure and keeps pending work. A large first backfill needs enough approved budget for its pending history. Confirmed uploads release their blob storage after the server's final manifest check.

The report root is an allowlist. Include each directory in which the selected CLI jobs write external `--out` results. The observer reads only the selected home's job metadata and named report evidence under these roots. It rejects symlink evidence and paths outside the roots. Backfill includes surviving `report.json` folders even if the old job index was pruned. Missing historical evidence is listed explicitly; vanished reports cannot be recreated by observation. Review the inventory before upload.

Then upload one cycle:

```sh
qajev observe --home /data/qa-home --project example-project \
  --report-root /data/qa-reports --state-dir /data/qa-observer \
  --url https://cloud.qajev.com --credential-file /data/qa-machine.credential --once
```

### A machine that serves several projects

When one home holds jobs of several projects, map each folder to its project with `--project-for FOLDER=PROJECT` (repeatable). A job belongs to the deepest mapped folder that holds the folder it started in; a job without a known start folder, and a surviving report without its job, are matched by their report folder. A known start folder decides alone: a job that started in an unmapped folder never takes the project of the folder its report went to. A folder mapped to two different projects is refused. A job that no folder holds gets `--project` when it is given, else it is skipped and counted as `no_project` in the printed counts. Folders match whole path parts: `/data/tw` holds `/data/tw/qa`, not `/data/twin`. The map is the operator's choice: a job's own metadata never picks its project, and no folder path is sent. Each mapped project must be on the machine's registered allowlist.

```sh
qajev observe --home /data/qa-home --report-root /data/tw --report-root /data/verse \
  --project-for /data/tw=twilight --project-for /data/verse=figgyz-verse \
  --state-dir /data/qa-observer --inventory
```

The map is part of the outbox's configuration. Changing it needs a new `--state-dir`, as a changed home or root does. An outbox made without a map opens unchanged.

Omit `--once` to observe every 60 seconds. `--every` changes the interval, with a minimum of 5 seconds. Every run is attributed to the authenticated machine; local provenance cannot impersonate another machine. The configured project must be on its registered allowlist. The observer uses its machine credential only for control-plane HTTPS, refuses redirects, and never passes it to Chrome, hooks, model providers or subprocesses.

An owner-user service can run this command with `Nice=19`, `UMask=0077` and a private persistent state directory. Its `ExecStart` contains only the credential file path. No service is installed automatically. This control-plane connection is separate from the application browser's host allowlist. It does not widen browser networking or grant permission for screenshots to be judged by a model.

## Durable publication

The outbox snapshots sanitized report and evidence bytes locally before attempting transport. Its SQLite checkpoint and content-addressed blobs survive restart, source pruning and a lost upload reply. A run is acknowledged only after the server confirms its local ID mapping and an empty missing-file manifest following all uploads. Repeating an observation updates one Cloud run; a changed live/final snapshot remains pending until its own acknowledgment. An outbox is bound to its home, roots, project, folder map, endpoint and registered machine identity.

Keep the state directory: its checkpoints prevent duplicate publication, and its pending blobs may be the only surviving evidence. After the server confirms every file and the acknowledgment commits locally, the observer removes blobs that no pending run still needs. Under its exclusive outbox lock, it also removes unreferenced hash-named blobs left by superseded pending snapshots, and finishes interrupted cleanup on restart. A lost reply or incomplete manifest keeps every pending blob. Unchanged confirmed reports are not copied back into the outbox on the next inventory. A changed report gets a fresh pending snapshot. If a pending blob is missing, inventory restores it from an unchanged surviving source. Without that source, upload reports the local run and missing durable evidence, keeps the run pending, and does not expose a filesystem traceback. A cleanup failure does not replace an earlier upload error. This is local outbox cleanup, not Cloud evidence deletion. Back up pending state and monitor its disk use. Stop the observer before moving it. Use a separate state directory for a different home, endpoint or registration. A permanent per-run refusal names its local ID so the operator can find the blocked item. Malformed image metadata is recorded as missing evidence while the readable report still publishes. An expired/revoked credential or failed request leaves work pending and exits with the configuration failure code; operator action is explicit. Never rerun a suite to repair an upload.

When the outbox is full, check available disk and raise `--max-outbox-mib`, or choose a new private `--state-dir` with the same machine registration, home, report roots, project and endpoint. Uploads remain idempotent under that machine identity. Keep the old state directory until all pending work is confirmed or safely backed up. A new state directory can rescan surviving source reports, but cannot recover unsent evidence whose source was pruned; use the retained old outbox for that work.

The dashboard shows every scenario, reported model cost and evidence completeness. External machine cost is **not tracked**, rather than a measured zero. Unknown scenario outcomes become an explicit harness row with the original outcome named; the full source report is retained. The server supports 10000 tests and 2000 declared files per observation, up to 50 MiB per file and 500 MiB per run. Oversized or unreadable input is refused, never silently acknowledged.
