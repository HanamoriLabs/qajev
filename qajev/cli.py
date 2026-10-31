"""qajev: QA any website with Jev. Heavy imports are deferred so `qajev --help` stays instant."""

import argparse
import contextlib
import json
import os
import shlex
import shutil
import signal
import sys
import threading
import time
from pathlib import Path

from . import __version__, config, lowprio

EXIT_CONFIG, EXIT_BROWSER, EXIT_REFUSED, EXIT_INTERRUPTED = 3, 4, 5, 130
DASHBOARD_STOP_S = 15  # `qajev dashboard --stop` waits this long for the dashboard to exit and free its lock

SUITE_TEMPLATE = """\
# QAJev suite. Run: qajev run {name}
name: My site
base_url: http://localhost:3000
mode: readonly            # readonly (default, blocks writes) | mutate (a local dev host only: localhost, *.test...)
device: desktop           # desktop | tall | phone | tablet | WIDTHxHEIGHT
budget: {{actions: 20, seconds: 90}}
cost_cap_usd: 1.0
# persona: "You are a first-time visitor who has never seen this product."
# hosts: [accounts.example.com]      # extra hosts Jev may visit (e.g. a sign-in provider)
# guard: {{deny: ["\\\\bexport\\\\b"], allow: [], block_urls: ["*://*/logout*"], redact_emails: false}}

scenarios:
  - name: home loads
    url: /
    expect:
      text: ["Welcome"]            # case-sensitive substrings of the page's visible text
      absent: ["Something went wrong"]

  - name: find pricing
    url: /
    goal: Find the pricing page and open it. Stop when plan prices are visible.
    expect:
      url: /pricing
      js: document.querySelectorAll('[data-plan]').length >= 2

  # A step without url continues in the previous scenario's tab and depends on it passing.
  - name: compare plans
    goal: Open the details of the most expensive plan. Stop when its features are listed.
    expect:
      text: ["Features"]
"""


def _load_flags(g):
    env = os.environ.get
    g.add_argument("--load-high", type=float, default=float(env("QAJEV_LOAD_HIGH", "150")),
                   help="wait before a scenario (smoke and play: before starting) while the 1-min load average is "
                        "at or above this (0 = never wait; default $QAJEV_LOAD_HIGH or 150)")
    g.add_argument("--load-ok", type=float, default=float(env("QAJEV_LOAD_OK", "100")),
                   help="resume once load falls below this (default $QAJEV_LOAD_OK or 100)")
    g.add_argument("--load-wait", type=float, default=float(env("QAJEV_LOAD_WAIT", "600")),
                   help="seconds of waiting allowed per run (default $QAJEV_LOAD_WAIT or 600)")


PLAN_HELP = ("the approved test plan this run follows (Markdown; overrides the suite's plan:). Without one the run "
             "warns; with QAJEV_REQUIRE_PLAN=1 it is refused")
RUNNER_HELP = ("where the run goes: local (this Mac), cloud (QAJev Cloud; a run that cannot go is refused, never run "
               "here), auto (the cloud when it can, else this Mac, saying why). Default: $QAJEV_RUNNER (the shell, "
               "then ~/.qajev/.env), then the project's runner, else local")


def _runner_flags(p, website=True):
    from .config import CLOUD_MODELS, RUNNERS

    g = p.add_argument_group("where it runs")
    g.add_argument("--runner", choices=RUNNERS, help=RUNNER_HELP)
    if website:
        g.add_argument("--serve", metavar="DIR[:PORT]",
                       help="cloud runs: ship this built site folder; the cloud machine serves it on its own "
                            "127.0.0.1:PORT (default: the port the run opens, else 8877)")
        g.add_argument("--model", choices=CLOUD_MODELS,
                       help="cloud runs: the model that decides (default: the dashboard's)")


def _common(p):
    g = p.add_argument_group("browser and run")
    g.add_argument("--cdp-url", help="attach to an existing Chrome DevTools endpoint instead of QAJev's own Chrome")
    g.add_argument("--profile", default=os.environ.get("QAJEV_PROFILE", "default"),
                   help="QAJev Chrome profile (sign in once with `qajev browser login`)")
    g.add_argument("--headless", action="store_true", help="run QAJev's Chrome headless")
    g.add_argument("--ephemeral", action="store_true", help="throwaway profile, deleted after the run")
    g.add_argument("--browser-runs", type=int, metavar="N",
                   help="machine-wide concurrent browser runs (1..64; QAJEV_BROWSER_RUNS, default 1). "
                        "Above 1 uses a fresh profile per run. RAM may lower admission; --jobs is separate")
    g.add_argument("--plan", type=Path, help=PLAN_HELP)
    g.add_argument("--real-devices", help="also run each website scenario in a real device browser: android (Chrome "
                                          "on a read-only emulator), ios (Safari on a throwaway simulator clone; "
                                          "page content not readable yet, comes out harness); comma-separated, off "
                                          "by default")
    g.add_argument("--devices", help="every website test runs on each of these, comma-separated (default "
                                     "desktop,phone, or $QAJEV_DEVICES); e.g. --devices desktop for desktop only")
    g.add_argument("--out", type=Path, default=Path(os.environ.get("QAJEV_OUT", "qajev-runs")),
                   help="where run folders go (default ./qajev-runs)")
    g.add_argument("--env-file", help="file with TYPESAFE_API_KEY etc. (default $QAJEV_ENV_FILE, then ~/.qajev/.env; "
                                      "never the current folder's .env)")
    g.add_argument("--jev-provider", choices=["auto", "typesafe", "openrouter", "cloudflare"],
                   help="who makes the decisions (default auto: TypeSafe key if set, else OpenRouter key; "
                        "cloudflare: Clef or Clef-flash, only when chosen, see QAJEV_CLEF_MODEL)")
    g.add_argument("--cost-cap", type=float, help="hard cap in USD for the whole run (default: suite, else 1.00)")
    g.add_argument("--usd-per-call", type=float, default=0.0005, help="estimated TypeSafe cost per decision")
    g.add_argument("--strict", action="store_true", help="count stuck scenarios as failures")
    g.add_argument("--no-shots", action="store_true", help="skip end-of-scenario screenshots")
    g.add_argument("--motion", choices=["reduce", "full"],
                   help="reduce (default): pages are told the visitor prefers reduced motion, so animation-heavy "
                        "sites stop changing under Jev; full: as a default browser")
    g.add_argument("--color-scheme", choices=["light", "dark"],
                   help="tell the page the visitor's system prefers light or dark (prefers-color-scheme), for every "
                        "test that names none; a test's own color_scheme wins")
    g.add_argument("--cpu-throttle", type=int, choices=range(1, 9), metavar="N",
                   help="run the page's CPU N times slower (1 to 8; default the suite's, else 1), e.g. 4 for a "
                        "phone-like phone pass; the report says so")
    _load_flags(g)
    g.add_argument("--json", action="store_true", help="print the report JSON on stdout")
    g.add_argument("--events", action="store_true", help="stream JSON-lines progress events on stderr")
    g.add_argument("--quiet", "-q", action="store_true", help="no progress output")
    g.add_argument("--background", action="store_true",
                   help="start the run as a job and return at once; follow it with `qajev jobs`, stop it with "
                        "`qajev stop`")
    _runner_flags(p)
    _ux_flags(p)


def _ux_flags(p):
    from . import ux_options

    def value(key, convert):
        def read(text):
            try:
                return ux_options.parse({key: convert(text)})[key]
            except (ValueError, TypeError) as e:
                raise argparse.ArgumentTypeError(str(e)) from None
        return read

    g = p.add_argument_group("deterministic UX checks (no model calls)")
    g.add_argument("--ux", choices=ux_options.ENUMS["ux"],
                   help="advisory (default): report UX findings; strict: findings fail; off: skip UX checks")
    g.add_argument("--no-ux", dest="ux", action="store_const", const="off", help="same as --ux off")
    g.add_argument("--ux-checks", type=value("ux_checks", str), metavar="CHECKS",
                   help="comma-separated cover,keyboard,shift,input-kind,tap,clip (default: all)")
    g.add_argument("--ux-keyboard-scope", choices=ux_options.ENUMS["ux_keyboard_scope"])
    g.add_argument("--ux-keyboard-ratio", type=value("ux_keyboard_ratio", float), metavar="RATIO")
    g.add_argument("--ux-keyboard-main", choices=ux_options.ENUMS["ux_keyboard_main"])
    g.add_argument("--ux-keyboard-press-return", action=argparse.BooleanOptionalAction, default=None,
                   help="verify that Return closes the keyboard, while preventing form submission")
    g.add_argument("--ux-settle-ms", type=value("ux_settle_ms", int), metavar="MS")
    g.add_argument("--ux-settle-timeout-ms", type=value("ux_settle_timeout_ms", int), metavar="MS",
                   help="cap each UX quiet wait at 0–10000 ms (default: 10000); every scan still runs")
    g.add_argument("--ux-shift-px", type=value("ux_shift_px", float), metavar="PX")
    g.add_argument("--ux-tap-min-px", type=value("ux_tap_min_px", float), metavar="PX")
    g.add_argument("--ux-max-findings", type=value("ux_max_findings", int), metavar="N")


