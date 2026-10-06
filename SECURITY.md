# Security

QAJev lets a model click through real websites, so its safety rules (see [docs/safety.md](docs/safety.md)) are
security features. A way around any of them is a security problem, for example:

- making Jev type into a password, one-time-code or payment field;
- a run changing a production site: any host that is not a local dev host (`localhost`, `127.0.0.1`, `[::1]`,
  `*.localhost`, `*.test`), in any mode. That includes a `DELETE`, `PUT` or `PATCH` leaving a production page, a
  `POST` leaving it that `allow_requests` does not name, a mutating or `--allow-destructive` run that starts
  against such a host instead of being refused, and a local page reaching a production host with a write;
- a write (form post, delete, other non-GET request) getting through in read-only mode;
- a hidden dangerous or destructive control (sign out, delete, refund, cancel, pay...) becoming reachable, on
  production in any way, or locally without `--allow-destructive`;
- a page's `confirm()`, `prompt()` or `beforeunload` getting an answer other than "no" on production, or Delete or
  Backspace reaching a production page outside a text field;
- a download reaching the disk;
- a secret from the environment appearing in a report;
- escaping the cost cap;
- a game run touching the player's real save folder, or sending input outside the game.

## Reporting

Please **do not open a public issue**. Report it privately through GitHub's
[security advisories](https://github.com/hanamorilabs/qajev/security/advisories/new) for this repository.

Include what you ran, what happened, and the smallest page or suite that shows it. You will get an answer within a
few days. Once it is fixed, the advisory is published with credit to you, if you want.

## Supported versions

Only the latest release gets security fixes.
