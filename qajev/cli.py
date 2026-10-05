"""qajev: QA any website with Jev. Heavy imports are deferred so `qajev --help` stays instant."""

import argparse
import contextlib
import json
import os
import shlex
import shutil
import signal
import sys
from pathlib import Path

from . import __version__

EXIT_CONFIG, EXIT_BROWSER, EXIT_INTERRUPTED = 3, 4, 130

SUITE_TEMPLATE = """\
# QAJev suite. Run: qajev run {name}
name: My site
base_url: http://localhost:3000
mode: readonly            # readonly (default, blocks writes) | mutate (loopback hosts only)
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


def _common(p):
    g = p.add_argument_group("browser and run")
    g.add_argument("--cdp-url", help="attach to an existing Chrome DevTools endpoint instead of QAJev's own Chrome")
    g.add_argument("--profile", default=os.environ.get("QAJEV_PROFILE", "default"),
                   help="QAJev Chrome profile (sign in once with `qajev browser login`)")
    g.add_argument("--headless", action="store_true", help="run QAJev's Chrome headless")
    g.add_argument("--ephemeral", action="store_true", help="throwaway profile, deleted after the run")
    g.add_argument("--real-devices", help="also run each website scenario in a real device browser: android (Chrome "
                                          "on a read-only emulator), ios (Safari on a throwaway simulator clone; "
                                          "page content not readable yet, comes out harness); comma-separated, off "
                                          "by default")
    g.add_argument("--devices", help="every website test runs on each of these, comma-separated (default "
                                     "desktop,phone, or $QAJEV_DEVICES); e.g. --devices desktop for desktop only")
    g.add_argument("--out", type=Path, default=Path(os.environ.get("QAJEV_OUT", "qajev-runs")),
                   help="where run folders go (default ./qajev-runs)")
    g.add_argument("--env-file", help="file with TYPESAFE_API_KEY etc. (default ./.env, then ~/.qajev/.env)")
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
    _load_flags(g)
    g.add_argument("--json", action="store_true", help="print the report JSON on stdout")
    g.add_argument("--events", action="store_true", help="stream JSON-lines progress events on stderr")
    g.add_argument("--quiet", "-q", action="store_true", help="no progress output")
    g.add_argument("--background", action="store_true",
                   help="start the run as a job and return at once; follow it with `qajev jobs`, stop it with "
                        "`qajev stop`")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="qajev",
        description="QA any website with Jev (TypeSafe x Browser Use): goals in plain words, verdicts from the page.",
    )
    parser.add_argument("--version", action="version", version=f"qajev {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="one scenario from flags: a goal and/or expectations on a URL")
    check.add_argument("url")
    check.add_argument("--goal", "-g", help="what Jev should do, in plain words; end with 'Stop when ...'")
    check.add_argument("--expect-text", "-t", action="append", default=[], help="page must show this (repeatable)")
    check.add_argument("--absent", "-a", action="append", default=[], help="page must not show this (repeatable)")
    check.add_argument("--visible", action="append", default=[],
                       help="this text must be on screen (in the viewport), not just in the page (repeatable)")
    check.add_argument("--expect-url", "-u", help="final URL must contain this")
    check.add_argument("--expect-url-regex", help="final URL must match this regex")
    check.add_argument("--expect-js", "-j", help="JS expression that must be truthy (may use await)")
    check.add_argument("--expect-looks", action="append", default=[], metavar="STATEMENT",
                       help="judged from the final screenshot, e.g. 'the Sign up button is not cut off' (repeatable; "
                            "needs Clef)")
    check.add_argument("--vision", action="store_true",
                       help="Clef sees the screenshot with every decision (needs Clef)")
    check.add_argument("--fetch", action="append", default=[], metavar="URL[=STATUS]",
                       help="in-page GET that must answer STATUS (default 200); repeatable")
    check.add_argument("--mode", choices=["readonly", "mutate"], default="readonly")
    check.add_argument("--device", help="test on this device only: desktop | tall | phone | tablet | WIDTHxHEIGHT "
                                       "(default: every device in --devices)")
    check.add_argument("--persona", help="who Jev is, prepended to the goal")
    check.add_argument("--about", help="what this test proves and why, in plain words (shown with its result)")
    check.add_argument("--speech", help="what the fake microphone 'hears' if the page listens")
    check.add_argument("--host", action="append", default=[], help="extra host Jev may visit (repeatable)")
    check.add_argument("--max-actions", type=int, default=20)
    check.add_argument("--max-seconds", type=float, default=90)
    check.add_argument("--settle", type=float, default=10, help="seconds to wait for expectations after Jev stops")
    _common(check)

    run = sub.add_parser("run", help="run a suite file (YAML or JSON)")
    run.add_argument("suite", type=Path, nargs="?", help="a suite file (or use --project)")
    run.add_argument("--only", action="append", default=[], help="run this scenario (plus its dependencies)")
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
    proj.add_argument("--expect-js", "-j", help="for --objective: JS expression that must be truthy")
    run.add_argument("--jobs", type=int, default=1, help="parallel workers for independent chains (default 1)")
    run.add_argument("--allow-commands", action="store_true", help="let the suite run shell commands (hooks/checks)")
    _common(run)

    smoke = sub.add_parser("smoke", help="crawl same-origin pages with no model calls and lint them")
    smoke.add_argument("url", nargs="?", help="start URL (or use --project)")
    smoke.add_argument("--project", "-p", help="crawl a project's environment from its base_url")
    smoke.add_argument("--env", "-e", help="project environment (default: the project's default_env)")
    smoke.add_argument("--max-pages", type=int, default=20)
    smoke.add_argument("--device", help="crawl on this device only (default: every device in --devices)")
    smoke.add_argument("--check-links", action="store_true", help="also HEAD-check discovered links not crawled")
    smoke.add_argument("--no-ux", action="store_true",
                       help="skip the UX measurements (contrast, keyboard, 200%% zoom, dialogs, design consistency)")
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
    play.add_argument("--goal", "-g", help="what a player wants, in plain words; end with 'Stop when ...'")
    play.add_argument("--about", help="what this test proves and why, in plain words (with --suite: the whole run)")
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

    browser = sub.add_parser("browser", help="QAJev's own Chrome: start, stop, status, login")
    browser.add_argument("action", choices=["start", "stop", "status", "login", "reap"])
    browser.add_argument("--profile", default=os.environ.get("QAJEV_PROFILE", "default"))
    browser.add_argument("--headless", action="store_true")
    browser.add_argument("--url", help="for login: the page to sign in on")
    browser.add_argument("--json", action="store_true")

    secret = sub.add_parser("secret", help="store or check a test account's password reference (never prints it)")
    secret.add_argument("action", choices=["set", "check"],
                        help="set: save a keychain: password (its store prompts for it); check: can QAJev read it")
    secret.add_argument("ref", help="keychain:SERVICE/ACCOUNT, op://VAULT/ITEM/FIELD or env:NAME")

    account = sub.add_parser("account", help="set up a stored test account: QAJev signs in with it, Jev never sees "
                             "the password (run it yourself: the Keychain asks you for it)")
    account.add_argument("action", choices=["add", "check"],
                         help="add: save the password, write the account, sign in once to prove it; check: sign in")
    account.add_argument("name", help="the account's name (letters, digits, - and _), e.g. shop-tester")
    account.add_argument("--email", help="add: the test account's email or user name")
    account.add_argument("--login-url", help="add: the sign-in page (https; with --project it may be a path)")
    account.add_argument("--password", metavar="REF",
                         help="where the password lives: keychain:qajev/NAME (the default, saved at the Keychain's "
                              "prompt), op://VAULT/ITEM/FIELD or env:NAME")
    account.add_argument("--project", help="write it into this project (add), or read it from there (check)")
    account.add_argument("--env", help="with --project: the env whose site to sign in to (default: its default)")
    account.add_argument("--default", action="store_true", help="add with --project: every env signs in with it")
    account.add_argument("--replace", action="store_true", help="add: save a new Keychain password over the old")
    account.add_argument("--no-check", action="store_true", help="add: do not sign in to prove it")
    account.add_argument("--visible", action="store_true", help="sign in in a visible window (default: headless)")
    account.add_argument("--cdp-url", help="sign in in this already running Chrome instead of a throwaway one")

    doctor = sub.add_parser("doctor", help="check keys, Chrome, ports and load (never prints secrets)")
    doctor.add_argument("--env-file")
    doctor.add_argument("--offline", action="store_true", help="skip the free OpenRouter key validity check")
    doctor.add_argument("--json", action="store_true")

    rep = sub.add_parser("report", help="print a run's report (Markdown, or JSON with --json)")
    rep.add_argument("run_dir", type=Path)
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
    jobs.add_argument("job", nargs="?", help="a job id: show its progress, finished scenarios and result")
    jobs.add_argument("--limit", type=int, default=20)
    jobs.add_argument("--json", action="store_true")

    stop = sub.add_parser("stop", help="stop a job (it closes its browser and keeps the scenarios that finished)")
    stop.add_argument("job")
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
    screen = _screen_printer(args)
    if _job_dir is None:
        return screen
    path = _job_dir / "events.jsonl"

    def emit(event):
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
            where = (f"{b.get('engine')} game {b.get('project')}" if b.get("surface") == "native"
                     else f"Chrome {b.get('cdp_url')}")
            print(f"qajev: {event['suite']} -> {event['run_dir']} ({where})", file=sys.stderr)
        elif kind == "waiting":
            print(f"  waiting: {event['reason']}", file=sys.stderr, flush=True)
        elif kind == "start":
            print(f"  ▸ {event['scenario']}", file=sys.stderr, flush=True)
        elif kind == "step" and not event.get("pulse"):
            p = f" (p {event['p']:.2f})" if isinstance(event.get("p"), (int, float)) else ""
            spent = f" · ${event['spent_usd']:.4f}" if isinstance(event.get("spent_usd"), (int, float)) else ""
            print(f"      Jev: {event['doing']}{p}{spent}", file=sys.stderr, flush=True)
        elif kind == "scenario":
            r = event["result"]
            actions = (r.get("jev") or {}).get("actions")
            extra = f", {actions} actions" if actions is not None else ""
            print(f"  {marks.get(r['outcome'], '?')} {r['outcome']:<10} {r['name']} ({r.get('seconds', 0)}s{extra})"
                  f" {r.get('reason', '')}", file=sys.stderr, flush=True)
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
    from .runner import Options

    return Options(
        cdp_url=args.cdp_url, profile=args.profile, headless=args.headless, ephemeral=args.ephemeral,
        out_dir=args.out, jobs=getattr(args, "jobs", 1), strict=args.strict,
        allow_commands=getattr(args, "allow_commands", False), typesafe_usd_per_call=args.usd_per_call,
        cost_cap_usd=args.cost_cap, load_high=args.load_high or None, load_ok=args.load_ok,
        load_wait=args.load_wait, only=getattr(args, "only", []), shots=not args.no_shots, emit=_printer(args),
        ux=not getattr(args, "no_ux", False),
        motion=args.motion,
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
        print(f"Report: {Path(report['run_dir']) / 'report.html'} (also report.md, report.json)", file=sys.stderr)
    if report.get("interrupted"):
        return EXIT_INTERRUPTED
    return report["exit_code"]


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
    if args.expect_looks:
        expect["looks"] = args.expect_looks
    if args.fetch:
        probes = []
        for item in args.fetch:
            url, _, status = item.partition("=")
            probes.append({"url": url, "status": int(status or 200)})
        expect["fetch"] = probes
    scenario = {"name": "check", "url": args.url, "expect": expect, "mode": args.mode, "device": args.device,
                "settle": args.settle, "budget": {"actions": args.max_actions, "seconds": args.max_seconds}}
    if args.goal:
        scenario["goal"] = args.goal
    if args.persona:
        scenario["persona"] = args.persona
    if args.about:
        scenario["about"] = args.about
    if args.speech:
        scenario["speech"] = args.speech
    if args.vision:
        scenario["vision"] = True
    if not args.device:
        del scenario["device"]
    data = {"name": f"check {args.url}", "scenarios": [scenario], "hosts": args.host}
    if args.devices:
        data["devices"] = args.devices
    return suite_mod.parse(data)


def _machine_lock(args):
    """One browser run at a time on this machine, across every session: later runs queue (and say so)."""
    from . import jobs

    return jobs.machine_lock(jobs.describe_argv(args.argv), emit=_printer(args),
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


def cmd_run(args):
    from .chrome import ChromeError
    from .config import load_env
    from .jobs import Busy
    from .project import ProjectError
    from .runner import ConfigError, run
    from .suite import SuiteError, load

    _lower_priority()
    _provider_flag(args)
    try:
        load_env(args.env_file)
        if args.command == "run" and getattr(args, "project", None):
            with _machine_lock(args):
                return _run_project(args)
        if args.command == "run" and not args.suite:
            return _fail(args, "give a suite file or --project", EXIT_CONFIG)
        suite = check_suite(args) if args.command == "check" else load(args.suite, devices=args.devices)
        if getattr(args, "real_devices", None):
            from .suite import _real_devices

            suite.real_devices = _real_devices(args.real_devices)
        with _machine_lock(args):
            report = run(suite, _options(args))
    except (SuiteError, ConfigError, FileNotFoundError, ProjectError) as e:
        return _fail(args, str(e), EXIT_CONFIG)
    except (ChromeError, Busy) as e:
        return _fail(args, str(e), EXIT_BROWSER)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    return _finish(args, report)


def _run_project(args):
    """A project's objectives (or one ad-hoc objective) as a suite; the report is filed with the project."""
    from . import project as project_mod
    from .runner import run
    from .suite import parse

    proj = project_mod.load(args.project)
    expect = {}
    for key, value in (("text", args.expect_text), ("absent", args.absent), ("url", args.expect_url),
                       ("js", args.expect_js)):
        if value:
            expect[key] = value
    if (expect or args.about) and not args.objective:
        raise project_mod.ProjectError("--expect-* and --about go with --objective (stored objectives carry their own)")
    env = args.env or proj.default_env
    data = project_mod.suite_data(proj, env=env, tags=args.tags, names=args.names, objective=args.objective,
                                  url=args.url, expect=expect, about=args.about)
    suite = parse(data, proj.config_path, devices=args.devices)
    if getattr(args, "real_devices", None):
        from .suite import _real_devices

        suite.real_devices = _real_devices(args.real_devices)
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
        with _machine_lock(args):
            _wait_for_quiet(args)
            report = smoke.run(url, opts, max_pages=args.max_pages, device=args.device, devices=args.devices,
                               check_links=args.check_links, delay=args.delay)
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
    if args.suite:
        import yaml

        try:
            spec = yaml.safe_load(args.suite.read_text()) or {}
        except (OSError, yaml.YAMLError) as e:
            return _fail(args, f"{args.suite}: {e}", EXIT_CONFIG)
        session_steps = spec.get("steps") or []
        if not session_steps:
            return _fail(args, f"{args.suite}: no steps", EXIT_CONFIG)
        from .suite import SuiteError, about

        try:
            run_about = args.about or about(spec.get("about"), "about")
            for i, step in enumerate(session_steps):
                if isinstance(step, dict) and step.get("about") is not None:
                    step["about"] = about(step["about"], f"steps[{i}].about")
        except SuiteError as e:
            return _fail(args, f"{args.suite}: {e}", EXIT_CONFIG)
        if args.only:
            try:
                session_steps = native.select_steps(session_steps, args.only)
            except native.NativeError as e:
                return _fail(args, f"{args.suite}: {e}", EXIT_CONFIG)
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
    from . import vision

    # Vision is on by default for a game (José, 3 Oct): with Clef and a window to look at; else off, quietly.
    window = not headless or mobile.is_mobile(args.project) or electron.is_electron(args.project)
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
    name = args.name or (spec.get("name") if args.suite else None) or (
        args.project if mobile.is_mobile(args.project) else Path(args.project).expanduser().resolve().name)
    emit = _printer(args) or (lambda _event: None)
    ledger = Ledger(args.cost_cap, args.usd_per_call)
    results, browser, interrupted = [], {}, False
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
            elif electron.is_electron(args.project):  # a web game in Electron (I'm Him's desktop build)
                game_cm = electron.ElectronGame(args.project, adapter=adapter, args=game_args, env=game_env,
                                                hide=[*(spec.get("hide") or [] if args.suite else []), *args.hide],
                                                allow=[*(spec.get("allow") or [] if args.suite else []), *args.allow])
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
                emit({"event": "run", "suite": f"play {name}", "run_dir": str(run_dir), "browser": browser,
                      "scenarios": len(session_steps) if session_steps else 1,
                      "decider": providers.describe(providers.resolve()).get("decider")})
                # Only a headless Godot game renders nothing to capture; Electron always has a real window
                # (its --headless only meant "no shots", so MCP's default headless runs had none).
                shots = not args.no_shots and not (headless and browser["engine"] == "godot")
                with live.frames(run_dir, getattr(game, "page_ws", None)):  # its screen, while someone watches it
                    if session_steps:
                        results = native.run_session(game, session_steps, ledger=ledger, run_dir=run_dir,
                                                     shots=shots, emit=emit, vision=sees)
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
    built = report_mod.build(SimpleNamespace(name=f"play {name}", about=run_about), results, [ledger.summary()],
                             browser=browser,
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
            print(f"{r['profile']:<16} pid {r['pid']:<7} {r['cdp_url']}  tabs={r.get('tabs')}  "
                  f"{'headless' if r['headless'] else 'headed'}  {'alive' if r['alive'] else 'DEAD'}")
    else:
        for key, value in out.items():
            print(f"{key}: {value}")
    return 0


def cmd_secret(args):
    from . import vault

    try:
        if args.action == "set":
            vault.store(args.ref)
            print(f"saved {args.ref}")
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
    from .config import DEFAULTS, env_files, load_env

    try:
        loaded = load_env(args.env_file)
    except FileNotFoundError as e:
        return _fail(args, str(e), EXIT_CONFIG)
    checks = []

    def add(name, ok, detail):
        checks.append({"check": name, "ok": ok, "detail": detail})

    add("env files", bool(loaded), ", ".join(loaded) or
        f"none found; looked in {', '.join(str(p) for p in env_files(args.env_file))}")
    for key in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "CLOUDFLARE_API_TOKEN", "TEXT_MODEL_API_KEY"):
        add(key, True, "set" if os.environ.get(key) else "not set")
    try:
        resolved = providers.resolve()
        models = providers.describe(resolved)
        add("jev route", bool(resolved["jev"]), f"{models['decider']}: {models['jev']}" if resolved["jev"] else
            "none: set TYPESAFE_API_KEY or OPENROUTER_API_KEY (Jev), or for Clef QAJEV_JEV_PROVIDER=cloudflare "
            "with CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN")
        add("text helper", True, models["text"] if resolved["text"] else
            "none: goals that type into fields need TEXT_MODEL_API_KEY or OPENROUTER_API_KEY")
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
    load1 = os.getloadavg()[0]
    add("load average", load1 < 150, f"{load1:.0f} (runs wait while >= 150)")
    ok = all(c["ok"] for c in checks)
    if args.json:
        print(json.dumps({"ok": ok, "checks": checks}, indent=2))
    else:
        for c in checks:
            print(f"{'ok ' if c['ok'] else 'NO '} {c['check']:<22} {c['detail']}")
    return 0 if ok else EXIT_CONFIG