ALLOW_DESTRUCTIVE_HELP = ("show delete, remove, refund, cancel... controls, hidden by default everywhere; local dev "
                          "hosts only: a production host is refused (exit 5)")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="qajev",
        description="QA any website with Jev (TypeSafe x Browser Use): goals in plain words, verdicts from the page.",
    )
    parser.add_argument("--version", action="version", version=f"qajev {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    observe = sub.add_parser("observe", help="publish registered-machine reports through a durable outbox; "
                                            "never run tests")
    observe.add_argument("--home", type=Path, default=config.HOME, help="QAJev home whose jobs are observed")
    observe.add_argument("--report-root", type=Path, action="append", required=True,
                         help="approved report root, including external --out folders (repeatable)")
    observe.add_argument("--state-dir", type=Path, required=True, help="private durable observer state, mode 0700")
    observe.add_argument("--project", help="approved project name for every job no --project-for folder holds")
    observe.add_argument("--project-for", action="append", default=[], metavar="FOLDER=PROJECT",
                         help="jobs started in FOLDER (or, with no known start folder, reporting there) belong to "
                              "PROJECT; the deepest folder wins (repeatable). A job no folder holds is skipped unless "
                              "--project is given")
    observe.add_argument("--url", default="https://cloud.qajev.com", help="Cloud control-plane origin")
    observe.add_argument("--credential-file", type=Path, help="operator-placed machine credential file, mode 0600")
    observe.add_argument("--inventory", action="store_true", help="snapshot backlog locally and print counts, "
                                                                "without a credential or network")
    observe.add_argument("--once", action="store_true", help="one inventory/upload cycle, then exit")
    observe.add_argument("--every", type=int, default=60, help="seconds between inventory/upload cycles, minimum 5")
    observe.add_argument("--max-outbox-mib", type=int, default=512,
                         help="total outbox byte budget in MiB, including database and journal headroom (default 512)")

    check = sub.add_parser("check", help="one scenario from flags: a goal and/or expectations on a URL")
    check.add_argument("url")
    check.add_argument("--goal", "-g", help="what Jev should do, in plain words; end with 'Stop when ...'")
    check.add_argument("--expect-text", "-t", action="append", default=[], help="page must show this (repeatable)")
    check.add_argument("--absent", "-a", action="append", default=[], help="page must not show this (repeatable)")
    check.add_argument("--visible", action="append", default=[],
                       help="this text must be on screen (in the viewport), whole and on top: not covered or cut "
                            "short (a failure says which); repeatable")
    check.add_argument("--expect-url", "-u", help="final URL must contain this")
    check.add_argument("--expect-url-regex", help="final URL must match this regex")
    check.add_argument("--expect-js", "-j", help="JS expression that must return exactly true (may use await); a "
                                                  "string it returns fails, as the reason")
    check.add_argument("--says", help="what the --expect-js check proves, in plain words (the report's test plan); "
                                      "without it the check is NOT DESCRIBED and the gate cannot be PASS")
    check.add_argument("--expect-looks", action="append", default=[], metavar="STATEMENT",
                       help="judged from the final screenshot, e.g. 'the Sign up button is not cut off' (repeatable; "
                            "needs Clef)")
    check.add_argument("--vision", action="store_true",
                       help="Clef sees the screenshot with every decision (needs Clef)")
    check.add_argument("--fetch", action="append", default=[], metavar="URL[=STATUS]",
                       help="in-page GET that must answer STATUS (default 200); repeatable")
    check.add_argument("--mode", choices=["readonly", "mutate"], default="readonly",
                       help="mutate: writes go out, on a local dev host only (localhost, 127.0.0.1, [::1], "
                            "*.localhost, *.test); any other host is refused (exit 5)")
    check.add_argument("--allow-destructive", action="store_true", help=ALLOW_DESTRUCTIVE_HELP)
    check.add_argument("--device", help="test on this device only: desktop | tall | phone | tablet | WIDTHxHEIGHT "
                                       "(default: every device in --devices)")
    check.add_argument("--persona", help="who Jev is, prepended to the goal")
    check.add_argument("--about", help="what this test proves and why, in plain words (shown with its result)")
    check.add_argument("--expect-fail", nargs="?", const=True, metavar="SAYS",
                       help="a planted fault: the test must fail on the check SAYS names (its --says text or its "
                            "words in the plan; bare: the test's only check). Failing only there gives graded=pass; "
                            "a passing test, or a failure on another check, gives graded=fail (gate FAIL). "
                            "Needs --about")
    check.add_argument("--speech", help="what the fake microphone 'hears' if the page listens")
    check.add_argument("--host", action="append", default=[], help="extra host Jev may visit (repeatable)")
    check.add_argument("--max-actions", type=int, default=20)
    check.add_argument("--max-seconds", type=float, default=90)
    check.add_argument("--settle", type=float, default=10, help="seconds to wait for expectations after Jev stops")
    _common(check)

    run = sub.add_parser("run", help="run a suite file (YAML or JSON)")
    run.add_argument("suite", type=Path, nargs="?", help="a suite file (or use --project)")
    run.add_argument("--only", action="append", default=[],
                     help="run this scenario on every device (plus its dependencies); NAME (phone) runs that copy only")
    run.add_argument("--exact", action="store_true",
                     help="with --only: each name on its own device only, not its other devices' copies")
    proj = run.add_argument_group("project mode")
    proj.add_argument("--project", "-p", help="project name or repo path (.qajev/project.toml)")
    proj.add_argument("--env", "-e", help="project environment (default: the project's default_env)")
    proj.add_argument("--suite", dest="tags", action="append", default=[], metavar="TAG",
                      help="run the objectives with this tag, e.g. --suite core (repeatable; default: all)")
    proj.add_argument("--objective", "-o", help="an ad-hoc goal in plain words, instead of the stored objectives")
    proj.add_argument("--name", dest="names", action="append", default=[], help="run this stored objective")
    proj.add_argument("--url", help="for --objective: where to start (default /)")
    proj.add_argument("--about", help="for --objective: what it proves and why, in plain words")
    proj.add_argument("--expect-text", "-t", action="append", default=[], help="for --objective: page must show")
    proj.add_argument("--absent", "-a", action="append", default=[], help="for --objective: page must not show")
    proj.add_argument("--expect-url", "-u", help="for --objective: final URL must contain")
    proj.add_argument("--expect-js", "-j", help="for --objective: JS expression that must return exactly true")
    proj.add_argument("--says", help="for --objective: what the --expect-js check proves, in plain words")
    run.add_argument("--jobs", type=int, default=1, help="parallel workers for independent chains (default 1)")
    run.add_argument("--allow-commands", action="store_true", help="let the suite run shell commands (hooks/checks)")
    run.add_argument("--allow-destructive", action="store_true", help=ALLOW_DESTRUCTIVE_HELP)
    _common(run)

    smoke = sub.add_parser("smoke", help="crawl same-origin pages with no model calls and lint them")
    smoke.add_argument("url", nargs="?", help="start URL (or use --project)")
    smoke.add_argument("--project", "-p", help="crawl a project's environment from its base_url")
    smoke.add_argument("--env", "-e", help="project environment (default: the project's default_env)")
    smoke.add_argument("--max-pages", type=int, default=20)
    smoke.add_argument("--device", help="crawl on this device only (default: every device in --devices)")
    smoke.add_argument("--check-links", action="store_true", help="also HEAD-check discovered links not crawled")
    smoke.add_argument("--delay", type=float,
                       help="seconds between page loads (default 1 for public hosts, 0 for loopback; "
                            "robots.txt crawl-delay wins if larger)")
    _common(smoke)

    play = sub.add_parser("play", help="Native: QA a game or a mobile app through QAJev's bridge (Godot, Electron, "
                                       "iOS Simulator, Android emulator); Jev plays the UI")
    play.add_argument("project", help="a Godot project folder (with project.godot), an Electron .app or project "
                                      "folder, or a mobile target: ios:<bundle id or URL>, android:<package or URL>")
    play.add_argument("--install", help="mobile: an .apk (Android) or .app (iOS simulator) to install on the "
                                        "throwaway device first")
    play.add_argument("--device", help="mobile: the iOS simulator (name or UDID) to clone, or the Android AVD to boot "
                                       "(default: the first iPhone / the first AVD)")
    play.add_argument("--adapter", help="per-game adapter for screens the game draws itself: a bundled name "
                                        "(suho, imhim) or a .gd (Godot) / .js (Electron) path")
    play.add_argument("--suite", type=Path, help="several steps in one game session (YAML): goal steps and "
                                                 "real-time play steps; see docs/games.md")
    play.add_argument("--only", action="append", default=[], metavar="STEP",
                      help="run this step of the --suite (repeatable), plus the steps it names in depends_on and "
                           "every setup: true step")
    play.add_argument("--game-env", action="append", default=[], metavar="KEY=VALUE",
                      help="an environment setting for the game, e.g. SUHO_FORCE_MOBILE=1 (repeatable)")
    play.add_argument("--game-arg", action="append", default=[], metavar="ARG",
                      help="a command-line switch for an Electron app, e.g. --game-arg=--query=autoplay=bot "
                           "(repeatable)")
    play.add_argument("--game-profile", metavar="NAME",
                      help="Electron: keep the game's save folder between runs in ~/.qajev/game-profiles/NAME "
                           "(a test profile, never a real save); default: a throwaway deleted at close")
    play.add_argument("--reset-game-profile", action="store_true",
                      help="empty the kept game profile before this run starts")
    play.add_argument("--goal", "-g", help="what a player wants, in plain words; end with 'Stop when ...'")
    play.add_argument("--about", help="what this test proves and why, in plain words (with --suite: the whole run)")
    play.add_argument("--plan", type=Path, help=PLAN_HELP)
    play.add_argument("--expect-screen", help="the screen the game must be on at the end (from the bridge)")
    play.add_argument("--expect-text", "-t", action="append", default=[], help="the game must show this text")
    play.add_argument("--expect-state", action="append", default=[], metavar="KEY=VALUE",
                      help='a game state value, e.g. game_over=false or kills=">= 1" (repeatable)')
    play.add_argument("--min-fps", type=float, help="the frame rate must be at least this at the end")
    play.add_argument("--allow-errors", action="store_true", help="engine/script errors do not fail the run")
    play.add_argument("--expect-looks", action="append", default=[], metavar="STATEMENT",
                      help="judged from the game's screenshot at the end (repeatable; needs Clef and a window)")
    play.add_argument("--vision", action="store_true", default=None,
                      help="Clef sees the game's screenshot with every decision (the default with Clef and a window; "
                           "this refuses to run without them)")
    play.add_argument("--no-vision", dest="vision", action="store_false",
                      help="decisions are made from the adapter's labels only, no screenshot")
    play.add_argument("--expect-closed", action="store_true",
                      help="the game must quit by itself with exit code 0 (with --allow QUIT, to test a normal quit)")
    play.add_argument("--allow", action="append", default=[], metavar="LABEL",
                      help="offer this exact label to Jev although it is hidden by default, e.g. QUIT (repeatable)")
    play.add_argument("--hide", action="append", default=[], metavar="LABEL",
                      help="never offer this exact label to Jev (repeatable)")
    play.add_argument("--name", help="the scenario's name (default: the project folder's)")
    play.add_argument("--max-actions", type=int, default=20)
    play.add_argument("--max-seconds", type=float, default=90)
    play.add_argument("--headless", action="store_true",
                      help="Godot: no window, fastest, no screenshots (Electron apps always open a window)")
    play.add_argument("--no-shots", action="store_true", help="skip the end screenshot")
    play.add_argument("--out", type=Path, default=Path(os.environ.get("QAJEV_OUT", "qajev-runs")))
    play.add_argument("--env-file")
    play.add_argument("--jev-provider", choices=["auto", "typesafe", "openrouter", "cloudflare"])
    play.add_argument("--cost-cap", type=float, default=1.0)
    play.add_argument("--usd-per-call", type=float, default=0.0005)
    _load_flags(play)
    play.add_argument("--json", action="store_true")
    play.add_argument("--events", action="store_true")
    play.add_argument("--quiet", "-q", action="store_true")
    play.add_argument("--background", action="store_true", help="start as a job and return at once")
    _runner_flags(play, website=False)  # the cloud does not take play runs yet: cloud refuses, auto runs here

    browser = sub.add_parser("browser", help="QAJev's own Chrome: start, stop, status, login")
    browser.add_argument("action", choices=["start", "stop", "status", "login", "reap"])
    browser.add_argument("--profile", default=os.environ.get("QAJEV_PROFILE", "default"))
    browser.add_argument("--headless", action="store_true")
    browser.add_argument("--url", help="for login: the page to sign in on")
    browser.add_argument("--json", action="store_true")

    secret = sub.add_parser("secret", help="check that QAJev can read a seeded test account's password reference "
                                           "(never prints it)")
    secret.add_argument("action", choices=["set", "check"],
                        help="check: can QAJev read a reference; set: refused since 0.4.0 (QAJev types a password only "
                             "for a seeded local test account; sign in to others once with qajev browser login)")
    secret.add_argument("ref", help="a reference, e.g. seed:FILE#KEY (QAJev signs in only with seed: on a local dev "
                                    "host; keychain:, op:// and env: passwords are refused)")

    account = sub.add_parser("account", help="check a project's seeded test account: QAJev signs in with it on a "
                             "local dev host, Jev never sees the password (add is refused since 0.4.0)")
    account.add_argument("action", choices=["add", "check"],
                         help="check: sign in with a project's account; add: refused since 0.4.0 (QAJev saves no "
                              "password; sign in once with qajev browser login)")
    account.add_argument("name", help="the account's name (letters, digits, - and _), e.g. shop-tester")
    account.add_argument("--email", help="add (refused since 0.4.0): the test account's email or user name")
    account.add_argument("--login-url", help="add (refused since 0.4.0): the sign-in page")
    account.add_argument("--password", metavar="REF", help="add (refused since 0.4.0): where the password lives")
    account.add_argument("--project", help="write it into this project (add), or read it from there (check)")
    account.add_argument("--env", help="with --project: the env whose site to sign in to (default: its default)")
    account.add_argument("--default", action="store_true", help="add (refused since 0.4.0): every env signs in with it")
    account.add_argument("--replace", action="store_true", help="add (refused since 0.4.0)")
    account.add_argument("--no-check", action="store_true", help="add (refused since 0.4.0): do not sign in first")
    account.add_argument("--visible", action="store_true", help="sign in in a visible window (default: headless)")
    account.add_argument("--cdp-url", help="sign in in this already running Chrome instead of a throwaway one")

    plan = sub.add_parser("plan", help="print a suite's test plan and the tests that do not say what they prove, "
                                       "without running anything (exit 2 while any is NOT DESCRIBED)")
    plan.add_argument("file", type=Path, help="a suite (scenarios:) or a game's steps suite (steps:)")
    plan.add_argument("--json", action="store_true")
    plan_hash = sub.add_parser("plan-hash", help="print the sha256 an approver puts on a test plan's approval line "
                                                 "(\"Approved by the Orchestrator <d Mon yyyy hh:mm> sha256:...\"); "
                                                 "on a plan that ends with that line, the sha256 of the text above it, "
                                                 "and whether the line holds it (exit 3 when not)")
    plan_hash.add_argument("file", type=Path, help="the test plan (Markdown), before or after its approval line")
    tunnel = sub.add_parser("tunnel", help="print a suite's tunnel: (or a project env's tunnel) as JSON: what the "
                                           "QAJev Cloud dispatcher on this machine checks a run's tunnel against")
    tunnel.add_argument("file", type=Path, help="a suite file, or a project file (.toml) with --env")
    tunnel.add_argument("--env", help="the project env (default: the project's default env)")

    mode = sub.add_parser("mode", aliases=["runner"],
                          help="show where runs go (this Mac or QAJev Cloud), or switch it: local, cloud, auto, or "
                               "dashboard (follow the dashboard's Settings page); --project for one project")
    mode.add_argument("where", nargs="?", choices=["local", "cloud", "auto", "dashboard", "default"],
                      help="local: this Mac; cloud: always the cloud; auto: the cloud when it can, else here; "
                           "dashboard: whatever the dashboard's \"Where runs go by default\" says; default (with "
                           "--project): the project follows this Mac's mode again")
    mode.add_argument("--project", help="set this project only (a name or a path): its runs go there whatever this "
                                        "Mac's mode is")
    mode.add_argument("--json", action="store_true")

    doctor = sub.add_parser("doctor", help="check keys and where each came from, Chrome, ports and load, QAJev Cloud, "
                                           "and print the production rule (never prints secrets)")
    doctor.add_argument("--env-file")
    doctor.add_argument("--offline", action="store_true",
                        help="skip the free OpenRouter key validity check and the QAJev Cloud check")
    doctor.add_argument("--json", action="store_true")

    rep = sub.add_parser("report", help="print a run's report (Markdown, or JSON with --json); a cloud run's id "
                                        "(r_...) fetches its report and screenshots into ~/.qajev/cloud-reports")
    rep.add_argument("run_dir", type=Path, help="a run folder, or a cloud run's id (r_...)")
    rep.add_argument("--json", action="store_true")
    rep.add_argument("--html", action="store_true",
                     help="(re)write report.html from report.json and print its path (e.g. for older runs)")

    projects = sub.add_parser("projects", help="list known projects and where their reports go")
    projects.add_argument("--json", action="store_true")

    reports = sub.add_parser("reports", help="recent project runs from the cross-project index")
    reports.add_argument("--project", "-p")
    reports.add_argument("--limit", type=int, default=20)
    reports.add_argument("--json", action="store_true")

    jobs = sub.add_parser("jobs", help="QAJev runs on this machine: queued, running and recent (or one job in detail)")
    jobs.add_argument("job", nargs="?", help="a job id: show its progress, finished scenarios and result (a cloud "
                                             "run's id, r_..., asks QAJev Cloud)")
    jobs.add_argument("--limit", type=int, default=20)
    jobs.add_argument("--json", action="store_true")

    stop = sub.add_parser("stop", help="stop a job (it closes its browser and keeps the scenarios that finished)")
    stop.add_argument("job", help="a job id, or a cloud run's id (r_...)")
    stop.add_argument("--json", action="store_true")

    nightly = sub.add_parser("nightly", help="each project's core objectives + a smoke crawl, compared with the "
                                             "previous run; notifies only on change")
    nightly.add_argument("--project", "-p", dest="projects", action="append", default=[],
                         help="only this project (repeatable; default: all)")
    nightly.add_argument("--max-pages", type=int, default=10, help="smoke crawl size per project (default 10)")
    nightly.add_argument("--no-notify", action="store_true", help="never post a notification")
    nightly.add_argument("--plan", action="store_true", help="print what would run, and exit")
    nightly.add_argument("--last", action="store_true", help="print the latest digest, and exit")
    nightly.add_argument("--install", action="store_true", help="schedule it every night with launchd")
    nightly.add_argument("--at", default="03:30", help="with --install: the time, HH:MM (default 03:30)")
    nightly.add_argument("--uninstall", action="store_true", help="remove the schedule")
    nightly.add_argument("--json", action="store_true")

    rerun = sub.add_parser("rerun", help="run a finished job again: the same command, or only the tests that "
                                         "did not pass (--failed)")
    rerun.add_argument("job", help="the job id (qajev jobs lists them)")
    rerun.add_argument("--failed", action="store_true",
                       help="only the tests that failed, got stuck or hit a harness limit (with what they depend on)")
    # Its own name: main() backgrounds any command whose args.background is set, and this one only rebuilds the run
    rerun.add_argument("--background", dest="rerun_background", action="store_true",
                       help="start it as a job and return its id")
    rerun.add_argument("--json", action="store_true", help="print the report as JSON")
    rerun.add_argument("--quiet", "-q", action="store_true", help="no progress on stderr")

    top = sub.add_parser("top", help="live dashboard: the browser, queued and running jobs, Chromes, recent reports")
    top.add_argument("--once", action="store_true", help="print one snapshot as text and exit")
    top.add_argument("--json", action="store_true", help="print one snapshot as JSON and exit")
    top.add_argument("--decisions", metavar="JOB",
                     help="a job's decisions as they are made: what the model chose, how sure, the runner-up "
                          "(the `d` view; with --once or --json, print them and exit)")

    dash = sub.add_parser("dashboard", help="every run in a local web page: filter by project, open each test with "
                                            "its screenshots and decisions, stop, rerun or start runs")
    dash.add_argument("--port", type=int, default=8790, help="first port to try on 127.0.0.1 (default 8790)")
    dash.add_argument("--open", action="store_true", help="open it in the browser")
    dash.add_argument("--json", action="store_true", help="print the address as JSON")
    dash.add_argument("--background", dest="dash_background", action="store_true",
                      help="run it detached (it outlives this terminal); print its address and return")
    dash.add_argument("--stop", action="store_true", help="stop the dashboard running on this machine")
    dash.add_argument("--new-key", action="store_true",
                      help="start with a new key: every address printed before stops working (the key is otherwise "
                           "kept across restarts, so an open tab keeps working)")

    init = sub.add_parser("init", help="write a starter suite file")
    init.add_argument("path", type=Path, nargs="?", default=Path("qajev.yaml"))

    mcp = sub.add_parser("mcp", help="serve QAJev as an MCP server over stdio")
    mcp.add_argument("--allow-commands", action="store_true",
                     help="let MCP clients run suites with shell hooks (off by default)")
    return parser


