# Contributing to QAJev

Thanks for helping. Bug reports, ideas, docs fixes and code are all welcome.

## Reporting a problem

Open an issue with:

- what you ran (the command, or the MCP tool and its parameters);
- what you expected, and what happened;
- the output of `qajev doctor`;
- if you can, the run's `report.md`. Check it for anything private first: page text, addresses, e-mails.

Security problems: please do not open a public issue; see [SECURITY.md](SECURITY.md).

## Setting up

```bash
git clone https://github.com/hanamorilabs/qajev && cd qajev
uv sync                                   # Python 3.12+, installs the dev tools too
uv run qajev doctor
```

## Checks

Run these before a pull request:

```bash
uv run ruff check .                       # lint
uv run basedpyright qajev                 # types (0 errors)
uv run pytest -q                          # offline tests: no browser, no network, no cost
```

GitHub Actions runs the same three on every push to `main` and every pull request, on macOS and Linux with
Python 3.12 and 3.13 (`.github/workflows/tests.yml`).

Tests that start a real (headless, throwaway) Chrome against the bundled demo site, or a Godot or Electron fixture
game, are opt-in:

```bash
QAJEV_LIVE=1 uv run pytest -q tests/test_live.py                     # Chrome, local pages only
QAJEV_LIVE=1 QAJEV_LIVE_JEV=1 uv run pytest -q tests/test_live.py    # + one paid Jev check (about $0.01)
QAJEV_LIVE=1 uv run pytest -q tests/test_native.py                  # needs Godot (or QAJEV_GODOT)
QAJEV_LIVE=1 QAJEV_ELECTRON=/path/to/electron uv run pytest -q tests/test_electron.py
```

The demo site: `python3 tests/fixtures/serve.py` serves it on `http://127.0.0.1:8765`.

## Guidelines

- **Safety first.** Changes must keep the guarantees in [docs/safety.md](docs/safety.md): production read-only and
  destructive never (`mutate` and `--allow-destructive` on a local dev host only, refused elsewhere with exit 5), no
  secrets typed, dangerous and destructive controls hidden, a cost cap. Tests that use a browser must
  only touch the local fixtures.
- **Keep product and harness apart.** A new way for a run to fail must say whether it is the product's fault or
  QAJev's.
- **Test the behaviour.** Add a test that fails without your change. Prefer the fixtures and fakes the existing
  tests use.
- **Fake secrets look fake.** A fixture password has the obviously fake `fixture-pass-*` form, and a test key comes
  from where it is published (an RFC's test vector, derived in the code). When the secret scanner (gitleaks) flags
  one, add a reviewed exception to `.gitleaks.toml` in the same pull request (the files it covers and the exact
  fake value, with why), never an inline allow,
  never a string split to get past it.
- **Plain docs.** If a user can see the change, update the docs in the same pull request. Write for someone who
  has never used QAJev.
- **Match the code around you:** the existing naming, comment style and module layout
  ([How it works](docs/how-it-works.md) has a map).

## Jev

QAJev pins [jev-ultrafast](https://github.com/browser-use/jev-ultrafast) to a commit, because it wraps a few of its
internals (`Browser.__init__`, `browser.cdp`, `model.post_json`, `field_text`). Bumping it needs the live tests run
again.

## License

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
