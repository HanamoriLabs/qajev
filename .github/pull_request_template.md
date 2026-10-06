## What changes, and why

<!-- In plain words: what a user or an agent sees differently, and what it fixes. -->

## Tests

<!-- The test that fails without this change, and what the full suite, ruff, basedpyright and gitleaks said. -->

## Every surface

Tick what this pull request updates, or say why a surface needs nothing (see CONTRIBUTING, "Every surface").

- [ ] CLI: `--help`, flags, exit codes
- [ ] `qajev doctor`
- [ ] MCP: tools, parameters, docstrings, the server's instructions
- [ ] JSON: `report.json`, `--json`, job results
- [ ] `README.md` and `docs/`
- [ ] `AGENT_PROMPT.md`
- [ ] `site/llms.txt` and `site/index.html`
- [ ] `CHANGELOG.md` (with a migration note if breaking)

## Safety

- [ ] Keeps the guarantees in `docs/safety.md`: production read-only, destructive never, no secrets typed, a cost cap