# ----------------------------------------------------------------------------------------------------------------


def _safe(write):
    """Progress is best effort: a closed stderr (a reader that went away) must never kill the run and its cleanup."""
    broken = False

    def emit(event):
        nonlocal broken
        if broken:
            return
        try:
            write(event)
        except (BrokenPipeError, OSError, ValueError):
            broken = True

    return emit


_job_dir = None  # this process's job folder while a check/run/smoke runs in the foreground
_rerun = None  # {"of": job id, "failed": bool} while `qajev rerun` starts its run: the new job records it


def _printer(args):
    from .config import redact_tree

    screen = _screen_printer(args)
    if _job_dir is None:  # a value read from a vault or a seed (a test token) never reaches the screen or the log
        return screen and (lambda event: screen(redact_tree(event)))
    path = _job_dir / "events.jsonl"

    def emit(event):
        event = redact_tree(event)
        try:
            with open(path, "a") as handle:
                handle.write(json.dumps(event, default=str) + "\n")
        except OSError:
            pass
        if screen:
            screen(event)

    return emit


def _screen_printer(args):
    if args.events:
        return _safe(lambda event: print(json.dumps(event, default=str), file=sys.stderr, flush=True))
    if args.quiet:
        return None
    marks = {"pass": "✓", "fail": "✗", "stuck": "…", "harness": "!", "unverified": "?", "skipped": "-"}

    def emit(event):
        kind = event["event"]
        if kind == "run":
            b = event["browser"]
            where = (f"app {b.get('project')}" if b.get("engine") == "app"
                     else f"{b.get('engine')} game {b.get('project')}" if b.get("surface") == "native"
                     else "a fresh Chrome for each test" if b.get("fresh") else f"Chrome {b.get('cdp_url')}")
            print(f"qajev: {event['suite']} -> {event['run_dir']} ({where})", file=sys.stderr)
        elif kind == "waiting":
            print(f"  waiting: {event['reason']}", file=sys.stderr, flush=True)
        elif kind == "admitted":
            held = (f", {event['timing']['hold_seconds']:.0f}s of it for another job's hold"
                    if event["timing"].get("hold_seconds") else "")
            print(f"  browser slot ready: queued {event['timing']['queue_seconds']:.1f}s{held} "
                  f"(capacity {event['capacity']})", file=sys.stderr, flush=True)
        elif kind == "start":
            print(f"  ▸ {event['scenario']}", file=sys.stderr, flush=True)
        elif kind == "step" and not event.get("pulse"):
            p = f" (p {event['p']:.2f})" if isinstance(event.get("p"), (int, float)) else ""
            spent = f" · ${event['spent_usd']:.4f}" if isinstance(event.get("spent_usd"), (int, float)) else ""
            print(f"      Jev: {event['doing']}{p}{spent}", file=sys.stderr, flush=True)
        elif kind == "scenario":
            from .report_html import ux_summary

            r = event["result"]
            actions = (r.get("jev") or {}).get("actions")
            extra = f", {actions} actions" if actions is not None else ""
            ux = ux_summary(r).partition(" · ")[2]
            print(f"  {marks.get(r['outcome'], '?')} {r['outcome']:<10} {r['name']} ({r.get('seconds', 0)}s{extra})"
                  f" {r.get('reason', '')}" + (f" · {ux}" if ux else ""), file=sys.stderr, flush=True)
        elif kind == "reaped":
            print(f"  cleaned up after a dead run: {', '.join(event['items'])}", file=sys.stderr, flush=True)
        elif kind == "signin" and "ok" in event:
            said = (f"signed in as {event.get('email') or event['account']}"
                    + (" (already)" if event.get("already") else f" ({event.get('seconds')}s)")
                    if event["ok"] else f"sign-in failed: {event.get('reason')}")
            print(f"  {'✓' if event['ok'] else '!'} {said}", file=sys.stderr, flush=True)
        elif kind == "signin":
            print(f"  ▸ signing in: {event['account']}", file=sys.stderr, flush=True)
    return _safe(emit)


def _options(args):
    from . import ux_options
    from .runner import Options

    return Options(
        cdp_url=args.cdp_url, profile=args.profile, headless=args.headless, ephemeral=args.ephemeral,
        out_dir=args.out, jobs=getattr(args, "jobs", 1), strict=args.strict,
        allow_commands=getattr(args, "allow_commands", False), typesafe_usd_per_call=args.usd_per_call,
        cost_cap_usd=args.cost_cap, load_high=args.load_high or None, load_ok=args.load_ok,
        load_wait=args.load_wait, only=getattr(args, "only", []), only_exact=getattr(args, "exact", False),
        shots=not args.no_shots, emit=_printer(args),
        **ux_options.parse(args),
        motion=args.motion, cpu_throttle=args.cpu_throttle, color_scheme=getattr(args, "color_scheme", None),
    )


def _record(data):
    if _job_dir is not None:
        try:
            (_job_dir / "result.json").write_text(json.dumps(data, default=str))
        except OSError:
            pass


def _finish(args, report):
    _record(report)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    elif not args.quiet:
        c = report["counts"]
        cost = report["cost"]
        stopped = " (interrupted)" if report.get("interrupted") else ""
        print(f"\nGate: {report['gate']}{stopped}  ({c['pass']} pass, {c['fail']} fail, {c['stuck']} stuck, "
              f"{c['harness']} harness, {c['unverified']} unverified, {c['skipped']} skipped; "
              f"{len(report['findings'])} finding(s); ${cost['usd']:.4f})", file=sys.stderr)
        for r in report["scenarios"]:
            if r.get("expected"):  # a planted fault: how the gate counted it
                print(f"  expected to fail: {r['name']}: {r['graded_reason']}", file=sys.stderr)
        print(f"Report: {Path(report['run_dir']) / 'report.html'} (also report.md, report.json)", file=sys.stderr)
    if report.get("interrupted"):
        return EXIT_INTERRUPTED
    return report["exit_code"]


def check_name(url, goal=None):
    """An ad-hoc check's test name: its site and path, plus the goal's intention. The cloud's Needs a look groups
    failures by project and test, so each check needs its own name, not "check" (the Orchestrator, 13 Oct)."""
    from urllib.parse import urlsplit

    from .jobs import _goal, plain_host
    parts = urlsplit(url if "://" in url else f"https://{url}")
    host = plain_host(parts, url).removeprefix("www.")
    path = parts.path.rstrip("/")
    name = f"{host} {path[:30]}…" if len(path) > 30 else f"{host} {path}" if path else host
    return name + (f" · {_goal(goal)}" if goal else "")


def check_suite(args):
    """Build a one-scenario suite from `qajev check` flags."""
    from . import suite as suite_mod

    expect = {}
    if args.expect_text:
        expect["text"] = args.expect_text
    if args.absent:
        expect["absent"] = args.absent
    if args.visible:
        expect["visible"] = args.visible
    if args.expect_url:
        expect["url"] = args.expect_url
    if args.expect_url_regex:
        expect["url_regex"] = args.expect_url_regex
    if args.expect_js:
        expect["js"] = args.expect_js
        if args.says:
            expect["says"] = {"js": args.says}
    if args.expect_looks:
        expect["looks"] = args.expect_looks
    if args.fetch:
        probes = []
        for item in args.fetch:
            url, _, status = item.partition("=")
            probes.append({"url": url, "status": int(status or 200)})
        expect["fetch"] = probes
    scenario = {"name": check_name(args.url, args.goal), "url": args.url, "expect": expect, "mode": args.mode,
                "device": args.device, "settle": args.settle,
                "budget": {"actions": args.max_actions, "seconds": args.max_seconds}}
    if args.goal:
        scenario["goal"] = args.goal
    if args.persona:
        scenario["persona"] = args.persona
    if args.about:
        scenario["about"] = args.about
    if args.expect_fail:
        scenario["expect_fail"] = args.expect_fail
    if args.speech:
        scenario["speech"] = args.speech
    if args.vision:
        scenario["vision"] = True
    if not args.device:
        del scenario["device"]
    data = {"name": f"check {args.url}", "scenarios": [scenario], "hosts": args.host}
    if args.allow_destructive:
        data["allow_destructive"] = True
    if args.devices:
        data["devices"] = args.devices
    return suite_mod.parse(data)


def _machine_lock(args, suite=None):
    """Browser runs share the pool; external Chrome, real devices and desktop work stay exclusive."""
    from . import browser_pool, jobs
    from .runner import ConfigError

    capacity = None
    if args.command in {"run", "check", "smoke"}:
        try:
            capacity = browser_pool.limit(getattr(args, "browser_runs", None))
        except ValueError as error:
            raise ConfigError(str(error)) from None
        devices = getattr(suite, "real_devices", getattr(args, "real_devices", None))
        if isinstance(devices, str):
            devices = bool(devices.strip()) and devices.strip().lower() != "off"
        if getattr(args, "cdp_url", None) or devices:
            capacity = 1
    return jobs.machine_lock(jobs.describe_argv(args.argv), emit=_printer(args), browser_runs=capacity,
                             job=_job_dir.name if _job_dir else None)


def _wait_for_quiet(args):
    """smoke and play open one browser, emulator or game for the whole run, so they wait for the load gate once,
    up front (check and run wait before each scenario). Raises Busy when the load stays high for --load-wait."""
    from .jobs import Busy
    from .runner import Options, wait_for_quiet

    ok, load = wait_for_quiet(Options(load_high=args.load_high or None, load_ok=args.load_ok,
                                      load_wait=args.load_wait, emit=_printer(args)))
    if not ok:
        raise Busy(f"the machine stayed busy (load1 {load:.0f}, waiting for < {args.load_ok:.0f}) for "
                   f"{args.load_wait:.0f}s; nothing was started")


