# Security

QAJev lets a model click through real websites, so its safety rules (see [docs/safety.md](docs/safety.md)) are
security features. A way around any of them is a security problem, for example:

- making Jev type into a password, one-time-code or payment field;
- a write (form post, delete, other non-GET request) getting through on a site that is not loopback, in read-only
  mode;
- a hidden dangerous control (sign out, delete, pay...) becoming reachable;
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
