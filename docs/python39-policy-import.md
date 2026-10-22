# Shared policy import on Python 3.9

The Cloud launch-agent templates run `/usr/bin/python3`. On the release Mac that is Python 3.9.6.
The shared policy used evaluated `int | None` and `str | None` annotations, which stop service imports
on that Python minor. Postponed annotations fix the whole module without a version-specific branch.

Keep the standalone policy copies in the CLI and Cloud repos byte-identical. The CLI keeps its declared
Python minimum; the Python 3.9 tests exercise its standalone policy module, not the entire CLI.
The Cloud test also imports dispatcher and sync without calling their main functions or reading credentials.

## Regression

Point `QAJEV_TEST_PYTHON39` at a Python 3.9 executable. Each isolated child verifies its actual
major/minor before importing the policy. It checks the import, clean diagnostics that keep the error
reason, metadata cleaning, and exact runner args/file declarations.

The test skips when no Python 3.9 executable is available during an ordinary suite. The dedicated
fault driver requires an explicit interpreter and refuses a skipped control, so a missing interpreter
cannot produce a green release proof.

Run on workload-cpu through the normal boxtest queue:

```sh
rtk proxy python3 tools/check-policy-python39-faults.py --python39 /path/to/python3.9
```

The driver first passes the control, then plants these faults one at a time and restores exact bytes:

| Fault | Mutation | Named regression |
|---|---|---|
| P39-1 | Remove postponed annotation evaluation | test_shared_policy_imports_on_python39 |
| P39-2 | Stringify only the first failing annotation | test_shared_policy_imports_on_python39 |

The diagnostic and request-policy tests catch both faults too. Cloud's
`test_dispatcher_and_sync_import_on_python39_without_starting_services` catches both at the actual
module entry points. A final passing control and source hash verify restoration.

## Next release

Check the launch-agent interpreter before any source swap, and bind this compatibility proof to the
candidate policy hash. Run the existing privacy/input-transport checks as well. This change prepares
code for review; another live attempt still needs redteam GO and the release word. The regression and
fault proof call no model, start no service, and use no Keychain.