def _test_plan(args, named, base):
    """The approved test plan for this run (planfile.py): --plan, else the suite's `plan:`. Not approved: a warning, or
    with QAJEV_REQUIRE_PLAN=1 a SuiteError (exit 3) before the queue and Chrome. -> the plan's check"""
    from . import planfile
    from .suite import SuiteError

    flag = getattr(args, "plan", None)
    path = flag.expanduser() if flag else planfile.resolve(named, base) if named else None
    status = planfile.check(path)
    if not status["approved"]:
        where = f" ({status['path']})" if status["path"] else ""
        if planfile.required():
            raise SuiteError(f"needs an approved test plan: {status['problem']}{where}")
        print(f"qajev: warning: no approved test plan: {status['problem']}{where}", file=sys.stderr)
    return status


def cmd_runner(args):
    """`qajev mode`: where runs go now, and why. With a value, this Mac's mode (QAJEV_RUNNER in ~/.qajev/.env) or
    one project's `runner` (--project) changes first."""
    from types import SimpleNamespace

    from . import cloud, config

    if args.project:
        from . import project as project_mod

        if args.where == "dashboard":
            return _fail(args, "a project takes local, cloud, auto or default; dashboard is for this Mac's mode",
                         EXIT_CONFIG)
        try:
            proj = project_mod.set_runner(args.project, args.where) if args.where else project_mod.load(args.project)
        except project_mod.ProjectError as e:
            return _fail(args, str(e), EXIT_CONFIG)
    elif args.where == "default":
        return _fail(args, "default is for a project (--project NAME): this Mac's mode is local, cloud, auto or "
                           "dashboard", EXIT_CONFIG)
    elif args.where:
        config.set_value(config.HOME / ".env", "QAJEV_RUNNER", args.where)
        cloud._dashboard_cache.clear()
    try:
        runner, source = cloud.runner_for(SimpleNamespace(runner=None, env_file=None, command="run",
                                                          project=args.project and str(proj.config_path)))
    except cloud.CloudError as e:
        return _fail(args, str(e), EXIT_CONFIG)
    setting = (cloud.setting("QAJEV_RUNNER")[0] or "local").strip().lower()
    shell = os.environ.get("QAJEV_RUNNER")
    note = (f"the shell sets QAJEV_RUNNER={shell}, which wins over ~/.qajev/.env: unset it to use {args.where}"
            if args.where and shell and shell.strip().lower() != args.where else None)
    if args.json:
        print(json.dumps({"runner": runner, "setting": setting, "source": source, "dashboard": cloud.dashboard_url(),
                          **({"note": note} if note else {})}))
        return 0
    whose = f"{proj.name}'s runs go to" if args.project else "Runs go to"
    print(f"{whose}: {'this Mac' if runner == 'local' else runner} ({source})")
    if setting == "dashboard":
        print(f"Change it on the dashboard: {cloud.dashboard_url()}/settings.html, \"Where runs go by default\".")
    print("Switch: qajev mode local | cloud | auto | dashboard, or --project NAME for one project. "
          "A run can still say --runner.")
    if note:
        print(f"qajev: warning: {note}", file=sys.stderr)
    return 0


def cmd_plan_hash(args):
    """The sha256 for a test plan's approval line (planfile.digest). A plan that ends with its approval line gives the
    sha256 of the text above that line, and the line's own sha256 is checked: an approved plan gives its approved hash
    (the Orchestrator, 30 Oct: an approved plan printed the hash of the plan and its approval line together)."""
    from . import planfile

    text, problem = planfile.read(args.file)
    if problem:
        return _fail(args, f"{args.file}: {problem}", EXIT_CONFIG)
    status = planfile.check(args.file)
    if not status["approval"]:
        print(f"sha256:{planfile.digest(text)}")
        return 0
    if status["problem"] == planfile.TEXT_AFTER:  # added after the approval: a new line at the end covers it all
        print(f"sha256:{planfile.digest(text)}")
        print(f"{args.file}: {planfile.words(status)}; this sha256 is for a new approval line at its end", file=sys.stderr)
        return 0
    print(f"sha256:{status['sha256']}")
    print(f"{args.file}: {planfile.words(status)}", file=sys.stderr)
    return 0 if status["approved"] else EXIT_CONFIG


def cmd_tunnel(args):
    """The tunnel a suite or project env declares, as the cloud plan sends it (without root and writes). The QAJev Cloud
    dispatcher reads the file on disk with this and refuses a run whose tunnel differs (redteam, 12 Oct)."""
    from . import project as project_mod
    from . import suite as suite_mod

    try:
        if args.file.suffix == ".toml":
            proj = project_mod.load(str(args.file))
            env = args.env or proj.default_env
            if env not in proj.envs:
                return _fail(args, f"{args.file}: no env {env!r}; have {sorted(proj.envs)}", EXIT_CONFIG)
            spec = proj.envs[env].get("tunnel")
        else:
            spec = suite_mod.load(args.file).tunnel
    except (project_mod.ProjectError, suite_mod.SuiteError, ValueError, OSError) as e:
        return _fail(args, f"{args.file}: {e}", EXIT_CONFIG)
    if not spec:
        return _fail(args, f"{args.file}: no tunnel", EXIT_CONFIG)
    print(json.dumps(spec, sort_keys=True))
    return 0


def cmd_run(args):
    from .chrome import ChromeError
    from .config import load_env
    from .jobs import Busy
    from .project import ProjectError
    from .runner import ConfigError, run
    from .suite import Refused, SuiteError, load

    _lower_priority()
    _provider_flag(args)
    try:
        load_env(args.env_file)
        if args.command == "run" and getattr(args, "project", None):
            proj, env, suite = _project_suite(args)  # refused before the queue and before Chrome
            suite.test_plan = _test_plan(args, suite.plan, proj.config_path.parent)
            with _machine_lock(args, suite):
                return _run_project(args, proj, env, suite)
        if args.command == "run" and not args.suite:
            return _fail(args, "give a suite file or --project", EXIT_CONFIG)
        suite = check_suite(args) if args.command == "check" else _destructive(load(args.suite, devices=args.devices),
                                                                                 args)
        if getattr(args, "real_devices", None):
            from .suite import _real_devices

            suite.real_devices = _real_devices(args.real_devices)
        suite.test_plan = _test_plan(args, suite.plan, suite.path.parent if suite.path else Path.cwd())
        with _machine_lock(args, suite):
            report = run(suite, _options(args))
    except Refused as e:
        return _refused(args, str(e))
    except (SuiteError, ConfigError, FileNotFoundError, ProjectError, ValueError) as e:
        return _fail(args, str(e), EXIT_CONFIG)
    except (ChromeError, Busy) as e:
        return _fail(args, str(e), EXIT_BROWSER)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    return _finish(args, report)


def _destructive(suite, args):
    """--allow-destructive on a loaded suite, judged again: a production host is refused."""
    from .suite import check_safety

    if getattr(args, "allow_destructive", False):
        suite.guard = {**suite.guard, "allow_destructive": True}
        check_safety(suite)
    return suite


def _project_suite(args):
    """A project's objectives (or one ad-hoc objective) as a suite. -> (project, env name, suite)"""
    from . import project as project_mod
    from .suite import parse

    proj = project_mod.load(args.project)
    expect = {}
    for key, value in (("text", args.expect_text), ("absent", args.absent), ("url", args.expect_url),
                       ("js", args.expect_js)):
        if value:
            expect[key] = value
    if args.says and args.expect_js:
        expect["says"] = {"js": args.says}
    if (expect or args.about) and not args.objective:
        raise project_mod.ProjectError("--expect-* and --about go with --objective (stored objectives carry their own)")
    env = args.env or proj.default_env
    data = project_mod.suite_data(proj, env=env, tags=args.tags, names=args.names, objective=args.objective,
                                  url=args.url, expect=expect, about=args.about)
    suite = _destructive(parse(data, proj.config_path, devices=args.devices), args)
    if getattr(args, "real_devices", None):
        from .suite import _real_devices

        suite.real_devices = _real_devices(args.real_devices)
    return proj, env, suite


def _run_project(args, proj, env, suite):
    """Run a project's suite; the report is filed with the project."""
    from . import project as project_mod
    from .runner import run

    opts = _options(args)
    opts.out_dir = project_mod.run_dir_parent(proj)
    report = run(suite, opts)
    report["project"] = {"name": proj.name, "env": env, "config": str(proj.config_path)}
    _file_known(proj, report)
    _file_changes(proj, report, env)
    row = project_mod.record(proj, report, env=env)
    report["index"] = row
    return _finish(args, report)


def _file_known(proj, report):
    """Move the owner's known issues out of the findings, then rewrite the report files with the result."""
    from . import project as project_mod
    from . import report as report_mod

    if proj.known:
        project_mod.apply_known(proj, report)
        report_mod.write(Path(report["run_dir"]), report)


def _file_changes(proj, report, env):
    """Compare with the previous finished run of this project, env and kind; rewrite the report with the result."""
    from . import changes
    from . import project as project_mod
    from . import report as report_mod

    found = changes.compare(proj.name, env, report, project_mod.index_rows(proj.name, 50))
    if found is not None:
        report["changes"] = found
        report_mod.write(Path(report["run_dir"]), report)


def cmd_projects(args):
    from . import project as project_mod

    items = project_mod.listing()
    if args.json:
        print(json.dumps(items, indent=2))
        return 0
    if not items:
        print("no projects; add <repo>/.qajev/project.toml or ~/.qajev/projects/<name>.toml")
    for p in items:
        if p.get("error"):
            print(f"{p['name']:<14} ERROR {p['error']}")
            continue
        envs = ", ".join(f"{k}={v}" for k, v in p["envs"].items())
        print(f"{p['name']:<14} {p['objectives']} objective(s)  {envs}\n{'':<14} reports -> {p['reports']}")
    return 0