def cmd_report(args):
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
        print(f"qajev: no {path.name} in {args.run_dir}", file=sys.stderr)
        return EXIT_CONFIG
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
    return f"{j['id']}  {j['state']:<8} {done:<8} {j['seconds']:>5}s  {j['title'][:60]}  {extra}"[:200]


def cmd_jobs(args):
    from . import jobs

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

    try:
        job = jobs.stop(args.job)
    except jobs.NoSuchJob:
        return _fail(args, f"no job {args.job}", EXIT_CONFIG)
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
            # lock still held ("another dashboard holds the lock but is not serving").
            deadline = time.monotonic() + 15
            while jobs.alive(there["pid"]) and time.monotonic() < deadline:
                time.sleep(0.1)
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
            child = subprocess.Popen([sys.executable, "-m", "qajev", "dashboard", "--port", str(args.port)],
                                     stdout=out, stderr=out, stdin=subprocess.DEVNULL, start_new_session=True)
        there = dashboard.wait_running(timeout=45, starting=child)
        if not there:
            if child.poll() is None:  # never left running half-started: it would hold the one-dashboard lock
                child.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(5)
            said = log.read_text(errors="replace")[start:].strip()[-600:] if log.exists() else ""
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
    try:
        os.nice(10)  # shared machine: the driver, its daemon and its Chrome all run below interactive work
    except OSError:
        pass


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