def cmd_reports(args):
    from . import project as project_mod

    rows = project_mod.index_rows(args.project, args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no project runs recorded yet")
    for r in rows:
        outcomes = ", ".join(f"{o['name']}: {o['outcome']}" for o in r["objectives"])
        print(f"{r['when'][:16]}  {r['project']:<12} {r['env']:<6} {r['gate']:<10} ${r['cost_usd']:.4f}  "
              f"{r['seconds']:.0f}s  {outcomes}\n{'':<18}{r['report']}")
    return 0


def cmd_smoke(args):
    from . import smoke
    from .chrome import ChromeError
    from .config import load_env
    from .jobs import Busy
    from .runner import ConfigError

    _lower_priority()
    _provider_flag(args)
    try:
        load_env(args.env_file)
        opts, url, proj, env = _options(args), args.url, None, None
        if args.project:
            from . import project as project_mod

            proj = project_mod.load(args.project)
            env = args.env or proj.default_env
            if env not in proj.envs:
                return _fail(args, f"{proj.name}: no env {env!r}", EXIT_CONFIG)
            if not url:
                from urllib.parse import urljoin

                target = proj.envs[env]
                url = urljoin(target["base_url"], target.get("smoke_start", "/"))
            opts.out_dir = project_mod.run_dir_parent(proj)
        if not url:
            return _fail(args, "give a URL or --project", EXIT_CONFIG)
        test_plan = _test_plan(args, None, Path.cwd())
        with _machine_lock(args):
            _wait_for_quiet(args)
            report = smoke.run(url, opts, max_pages=args.max_pages, device=args.device, devices=args.devices,
                               check_links=args.check_links, delay=args.delay)
        report["test_plan"] = test_plan
        from . import report as report_mod

        report_mod.write(Path(report["run_dir"]), report)
        if proj:
            report["project"] = {"name": proj.name, "env": env, "config": str(proj.config_path)}
            _file_known(proj, report)
            _file_changes(proj, report, env)
            report["index"] = project_mod.record(proj, report, env=env)
    except (ConfigError, FileNotFoundError, ValueError) as e:
        return _fail(args, str(e), EXIT_CONFIG)
    except (ChromeError, Busy) as e:
        return _fail(args, str(e), EXIT_BROWSER)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    return _finish(args, report)


def _state_value(text):
    key, _, raw = text.partition("=")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw  # e.g. ">= 1", or a plain word
    return key.strip(), value


def cmd_play(args):
    import time
    from types import SimpleNamespace

    from . import electron, live, mobile, native, providers
    from . import report as report_mod
    from .config import load_env, redact_tree, secret_values
    from .jobs import Busy
    from .ledger import Ledger
    from .runner import slug

    _lower_priority()
    _provider_flag(args)
    try:
        load_env(args.env_file)
    except FileNotFoundError as e:
        return _fail(args, str(e), EXIT_CONFIG)
    expect = {}
    if args.expect_screen:
        expect["screen"] = args.expect_screen
    if args.expect_text:
        expect["text"] = args.expect_text
    if args.expect_state:
        expect["state"] = dict(_state_value(t) for t in args.expect_state)
    if args.min_fps is not None:
        expect["min_fps"] = args.min_fps
    if args.expect_closed:
        expect["closed"] = True
    if args.expect_looks:
        expect["looks"] = args.expect_looks
    if expect:
        expect["no_errors"] = not args.allow_errors
    if args.goal:
        try:
            if providers.describe(providers.resolve())["jev"] == "none":
                return _fail(args, "a goal needs a decision model: TYPESAFE_API_KEY or an OpenRouter key (Jev), "
                             "or QAJEV_JEV_PROVIDER=cloudflare with CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN "
                             "(Clef); see `qajev doctor`", EXIT_CONFIG)
        except providers.ProviderError as e:
            return _fail(args, str(e), EXIT_CONFIG)
    session_steps, game_env, adapter, headless, asked = None, {}, args.adapter, args.headless, args.vision
    run_about = None  # with a suite, --about (or its about:) is the whole run's; without, the one test's
    game_args = []
    spec = {}
    if args.suite:
        import yaml

        try:
            spec = yaml.safe_load(args.suite.read_text()) or {}
        except (OSError, yaml.YAMLError) as e:
            return _fail(args, f"{args.suite}: {e}", EXIT_CONFIG)
        session_steps = spec.get("steps") or []
        if not session_steps:
            return _fail(args, f"{args.suite}: no steps", EXIT_CONFIG)
        # A game or an app has no in-page write guard: QAJev plays it read-only, and a run asking to change data is
        # refused (0.4.0: Godot and native mobile apps never; an Electron app waits for a local `backend:`).
        if spec.get("mode") == "mutate" or any(isinstance(s, dict) and s.get("mode") == "mutate"
                                               for s in session_steps):
            return _refused(args, f"{args.suite}: QAJev never changes data through a game or an app: play runs are "
                                  "read-only, so mode: mutate is refused.")
        from .suite import SuiteError, about

        try:
            run_about = args.about or about(spec.get("about"), "about")
            for i, step in enumerate(session_steps):
                if isinstance(step, dict) and step.get("about") is not None:
                    step["about"] = about(step["about"], f"steps[{i}].about")
        except SuiteError as e:
            return _fail(args, f"{args.suite}: {e}", EXIT_CONFIG)
        try:  # judged_by names later steps of the whole suite, before --only picks some
            native.check_judged_by(session_steps)
            native.check_window(session_steps)
            native.check_pad(session_steps)
            native.check_looks(session_steps, args.suite.parent)
            from .handoff import check_handoff
            from .mackeys import check_keys
            from .openurl import check_open_url

            check_handoff(session_steps)
            check_keys(session_steps, spec.get("app"))
            check_open_url(session_steps)
        except native.NativeError as e:
            return _fail(args, f"{args.suite}: {e}", EXIT_CONFIG)
        if args.only:
            try:
                session_steps = native.select_steps(session_steps, args.only)
            except native.NativeError as e:
                return _fail(args, f"{args.suite}: {e}", EXIT_CONFIG)
        if spec.get("app") is not None:  # a native app with a QAJev bridge, started from its own command
            from . import vault
            from .app import app_files, app_spec

            try:
                if isinstance(spec["app"], dict) and isinstance(spec["app"].get("files"), dict):
                    spec["app"]["files"] = {k: vault.anchor(v, args.suite.parent) if isinstance(v, str) else v
                                            for k, v in spec["app"]["files"].items()}
                app_files(app_spec(spec["app"]))  # each fixture checked now, before the queue and the app
            except (native.NativeError, vault.VaultError) as e:
                return _fail(args, f"{args.suite}: {e}", EXIT_CONFIG)
            godot_only = [k for k in ("adapter", "seed", "headless", "game_profile") if spec.get(k)]
            if godot_only or args.adapter or args.headless:
                return _fail(args, f"{args.suite}: app: starts the app from its own command; "
                                   f"{', '.join(godot_only) or '--adapter/--headless'} is for a Godot or Electron game",
                             EXIT_CONFIG)
        game_env.update({str(k): str(v) for k, v in (spec.get("env") or {}).items()})
        game_args += [str(a) for a in spec.get("args") or []]
        adapter = adapter or spec.get("adapter")
        headless = headless or bool(spec.get("headless"))
        if asked is None and spec.get("vision") is not None:
            asked = bool(spec["vision"])
        if spec.get("seed") and (mobile.is_mobile(args.project) or electron.is_electron(args.project)):
            return _fail(args, f"{args.suite}: seed is for Godot games (saves copied into user://)", EXIT_CONFIG)
        if spec.get("seed"):
            try:
                native.seed_folder(Path(args.project).expanduser(), spec["seed"])
            except native.NativeError as e:
                return _fail(args, f"{args.suite}: {e}", EXIT_CONFIG)
    elif args.only:
        return _fail(args, "--only picks steps of a session: it needs --suite", EXIT_CONFIG)
    from .suite import SuiteError

    try:
        test_plan = _test_plan(args, spec.get("plan") if args.suite else None,
                               args.suite.parent if args.suite else Path.cwd())
    except SuiteError as e:
        return _fail(args, str(e), EXIT_CONFIG)
    from . import vision

    app = spec.get("app") if args.suite else None
    if app is not None and app.get("files"):  # a test token handed to the app: only under a plan that says so
        from . import planfile

        text = planfile.read(test_plan["path"])[0] if test_plan.get("approved") else None
        if not text or "app.files" not in text:
            why = "it does not name app.files" if text else test_plan.get("problem")
            return _fail(args, f"{args.suite}: app.files needs an approved test plan that names app.files ({why})",
                         EXIT_CONFIG)
    is_electron = electron.is_electron(args.project) and app is None  # a suite's app: wins over the .app guess
    # Vision is on by default for a game (José, 3 Oct): with Clef and a window to look at; else off, quietly.
    window = not headless or mobile.is_mobile(args.project) or is_electron or app is not None
    sees, refused = vision.for_play(asked, window=window)
    if refused:
        return _fail(args, refused, EXIT_CONFIG)
    looking = bool(expect.get("looks")) or any(
        s.get("vision") or (s.get("expect") or {}).get("looks") for s in session_steps or [])
    if looking:  # a step's own vision: true, or looks: these were asked for, so they refuse instead of skipping
        refused = vision.require_clef("vision" if any(s.get("vision") for s in session_steps or []) else "looks")
        if refused:
            return _fail(args, refused, EXIT_CONFIG)
        if not window:
            return _fail(args, "vision and looks need the game's window, and a headless Godot game draws none: "
                               "run it without --headless (MCP: headless=false)", EXIT_CONFIG)
    game_env.update(dict(item.split("=", 1) for item in args.game_env if "=" in item))
    game_args += args.game_arg
    # A kept TEST profile (game_profile.py): --game-profile NAME, or the suite's game_profile:; Electron only
    game_profile_name = args.game_profile or (spec.get("game_profile") if args.suite else None)
    reset_profile = args.reset_game_profile or bool(spec.get("reset_game_profile") if args.suite else False)
    if (game_profile_name or reset_profile) and not is_electron:
        return _fail(args, "--game-profile keeps an Electron game's save folder; this game is not Electron (a Godot "
                           "game's saves come from seed:)", EXIT_CONFIG)
    name = args.name or (spec.get("name") if args.suite else None) or (
        args.project if mobile.is_mobile(args.project) else Path(args.project).expanduser().resolve().name)
    emit = _printer(args) or (lambda _event: None)
    ledger = Ledger(args.cost_cap, args.usd_per_call)
    results, browser, interrupted = [], {}, False
    game_cm = None
    started_at = time.time()
    run_dir = args.out / f"{time.strftime('%Y%m%d-%H%M%S')}-play-{slug(name)}"
    try:
        with _machine_lock(args):
            _wait_for_quiet(args)
            reaped = native.reap()
            if reaped:
                emit({"event": "reaped", "items": reaped})
            run_dir.mkdir(parents=True, exist_ok=True)
            if mobile.is_mobile(args.project):  # an app (or the browser) on a QAJev-owned simulator or emulator
                game_cm = mobile.MobileApp(args.project, device=args.device or (spec.get("device") if args.suite
                                                                                 else None),
                                           hide=[*(spec.get("hide") or [] if args.suite else []), *args.hide],
                                           allow=[*(spec.get("allow") or [] if args.suite else []), *args.allow],
                                           install=args.install or (spec.get("install") if args.suite else None))
            elif app is not None:  # any native app with a QAJev bridge (Twinbox Desktop)
                from .app import AppProcess

                game_cm = AppProcess(args.project, app, env=game_env, args=game_args,
                                     hide=[*(spec.get("hide") or []), *args.hide],
                                     allow=[*(spec.get("allow") or []), *args.allow])
            elif is_electron:  # a web game in Electron (I'm Him's desktop build)
                game_cm = electron.ElectronGame(args.project, adapter=adapter, args=game_args, env=game_env,
                                                hide=[*(spec.get("hide") or [] if args.suite else []), *args.hide],
                                                allow=[*(spec.get("allow") or [] if args.suite else []), *args.allow],
                                                profile=game_profile_name, reset_profile=reset_profile)
            else:
                game_cm = native.GodotGame(args.project, adapter=adapter, headless=headless, env=game_env,
                                            hide=[*(spec.get("hide") or [] if args.suite else []), *args.hide],
                                            allow=[*(spec.get("allow") or [] if args.suite else []), *args.allow],
                                            seed=spec.get("seed") if args.suite else None)
            with game_cm as game:
                browser = {"surface": "native", "engine": getattr(game, "engine", "godot"),
                           "project": str(game.project), "adapter": game.adapter.stem if game.adapter else None,
                           "headless": headless, "env": game_env, "args": game_args,
                           "managed": True, "pid": game.proc.pid if game.proc else None, "vision": sees}
                notes = getattr(game, "file_notes", None)
                if notes:  # the file names and their fixtures, never the values
                    browser["app_files"] = notes
                if getattr(game, "record", None) and game.record.get("profile"):  # the save folder, kept or not
                    browser["game_profile"] = game.record["profile"]
                emit({"event": "run", "suite": f"play {name}", "run_dir": str(run_dir), "browser": browser,
                      "scenarios": len(session_steps) if session_steps else 1,
                      "decider": providers.describe(providers.resolve()).get("decider")})
                # Only a headless Godot game renders nothing to capture; Electron always has a real window
                # (its --headless only meant "no shots", so MCP's default headless runs had none).
                shots = not args.no_shots and not (headless and browser["engine"] == "godot")
                with live.frames(run_dir, getattr(game, "page_ws", None)):  # its screen, while someone watches it
                    if session_steps:
                        results = native.run_session(game, session_steps, ledger=ledger, run_dir=run_dir,
                                                     shots=shots, emit=emit, vision=sees,
                                                     looks_folder=args.suite.parent if args.suite else None)
                    else:
                        emit({"event": "start", "scenario": name})
                        result = native.play(game, name=name, goal=args.goal, expect=expect, about=args.about,
                                             budget={"actions": args.max_actions, "seconds": args.max_seconds},
                                             ledger=ledger, run_dir=run_dir, shots=shots, emit=emit, vision=sees)
                        result["cost_usd"] = round(ledger.spent(), 5)
                        emit({"event": "scenario", "result": result})
                        results = [result]
                results[0]["boot_seconds"] = getattr(game, "boot_seconds", None)
    except (native.NativeError, Busy) as e:
        return _fail(args, str(e), EXIT_BROWSER)
    except KeyboardInterrupt:
        interrupted = True
    native.finalize_output(game_cm, results, steps=session_steps)
    built = report_mod.build(SimpleNamespace(name=f"play {name}", about=run_about, test_plan=test_plan), results,
                             [ledger.summary()], browser=browser,
                             started_at=started_at, strict=False, interrupted=interrupted, run_dir=run_dir)
    if args.goal or session_steps:
        built["models"] = providers.describe(providers.resolve())
    built = redact_tree(built, secret_values())
    run_dir.mkdir(parents=True, exist_ok=True)
    report_mod.write(run_dir, built)
    emit({"event": "done", "gate": built["gate"], "run_dir": str(run_dir)})
    return _finish(args, built)


def cmd_browser(args):
    from . import chrome

    try:
        if args.action == "start":
            out = chrome.start(args.profile, headless=args.headless)
        elif args.action == "stop":
            out = {"profile": args.profile, "stopped": chrome.stop(args.profile)}
        elif args.action == "status":
            chrome.reap()
            out = chrome.status()
        elif args.action == "reap":
            from . import native

            out = {"reaped": chrome.reap() + native.reap()}
        else:
            if not args.url:
                return _fail(args, "login needs --url (the page to sign in on)", EXIT_CONFIG)
            running = chrome.status(args.profile)
            if running and running[0].get("headless"):
                return _fail(args, f"profile {args.profile!r} runs headless; stop it first to sign in", EXIT_CONFIG)
            record = chrome.start(args.profile, visible=True)
            tab = chrome.open_visible(record["cdp_url"], args.url)
            out = {**record, "login_tab": tab.get("id"),
                   "next": "Sign in yourself in that window (QAJev never types credentials); the profile keeps it."}
    except chrome.ChromeError as e:
        return _fail(args, str(e), EXIT_BROWSER)
    if args.json:
        print(json.dumps(out, indent=2))
    elif isinstance(out, list):
        if not out:
            print("no QAJev Chrome running")
        for r in out:
            name = f"{r['profile']} (throwaway)" if r.get("ephemeral") else r["profile"]
            print(f"{name:<16} pid {r['pid']:<7} {r['cdp_url']}  tabs={r.get('tabs')}  "
                  f"{'headless' if r['headless'] else 'headed'}  {'alive' if r['alive'] else 'DEAD'}")
    else:
        for key, value in out.items():
            print(f"{key}: {value}")
    return 0


def cmd_secret(args):
    from . import vault

    try:
        if args.action == "set":  # José, 6 Oct: no stored password QAJev types; check still reads a reference
            from .signin import BROWSER_LOGIN

            print(f"qajev: refused: QAJev no longer stores a password. {BROWSER_LOGIN.format(url='<sign-in page>')}",
                  file=sys.stderr)
            return 2
        value = vault.resolve(args.ref)
    except vault.VaultError as e:
        print(f"qajev: {e}", file=sys.stderr)
        return 2
    print(f"ok: {args.ref} can be read ({len(value)} characters)")
    return 0


def cmd_account(args):
    from urllib.parse import urlsplit

    from . import account as account_mod
    from . import project as project_mod
    from . import vault
    from .chrome import ChromeError
    from .jobs import Busy

    name = args.name
    try:
        if not account_mod.NAME.match(name):
            raise account_mod.AccountError("the name takes letters, digits, - and _ (e.g. shop-tester)")
        proj = project_mod.load(args.project) if args.project else None
        if args.action == "add":  # José, 6 Oct: QAJev saves and types no person's password
            from .signin import BROWSER_LOGIN

            raise account_mod.AccountError("refused: QAJev no longer saves an account's password. A seeded local "
                                           "test account goes in the suite or project as password: seed:FILE#KEY. "
                                           + BROWSER_LOGIN.format(url=args.login_url or "<sign-in page>"))
        if args.action == "add":
            if not (args.email and args.login_url):
                raise account_mod.AccountError("add needs --email and --login-url")
            ref = args.password or account_mod.default_ref(name)
            raw = account_mod.block(name, args.email, ref, args.login_url)
            how = account_mod.save_password(ref, replace=args.replace)
            print({"saved": f"password saved in the Keychain as {ref}",
                   "kept": f"{ref} already holds a password: kept (--replace to change it)",
                   "found": f"{ref} can be read"}[how])
            if proj:
                proj = account_mod.add_to_project(proj.config_path, name, args.email, ref, args.login_url,
                                                  default=args.default)
                print(f"added [accounts.{name}] to {proj.config_path} (references only)"
                      + ("" if proj.account == name else f'; use it with account = "{name}" in an env'))
            else:  # JSON strings are valid YAML: the block pastes as is
                print(f"for a suite file:\n\naccount:\n  name: {json.dumps(name)}\n  email: {json.dumps(args.email)}\n"
                      f"  password: {json.dumps(ref)}\n  login: {{url: {json.dumps(args.login_url)}}}\n")
            if args.no_check:
                return 0
            account, hosts = (account_mod.project_account(proj, name, args.env) if proj else
                              (raw, {urlsplit(args.login_url).netloc}))
        else:
            if not proj:
                raise account_mod.AccountError("check needs --project (a suite's account is checked by its run)")
            account, hosts = account_mod.project_account(proj, name, args.env)
        print(f"signing in as {name} at {account['login']['url']} ...")
        args.events, args.quiet, args.json = False, False, False  # the queue's own messages, said plainly
        with _machine_lock(args):
            done = account_mod.try_sign_in(account, hosts, headless=not args.visible, cdp_url=args.cdp_url)
    except (account_mod.AccountError, project_mod.ProjectError, vault.VaultError) as e:
        print(f"qajev: {e}", file=sys.stderr)
        return EXIT_CONFIG
    except (ChromeError, Busy) as e:
        print(f"qajev: {e}", file=sys.stderr)
        return EXIT_BROWSER
    if not done["ok"]:
        print(f"sign-in failed: {done['reason']}", file=sys.stderr)
        return 2
    print(f"ok: signed in as {done.get('email') or name}"
          + (" (already signed in)" if done.get("already") else f" in {done.get('seconds')} s"))
    return 0


def cmd_doctor(args):
    from . import chrome, providers
    from .config import DEFAULTS, LOCAL_DEV_HOSTS, env_files, key_sources, load_env, qajev_commit

    try:
        loaded = load_env(args.env_file)
    except FileNotFoundError as e:
        return _fail(args, str(e), EXIT_CONFIG)
    checks = []

    # A level per check (the 11 Oct new-user test): "ok"; "problem" (NO, exit 3); "info" (--, an optional setting not
    # set); "todo" (--, a step a fresh install has not taken yet: not an error, the Next lines say what to do).
    def add(name, ok, detail, level=None):
        checks.append({"check": name, "ok": ok, "detail": detail, "level": level or ("ok" if ok else "problem")})

    add("qajev", True, f"{__version__} ({qajev_commit() or 'installed package, no checkout'})")
    if loaded:
        add("env files", True, ", ".join(loaded))
    else:
        add("env files", True, f"none yet; QAJev reads {', '.join(str(p) for p in env_files(args.env_file))}", "info")
    for key in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "CLOUDFLARE_API_TOKEN", "TEXT_MODEL_API_KEY"):
        if os.environ.get(key):
            add(key, True, "set")
        else:
            add(key, True, "not set (optional)", "info")
    for line in key_sources():  # where QAJev's own keys came from: names only (Foley1, 6 Oct)
        add("key source", True, line)
    from . import planfile

    add("test plan", True, "required: a run without an approved test plan is refused (QAJEV_REQUIRE_PLAN=1)"
        if planfile.required() else "a run without an approved test plan warns (QAJEV_REQUIRE_PLAN=1 refuses it)")
    try:
        resolved = providers.resolve()
        models = providers.describe(resolved)
        if resolved["jev"]:
            add("jev route", True, f"{models['decider']}: {models['jev']}")
        else:
            add("jev route", True, "none yet: goals need a model key (see Next); checks without a goal run already",
                "todo")
        if resolved["text"]:
            add("text helper", True, models["text"])
        else:
            add("text helper", True, "none: goals that type into fields need TEXT_MODEL_API_KEY or "
                "OPENROUTER_API_KEY", "info")
        used = {resolved["jev_key_name"] if resolved["jev"] == "openrouter" else None,
                resolved["text_key_name"] if "openrouter.ai" in str(resolved["text_base_url"] or "") else None}
        for name in sorted(str(n) for n in used if n and not args.offline):
            status = providers.check_openrouter_key(os.environ[name])
            add(f"{name} valid", status == 200, {200: "accepted by OpenRouter", None: "OpenRouter unreachable"}.get(
                status, f"rejected by OpenRouter (HTTP {status}); a stale key in your shell overrides the env file"))
    except providers.ProviderError as e:
        add("jev route", False, str(e))
    for key in DEFAULTS:
        add(key, True, os.environ.get(key))
    try:
        add("chrome", True, chrome.binary())
    except chrome.ChromeError as e:
        add("chrome", False, str(e))
    try:
        add("free port", True, str(chrome.free_port()))
    except chrome.ChromeError as e:
        add("free port", False, str(e))
    reaped = chrome.reap()
    if reaped:
        add("dead runs cleaned", True, ", ".join(reaped))
    running = chrome.status()
    add("qajev chrome", True, ", ".join(f"{r['profile']}@{r['port']}" for r in running) or "none running")
    try:
        import jev_ultrafast  # noqa: F401

        add("jev-ultrafast", True, "importable")
    except ImportError as e:
        add("jev-ultrafast", False, str(e))
    add("production", True, "read-only, destructive never: only a local dev host may change ("
        + ", ".join(LOCAL_DEV_HOSTS) + "); mutate or --allow-destructive anywhere else is refused (exit 5)")
    from . import cloud

    add("cloud", *cloud.doctor(args.env_file, offline=args.offline))  # never the key, only where it is
    load1 = os.getloadavg()[0]
    add("load average", load1 < 150, f"{load1:.0f} (runs wait while >= 150)")
    ok = all(c["ok"] for c in checks)
    ready = not any(c["level"] == "todo" for c in checks)
    nxt = [] if ready else [
        "Put one model key in ~/.qajev/.env: TYPESAFE_API_KEY or OPENROUTER_API_KEY (for Jev, which reads the page).",
        "Or run your tests in QAJev Cloud instead: qajev mode cloud (it needs a cloud key; the cloud line above says "
        "where QAJev looks for it).",
        "Then run qajev doctor again.",
    ]
    if args.json:
        print(json.dumps({"ok": ok, "ready": ready, "next": nxt, "checks": checks}, indent=2))
    else:
        marks = {"ok": "ok ", "problem": "NO ", "info": "-- ", "todo": "-- "}
        for c in checks:
            print(f"{marks[c['level']]} {c['check']:<22} {c['detail']}")
        if nxt:
            print("\nNext:\n" + "\n".join(f"  {i}. {line}" for i, line in enumerate(nxt, 1)))
    return 0 if ok else EXIT_CONFIG