def _fail(args, message, code):
    _record({"error": message, "exit_code": code})
    if getattr(args, "json", False):
        print(json.dumps({"error": message, "exit_code": code}))
    print(f"qajev: {message}", file=sys.stderr)
    return code


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.argv = list(sys.argv[1:] if argv is None else argv)
    if getattr(args, "background", False):
        return cmd_background(args)
    global _job_dir
    _job_dir = None
    if args.command in {"check", "run", "smoke", "play"}:
        signal.signal(signal.SIGTERM, _interrupt)
        signal.signal(signal.SIGINT, _interrupt)
        if not os.environ.get("QAJEV_JOB"):  # a background job's run is already recorded by its wrapper
            from . import jobs

            _job_dir = jobs.register(args.argv, rerun=_rerun)
    handlers = {"check": cmd_run, "run": cmd_run, "smoke": cmd_smoke, "browser": cmd_browser, "doctor": cmd_doctor,
                "secret": cmd_secret, "account": cmd_account,
                "report": cmd_report, "init": cmd_init, "mcp": cmd_mcp, "projects": cmd_projects,
                "reports": cmd_reports, "jobs": cmd_jobs, "stop": cmd_stop,
                "top": cmd_top, "nightly": cmd_nightly, "rerun": cmd_rerun, "dashboard": cmd_dashboard,
                "play": cmd_play}
    code = 1  # a crash on the way out still leaves an exit code for `qajev jobs`
    try:
        code = handlers[args.command](args)
        return code
    except BrokenPipeError:
        code = 0
        return 0
    except KeyboardInterrupt:
        code = EXIT_INTERRUPTED
        if args.command in {"check", "run", "smoke", "play"}:  # stopped before its handler could (still importing)
            return code
        raise
    finally:
        if _job_dir is not None:
            with contextlib.suppress(OSError):
                (_job_dir / "exit_code").write_text(f"{code}\n")