def _cloud_report(args):
    """`qajev report r_...`: a cloud run's report, fetched with this Mac's key and kept in ~/.qajev/cloud-reports."""
    from . import cloud
    from .config import HOME

    run_id = str(args.run_dir)
    folder = HOME / "cloud-reports" / run_id
    try:
        data, shots = cloud.fetch_report(cloud.connect(getattr(args, "env_file", None)), run_id, folder)
    except json.JSONDecodeError as e:
        return _failure(args, f"invalid JSON in cloud report.json: {e.msg} (line {e.lineno}, column {e.colno})",
                        cloud.EXIT_TROUBLE, report=False)
    except UnicodeError:
        return _failure(args, "invalid JSON in cloud report.json: text is not valid UTF-8", cloud.EXIT_TROUBLE,
                        report=False)
    except cloud.ApiError as e:
        if e.status == 404:
            return _failure(args, f"cloud run {run_id} has no report: no such run, or it has not finished",
                            cloud.EXIT_TROUBLE, report=False)
        return _failure(args, str(e), e.code, report=False)
    except (cloud.CloudError, ValueError, OSError) as e:
        return _failure(args, str(e), getattr(e, "code", cloud.EXIT_TROUBLE), report=False)
    md = folder / "report.md"
    print(json.dumps(data, indent=2) if args.json else (md.read_text() if md.exists() else json.dumps(data, indent=2)))
    if not args.json:
        print(f"qajev: kept in {folder}: report.html, report.md, report.json and {shots} screenshot"
              f"{'' if shots == 1 else 's'}", file=sys.stderr)
    return 0


def cmd_report(args):
    if args.html and args.json:
        return _failure(args, "--html and --json cannot be used together", EXIT_CONFIG, report=False)
    if not args.html and not args.run_dir.exists():
        from .cloud import RUN_ID

        if RUN_ID.match(str(args.run_dir)):
            return _cloud_report(args)
    if args.html:
        from . import report as report_mod

        source = args.run_dir / "report.json"
        data = json.loads(source.read_text()) if source.exists() else {}
        if not data or data.get("partial"):
            print(f"qajev: no finished report.json in {args.run_dir}", file=sys.stderr)
            return EXIT_CONFIG
        path = report_mod.write_html(args.run_dir, data)
        print(path)
        return 0
    path = args.run_dir / ("report.json" if args.json else "report.md")
    if not path.exists():
        return _failure(args, f"no {path.name} in {args.run_dir}", EXIT_CONFIG, report=False)
    if args.json:
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            return _failure(args, f"invalid JSON in report.json: {e.msg} (line {e.lineno}, column {e.colno})",
                            EXIT_CONFIG, report=False)
        except UnicodeError:
            return _failure(args, "invalid JSON in report.json: text is not valid UTF-8", EXIT_CONFIG, report=False)
        except OSError as e:
            return _failure(args, f"could not read report.json: {e}", EXIT_CONFIG, report=False)
        print(json.dumps(data, indent=2))
        return 0
    print(path.read_text())
    return 0


def cmd_background(args):
    from . import jobs

    argv = [a for a in args.argv if a != "--background"]
    job = jobs.start(argv, **({"rerun": _rerun} if _rerun else {}))
    if args.json:
        print(json.dumps(job, indent=2))
    else:
        print(f"job {job['id']} started: {job['title']}\n"
              f"  follow: qajev jobs {job['id']}\n  stop:   qajev stop {job['id']}")
    return 0


def _job_line(j):
    p = j["progress"]
    done = f"{p['done']}/{p['total']}" if p.get("total") else f"{p['done']} done"
    extra = j.get("gate") or j.get("current") or j.get("waiting") or j.get("error") or ""
    timing = j.get("timing")
    clocks = (f"queued {timing['queue_seconds']:.0f}s, run {timing['run_seconds']:.0f}s  " if timing else "")
    return f"{j['id']}  {j['state']:<8} {done:<8} {j['seconds']:>5}s  {clocks}{j['title'][:60]}  {extra}"[:200]


def cmd_jobs(args):
    from . import jobs

    if args.job and args.job.startswith("r_"):  # a QAJev Cloud run
        return _cloud_job(args)
    if args.job:
        try:
            job = jobs.status(args.job, detail=True)
        except jobs.NoSuchJob:
            return _fail(args, f"no job {args.job}", EXIT_CONFIG)
        if args.json:
            print(json.dumps(job, indent=2, default=str))
            return 0
        print(_job_line(job))
        if job.get("run_dir"):
            print(f"  run folder: {job['run_dir']}")
        for r in job["scenarios"]:
            print(f"  {r['outcome']:<10} {r['name'][:60]}  {(r.get('reason') or '')[:100]}")
        if job.get("report"):
            print(f"  report: {Path(job['report']['run_dir']) / 'report.html'}")
        return 0
    rows = jobs.listing(args.limit)
    now = jobs.holder()
    if args.json:
        print(json.dumps({"holder": now, "jobs": rows}, indent=2, default=str))
        return 0
    print(f"browser in use by: {jobs._who(now)}" if now else "browser free")
    for j in rows:
        print(_job_line(j))
    if not rows:
        print("no jobs yet (runs started with --background or through MCP are jobs)")
    return 0


def cmd_rerun(args):
    from . import jobs

    try:
        argv, what = jobs.rerun_argv(args.job, failed=args.failed)
        cwd = jobs.rerun_cwd(args.job)
    except jobs.NoSuchJob:
        return _fail(args, f"no job {args.job!r} (qajev jobs lists them)", EXIT_CONFIG)
    except jobs.NothingToRerun as e:
        print(f"qajev: {e}", file=sys.stderr)
        return 0
    except ValueError as e:
        return _fail(args, str(e), EXIT_CONFIG)
    argv += [flag for flag, on in (("--background", args.rerun_background), ("--json", args.json),
                                   ("--quiet", args.quiet)) if on]
    if not args.quiet:
        print(f"qajev: rerunning job {args.job}, {what}: qajev {shlex.join(argv)}"
              + (f" (in {cwd})" if cwd else ""), file=sys.stderr)
    if cwd:
        os.chdir(cwd)  # where the job ran: its relative paths (a game, a suite) are the same files
    global _rerun
    _rerun = {"of": args.job, "failed": bool(args.failed)}
    try:
        return main(argv)
    finally:
        _rerun = None


def cmd_stop(args):
    from . import jobs

    if args.job.startswith("r_"):  # a QAJev Cloud run: the dashboard stops it
        return _cloud_job(args, stopping=True)
    try:
        job = jobs.stop(args.job)
    except jobs.NoSuchJob:
        return _fail(args, f"no job {args.job}", EXIT_CONFIG)
    except jobs.NotOurs as e:
        return _fail(args, str(e), EXIT_CONFIG)
    print(json.dumps(job, indent=2, default=str) if args.json else _job_line(job))
    return 0


def cmd_nightly(args):
    from . import nightly

    out = None
    if args.install:
        try:
            out = nightly.install(args.at, args.projects)
        except RuntimeError as e:  # no launchd (not macOS), or launchctl refused
            return _fail(args, str(e), EXIT_CONFIG)
        text = f"scheduled every night at {out['at']} ({out['plist']})"
    elif args.uninstall:
        out = {"removed": nightly.uninstall()}
        text = "schedule removed" if out["removed"] else "no schedule was installed"
    elif args.plan:
        out = [{"project": p, "kind": k, "argv": a} for p, k, a in nightly.plan(args.projects or None, args.max_pages)]
        schedule = nightly.scheduled()
        text = "\n".join(f"{s['project']:<16} {s['kind']:<11} qajev {' '.join(s['argv'])}" for s in out)
        state = (schedule["at"] + (" (loaded)" if schedule["loaded"] else " (NOT loaded)")) if schedule else "none"
        text += f"\nschedule: {state}"
    elif args.last:
        out = nightly.last()
        text = nightly.markdown(out) if out else "no nightly run yet"
    if out is not None or args.last:
        print(json.dumps(out, indent=2) if args.json else text)
        return 0
    _lower_priority()
    def say(line):
        if not args.json:
            print(line, file=sys.stderr, flush=True)

    digest = nightly.run(args.projects or None, args.max_pages, notify=not args.no_notify, emit=say)
    print(json.dumps(digest, indent=2) if args.json else nightly.markdown(digest) + f"\ndigest: {digest['path']}")
    return 0


def cmd_dashboard(args):
    import time

    from . import dashboard, jobs

    def tell(url, already):
        if args.json:
            print(json.dumps({"url": url, "already_running": already}), flush=True)
        else:
            stop = "" if already else (" `qajev dashboard --stop` stops it." if args.dash_background
                                       else " Ctrl-C stops it.")
            print(f"QAJev dashboard{' (already running)' if already else ''}: {url}\n"
                  "  The address carries its key: keep it to yourself." + stop, flush=True)
        if args.open:
            import webbrowser

            webbrowser.open(url)

    if args.new_key and not args.stop:
        if dashboard.running():
            return _fail(args, "a dashboard is running: `qajev dashboard --stop` first, then --new-key", EXIT_CONFIG)
        dashboard.stored_key(new=True)
    there = dashboard.running()
    if args.stop:
        if there:
            os.kill(there["pid"], signal.SIGTERM)
            # Stopped means gone: it closes its server and removes its record first. A start right after found the
            # lock still held ("another dashboard holds the lock but is not serving"). Its pid is not proof: macOS
            # calls a process that is still exiting gone before the kernel frees its lock, so wait for the lock too.
            deadline, free = time.monotonic() + DASHBOARD_STOP_S, None
            while time.monotonic() < deadline:
                if not jobs.alive(there["pid"]) and (free := dashboard.claim()) is not None:
                    break
                time.sleep(0.05)
            if free is None:
                return _fail(args, f"the dashboard (pid {there['pid']}) did not stop within {DASHBOARD_STOP_S:g} s: "
                             "it still runs or still holds its lock", EXIT_CONFIG)
            os.close(free)
        if args.json:
            print(json.dumps({"stopped": there["pid"] if there else None}))
        else:
            print(f"qajev: stopped the dashboard (pid {there['pid']})" if there else "qajev: no dashboard running")
        return 0
    if there:  # one dashboard per machine: it shows every run anyway
        tell(there["url"], True)
        return 0
    if args.dash_background:
        import subprocess

        log = dashboard.STATE.with_suffix(".log")
        start = log.stat().st_size if log.exists() else 0
        with open(log, "a") as out:
            child = subprocess.Popen(config.qajev_argv("dashboard", "--port", args.port),
                                     stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
        there = dashboard.wait_running(timeout=45, starting=child)
        if not there:
            if child.poll() is None:  # never left running half-started: it would hold the one-dashboard lock
                child.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(5)
            from qajev_upload_privacy import sanitize_text

            said = (sanitize_text(log.read_bytes()[start:].decode("utf-8", errors="replace").strip(), 600, tail=True)
                    if log.exists() else "")
            return _fail(args, f"the dashboard did not start (exit {child.poll()}): {said or 'it said nothing'}",
                         EXIT_CONFIG)
        tell(there["url"], False)
        return 0
    lock = dashboard.claim()
    if lock is None:  # another `qajev dashboard` is starting right now: point at that one
        there = dashboard.wait_running()
        if not there:
            return _fail(args, "another dashboard holds the lock but is not serving", EXIT_CONFIG)
        tell(there["url"], True)
        return 0
    try:
        server = dashboard.make_server(args.port, key=dashboard.stored_key())
    except OSError as e:
        return _fail(args, str(e), EXIT_CONFIG)
    tell(f"http://127.0.0.1:{server.server_address[1]}/?k={server.RequestHandlerClass.key}", False)
    signal.signal(signal.SIGTERM, signal.default_int_handler)  # `kill` stops it like Ctrl-C: it removes its record
    with contextlib.suppress(KeyboardInterrupt):
        dashboard.serve(server)
    return 0


def cmd_top(args):
    from . import jobs, top

    if args.decisions:
        try:
            jobs.status(args.decisions)
        except jobs.NoSuchJob:
            return _fail(args, f"no job {args.decisions!r} (qajev jobs lists them)", EXIT_CONFIG)
        if args.json:
            print(json.dumps(jobs.decisions(args.decisions), indent=2, default=str))
            return 0
        if args.once or not sys.stdout.isatty():
            print(top.plain_decisions(args.decisions, width=shutil.get_terminal_size((120, 40)).columns))
            return 0
        return top.run_ui(watch=args.decisions)
    if args.json:
        print(json.dumps(top.snapshot(), indent=2, default=str))
        return 0
    if args.once or not sys.stdout.isatty():
        print(top.plain(top.snapshot(), width=shutil.get_terminal_size((120, 40)).columns))
        return 0
    return top.run_ui()


def cmd_init(args):
    if args.path.exists():
        print(f"qajev: {args.path} already exists", file=sys.stderr)
        return EXIT_CONFIG
    args.path.write_text(SUITE_TEMPLATE.format(name=args.path))
    print(f"wrote {args.path}; edit base_url and run: qajev run {args.path}")
    return 0


def cmd_mcp(args):
    from .mcp_server import serve

    serve(allow_commands=args.allow_commands)
    return 0


def _lower_priority():
    lowprio.lower_self()  # shared machine: the driver, its daemon and its Chrome all run below interactive work


def cmd_observe(args):
    from .observer import Client, Observer, ObserverError, OutboxFull

    _lower_priority()
    if args.every < 5:
        print("qajev: observer interval must be at least 5 seconds", file=sys.stderr)
        return EXIT_CONFIG
    if args.max_outbox_mib < 1:
        print("qajev: --max-outbox-mib must be at least 1", file=sys.stderr)
        return EXIT_CONFIG
    if not args.inventory and not args.credential_file:
        print("qajev: observe needs --credential-file (or --inventory for a local-only snapshot)", file=sys.stderr)
        return EXIT_CONFIG
    if args.credential_file and any(args.credential_file.resolve().is_relative_to(root.resolve())
                                    for root in args.report_root):
        print("qajev: keep the machine credential outside approved report roots", file=sys.stderr)
        return EXIT_CONFIG
    project_for = []
    for item in args.project_for:
        folder, _, name = item.rpartition("=")
        if not folder or not name:
            print(f"qajev: --project-for takes FOLDER=PROJECT, not {item!r}", file=sys.stderr)
            return EXIT_CONFIG
        project_for.append((Path(folder), name))
    if not args.project and not project_for:
        print("qajev: observe needs --project or at least one --project-for FOLDER=PROJECT", file=sys.stderr)
        return EXIT_CONFIG
    try:
        with Observer(args.home, args.report_root, args.state_dir, args.project, args.url,
                      max_outbox_bytes=args.max_outbox_mib * 1024**2, project_for=project_for) as observer:
            client = None
            while True:
                inventory = observer.inventory()
                if args.inventory:
                    print(json.dumps(inventory))
                    return 0
                if client is None:
                    client = Client(args.url, args.credential_file)
                print(json.dumps({**inventory, **observer.flush(client)}), flush=True)
                if args.once:
                    return 0
                time.sleep(args.every)
    except OutboxFull as failure:
        print(json.dumps({"error": "outbox_full", "blob_bytes": failure.blob_bytes,
                          "outbox_bytes": failure.used, "max_outbox_bytes": failure.limit,
                          "next_write_bytes": failure.needed}), flush=True)
        print(f"qajev: observation stopped: {failure}", file=sys.stderr)
        return EXIT_CONFIG
    except (ObserverError, OSError) as failure:
        # Network failures have a fixed, credential-free message; no endpoint response body is printed.
        print(f"qajev: observation stopped: {failure}", file=sys.stderr)
        return EXIT_CONFIG


def _provider_flag(args):
    if getattr(args, "jev_provider", None):
        os.environ["QAJEV_JEV_PROVIDER"] = args.jev_provider


_interrupted = False


def _interrupt(_signum, _frame):
    """First SIGINT/SIGTERM: stop and clean up, exactly like Ctrl-C. Later ones are ignored so they cannot abort that
    cleanup halfway (Ctrl-C and `timeout` signal the whole process group, and `uv run` forwards on top)."""
    global _interrupted
    if _interrupted:
        return
    _interrupted = True
    raise KeyboardInterrupt


def _stop_when_orphaned(every=2.0, stop=None):
    """Stop the run, like Ctrl-C, when the process that started it ends. A lane stop or a killed script left
    `qajev play` running on its own, its game window at 120% CPU for 20 minutes (SideGame1 plan 61, 7 Oct).
    Started by launchd (parent 1 from the start: nightly), there is nothing to watch. QAJEV_OUTLIVE_PARENT=1 keeps
    a run that was detached on purpose. A background job's parent is its wrapper, which waits for it. It waits on an
    event, not time.sleep, so a fake clock in the same process can never make it spin (CI, 13 Oct); `stop` ends it."""
    parent = os.getppid()
    if parent == 1 or os.environ.get("QAJEV_OUTLIVE_PARENT") == "1":
        return None
    halt = stop or threading.Event()

    def watch():
        while os.getppid() == parent:
            if halt.wait(every):
                return
        print(f"qajev: the process that started this run (pid {parent}) ended; stopping", file=sys.stderr)
        os.kill(os.getpid(), signal.SIGTERM)

    thread = threading.Thread(target=watch, name="qajev-parent-watch", daemon=True)
    thread.start()
    return thread


def _refused(args, message):
    """Production is read-only, destructive is never: nothing was started (exit 5, outcome "refused")."""
    return _failure(args, message, EXIT_REFUSED, {"outcome": "refused", "reason": message}, prefix="refused: ")


def cmd_plan(args):
    """A suite's test plan, without running anything: each test's about and its checks in plain words, and the tests
    that are NOT DESCRIBED (a run of it would be INCOMPLETE, never PASS), and its approved test plan file (planfile.py).
    Exit 0 when all are described, 2 if not, or when QAJEV_REQUIRE_PLAN=1 and the plan file is not approved."""
    import yaml

    from . import native, planfile
    from . import plan as plan_mod
    from .suite import SuiteError, about, load

    try:
        spec = yaml.safe_load(args.file.read_text()) or {}
        blind = []  # the tests whose only checks are "lacks" checks
        keyed = []  # the tests whose goal asks Jev to press a key
        planted = []  # the planted faults (expect_fail), each with the check it must fail on
        if isinstance(spec, dict) and spec.get("steps") and not spec.get("scenarios"):  # a game's steps suite
            name, run_about = spec.get("name") or args.file.stem, about(spec.get("about"), "about")
            native.check_window(spec["steps"])
            native.check_pad(spec["steps"])
            native.check_looks(spec["steps"], args.file.parent)
            from .mackeys import check_keys

            check_keys(spec["steps"], spec.get("app"))
            from .openurl import check_open_url

            check_open_url(spec["steps"])
            items = plan_mod.from_steps(native.check_judged_by(spec["steps"]))
            named = spec.get("plan")
        else:
            suite = load(args.file)
            name, run_about, items = suite.name, suite.about, plan_mod.from_suite(suite.scenarios)
            named = suite.plan
            blind = plan_mod.absent_only(suite.scenarios)
            keyed = plan_mod.key_goals(suite.scenarios)
            planted = [{"name": s.name, "check": s.expect_fail["words"]} for s in suite.scenarios if s.expect_fail]
    except (OSError, yaml.YAMLError, SuiteError, native.NativeError) as e:
        return _fail(args, f"{args.file}: {e}", EXIT_CONFIG)
    missing = plan_mod.not_described(items)
    test_plan = planfile.check(planfile.resolve(named, args.file.parent) if named else None)
    unapproved = planfile.required() and not test_plan["approved"]
    if args.json:
        print(json.dumps({"name": name, "about": run_about, "plan": items, "not_described": missing,
                          "absent_only": blind, "key_goals": keyed, "expected_fail": planted,
                          "test_plan": test_plan}))
    else:
        print(f"Test plan: {name}" + (f" — {run_about}" if run_about else ""))
        print(f"Plan file: {test_plan['path'] + ', ' if test_plan['path'] else ''}{planfile.words(test_plan)}")
        for it in items:
            print(f"{it['n']}. {it['name']}: {it['about'] or 'NOT DESCRIBED (no about)'}")
            for line in it["checks"]:
                print(f"     - {line['words'] or 'NOT DESCRIBED: ' + str(line['check'])}")
        described = len(items) - len(missing)
        print(f"{described} of {len(items)} tests say what they prove"
              + (f"; NOT DESCRIBED: {', '.join(missing)} (a run would be INCOMPLETE)" if missing else ""))
        if blind:
            print(f"ABSENT ONLY: {', '.join(blind)}: each only checks that a text is absent, and a blank page passes "
                  "that. Add a text or js check that must be there.")
        if keyed:
            print("PRESSES KEYS: " + "; ".join(f"{k['name']} (“{k['says']}”)" for k in keyed) + ": Jev cannot press "
                  "keys. Press them in a `key:` hook (before or after) and leave them out of the goal.")
        if planted:
            print(f"EXPECTED TO FAIL: {len(planted)} planted fault(s), each met only when it fails on its check: "
                  + "; ".join(f"{p['name']} (on “{p['check']}”)" for p in planted))
    return 2 if missing or blind or keyed or unapproved else 0


def url_refusal(args):
    """config.user_info_refusal for a parsed run's URL arguments, with or without a scheme ("alice:s3cret@host/x" is a
    URL to check and smoke; redteam on #133), and for each --host, then for any other argument with "://". An e-mail
    flag is no URL."""
    from .config import user_info_in_args, user_info_refusal

    named = [getattr(args, key, None) for key in ("url", "expect_url", "cdp_url")]
    for value in [*named, *(getattr(args, "fetch", None) or []), *(getattr(args, "host", None) or [])]:
        if value and (refused := user_info_refusal(value)):
            return refused
    return user_info_in_args(getattr(args, "argv", None) or [])


def _fail(args, message, code):
    return _failure(args, message, code)


def _failure(args, message, code, extra=None, *, prefix="", suffix="", report=True):
    args._failure_code = code  # a closed output pipe must not turn a rejected run into exit 0
    message = config.redact(str(message))
    out = config.redact_tree({"error": message, "exit_code": code, **(extra or {})})
    if report and getattr(args, "command", None) == "run":
        from . import report as report_mod

        source = getattr(args, "suite", None) or getattr(args, "project", None)
        try:
            data = report_mod.suite_error(args.out, source, message, code,
                                         outcome=out.get("outcome", "suite_error"), runner=out.get("runner"))
        except OSError as e:
            # A full disk or an invalid --out must not turn a rejection into a traceback or a clean exit.
            print(f"qajev: could not write error report: {config.redact(str(e))}", file=sys.stderr)
        else:
            out = data
            print(f"Report: {Path(data['run_dir']) / 'report.md'} (also report.json, report.html)", file=sys.stderr)
    _record(out)
    if getattr(args, "json", False):
        print(json.dumps(out))
    print(f"qajev: {prefix}{out['error']}{config.redact(suffix)}", file=sys.stderr)
    return code


def _route(args):
    """--runner: where this run goes (cloud.py). -> an exit code when the cloud took or refused it, None to run here.
    cloud never falls back to this Mac; auto runs here, saying why, only a run the cloud may not take."""
    from . import cloud

    try:
        runner, source = cloud.runner_for(args)
    except cloud.CloudError as e:
        return _cloud_fail(args, str(e), e.code)
    cloud_only = [flag for flag, on in (("--serve", getattr(args, "serve", None)),
                                        ("--model", getattr(args, "model", None))) if on]
    if runner == "local":
        if cloud_only:
            return _fail(args, f"{' and '.join(cloud_only)} go with cloud runs (--runner cloud or auto); this run "
                               "stays on this Mac", EXIT_CONFIG)
        return None
    try:
        from .config import load_env

        load_env(args.env_file)  # QAJEV_CLOUD_URL and QAJEV_CLOUD_KEY may live in ~/.qajev/.env
        args.runner_named = source == "--runner"  # held suites and this Mac's dev servers need the cloud named here
        plan = cloud.plan(args)
    except FileNotFoundError as e:
        return _cloud_fail(args, str(e), EXIT_CONFIG)
    except cloud.NotAllowed as e:
        if runner == "cloud":
            said = "" if source == "--runner" else f" ({source})"
            return _cloud_fail(args, f"--runner cloud{said}: {e}", EXIT_CONFIG)
        if cloud_only:
            return _fail(args, f"{' and '.join(cloud_only)} go with cloud runs, and this one runs on this Mac: {e}",
                         EXIT_CONFIG)
        if not args.quiet:
            print(f"qajev: --runner auto: this run stays on this Mac: {e}", file=sys.stderr, flush=True)
        return None
    except cloud.CloudError as e:
        return _cloud_fail(args, str(e), e.code)
    return cmd_cloud(args, plan)


def _cloud_fail(args, message, code, run_id=None, url=None):
    """A cloud run that was refused or failed: nothing ran on this Mac. Says the run's address when it has one."""
    out = {"runner": "cloud", "error": message, "reason": message, "exit_code": code}
    if run_id:
        out.update(run_id=run_id, url=url)
    suffix = f" ({url})" if url and url not in message else ""
    return _failure(args, message, code, out, suffix=suffix, report=run_id is None)


def cmd_cloud(args, plan):
    """Send the run to QAJev Cloud, then (unless --background) follow it to its gate."""
    from . import cloud

    for warning in plan.warnings:
        print(f"qajev: warning: {warning}", file=sys.stderr)
    try:
        client = cloud.connect(args.env_file)
        run_id = cloud.submit(client, plan)
    except cloud.CloudError as e:
        return _cloud_fail(args, str(e), e.code, e.run_id, e.url)
    url = cloud.run_url(client.base, run_id)
    if args.background:
        if args.json:
            print(json.dumps({"runner": "cloud", "run_id": run_id, "url": url, "gate": "QUEUED"}))
        else:
            print(f"cloud run {run_id} queued: {url}\n  follow: qajev jobs {run_id}\n  stop:   qajev stop {run_id}")
        return 0
    say = _cloud_printer(args)
    say({"event": "cloud", "run_id": run_id, "url": url, "gate": "queued"})
    signal.signal(signal.SIGTERM, _interrupt)  # a stop by signal stops the cloud run too, like Ctrl-C
    signal.signal(signal.SIGINT, _interrupt)
    _stop_when_orphaned()
    try:
        run = cloud.wait(client, run_id, on_change=lambda r: say({"event": "cloud", "run_id": run_id, "url": url,
                                                                  "gate": r.get("gate"), "progress": r.get("progress"),
                                                                  "now_test": r.get("now_test"),
                                                                  "waiting": r.get("waiting")}))
    except KeyboardInterrupt:
        with contextlib.suppress(cloud.CloudError):
            cloud.stop(client, run_id)
        print(f"qajev: stopped cloud run {run_id}: {url}", file=sys.stderr)
        return EXIT_INTERRUPTED
    except cloud.CloudError as e:
        return _cloud_fail(args, str(e), e.code, run_id, url)
    out = cloud.result(client.base, run)
    if args.json:
        print(json.dumps(out, indent=2))
    elif not args.quiet:
        _cloud_summary(out)
    return cloud.exit_code(out["gate"])


def _cloud_printer(args):
    """Progress of a cloud run on stderr: a line when its gate, progress or test changes (JSON lines with --events)."""
    if args.events:
        return _safe(lambda event: print(json.dumps(event, default=str), file=sys.stderr, flush=True))
    if args.quiet or args.json:
        return lambda _event: None

    def emit(event):
        gate = event.get("gate")
        if "progress" not in event:  # the first line: where a person can watch it
            print(f"qajev: cloud run {event['run_id']}: {event['url']}", file=sys.stderr, flush=True)
        elif gate == "queued":
            why = (event.get("waiting") or {}).get("text") or "waiting for a cloud machine"
            print(f"  queued: {why}", file=sys.stderr, flush=True)
        elif gate == "running":
            now = " · ".join(str(x) for x in (event.get("progress"), event.get("now_test")) if x)
            print(f"  running{': ' + now if now else ''}", file=sys.stderr, flush=True)

    return _safe(emit)


def _cloud_summary(out):
    marks = {"pass": "✓", "fail": "✗", "stuck": "…", "harness": "!", "todo": "-"}
    for t in out["tests"]:
        print(f"  {marks.get(t['outcome'], '?')} {str(t['outcome']):<10} {t['name']}"
              + (f"  {t['reason']}" if t.get("reason") else ""), file=sys.stderr)
    c = out["counts"]
    counts = ", ".join(f"{c[k]} {k}" for k in ("pass", "fail", "stuck", "harness") if k in c)
    print(f"\nGate: {out['gate']}" + (f"  ({counts})" if counts else ""), file=sys.stderr)
    if out.get("reason"):
        print(f"Reason: {out['reason']}", file=sys.stderr)
    print(f"Run: {out['url']}", file=sys.stderr)
    if out.get("report_url"):
        print(f"Report: {out['report_url']}", file=sys.stderr)


def _cloud_job(args, stopping=False):
    """`qajev jobs r_...` / `qajev stop r_...`: a cloud run's state, from the dashboard."""
    from . import cloud

    try:
        client = cloud.connect()
        if stopping:
            cloud.stop(client, args.job)
        out = cloud.result(client.base, cloud.status(client, args.job))
    except cloud.CloudError as e:
        return _cloud_fail(args, str(e), e.code, args.job, None)
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"{out['run_id']}  {out['gate']}  {out['url']}")
    for t in out["tests"]:
        print(f"  {str(t['outcome']):<10} {str(t['name'])[:60]}  {(t.get('reason') or '')[:100]}")
    if out.get("reason"):
        print(f"  reason: {out['reason']}")
    if out.get("waiting"):
        print(f"  waiting: {out['waiting'].get('text')}")
    if out.get("report_url"):
        print(f"  report: {out['report_url']}")
    return 0


def main(argv=None):
    global _job_dir
    _job_dir = None
    args = build_parser().parse_args(argv)
    args.argv = list(sys.argv[1:] if argv is None else argv)
    if args.command in {"check", "run", "smoke", "play"}:
        # Before anything is recorded or sent: a job record and a cloud run keep the arguments as typed
        if refused := url_refusal(args):
            return _fail(args, refused, EXIT_CONFIG)
    # Where it runs, before anything starts here. A background job's run was routed when it started.
    if args.command in {"check", "run", "smoke", "play"} and not os.environ.get("QAJEV_JOB"):
        routed = _route(args)
        if routed is not None:
            return routed
    if getattr(args, "background", False):
        return cmd_background(args)
    if args.command in {"check", "run", "smoke", "play"}:
        signal.signal(signal.SIGTERM, _interrupt)
        signal.signal(signal.SIGINT, _interrupt)
        _stop_when_orphaned()
        if not os.environ.get("QAJEV_JOB"):  # a background job's run is already recorded by its wrapper
            from . import jobs

            _job_dir = jobs.register(args.argv, rerun=_rerun)
    handlers = {"observe": cmd_observe, "check": cmd_run, "run": cmd_run, "smoke": cmd_smoke,
                "browser": cmd_browser, "doctor": cmd_doctor,
                "secret": cmd_secret, "account": cmd_account,
                "report": cmd_report, "init": cmd_init, "mcp": cmd_mcp, "projects": cmd_projects,
                "reports": cmd_reports, "jobs": cmd_jobs, "stop": cmd_stop,
                "top": cmd_top, "nightly": cmd_nightly, "rerun": cmd_rerun, "dashboard": cmd_dashboard,
                "play": cmd_play, "plan": cmd_plan, "plan-hash": cmd_plan_hash, "tunnel": cmd_tunnel,
                "mode": cmd_runner, "runner": cmd_runner}
    code = 1  # a crash on the way out still leaves an exit code for `qajev jobs`
    try:
        code = handlers[args.command](args)
        return code
    except BrokenPipeError:
        code = getattr(args, "_failure_code", 0)
        return code
    except KeyboardInterrupt:
        code = EXIT_INTERRUPTED
        if args.command in {"check", "run", "smoke", "play"}:  # stopped before its handler could (still importing)
            return code
        raise
    finally:
        if _job_dir is not None:
            with contextlib.suppress(OSError):
                (_job_dir / "exit_code").write_text(f"{code}\n")
