"""QAJev as an MCP server (stdio). Each browser run is a QAJev job: the CLI in its own process.

A process per run keeps every run isolated: its own browser_harness daemon (BU_NAME is read at import),
its own crash domain, and a clean stop on cancel. As a job it is visible to every agent and terminal on the
machine (qa_jobs, `qajev jobs`, `qajev top`) and stoppable from any of them. Only one browser run executes
at a time on the whole machine (a lock shared with the CLI); later runs queue.
"""

import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import yaml
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__, config, jobs
from .config import HOME

INSTRUCTIONS = """\
QAJev drives a real Chrome with Jev (TypeSafe) to QA websites. Use it to check that a person could do
something on a site, and to catch errors a browser can see.

- qa_smoke first: crawls same-origin pages with no model calls (free) and lists HTTP errors, script
  errors, broken images and links, accessibility and SEO gaps.
- qa_check for one goal: say what a user wants in plain words, ending with "Stop when ...", and ALWAYS
  give expectations (expect_text / expect_url / expect_js). Jev's own DONE is only a hint; the verdict
  comes from those checks.
- qa_run_suite for many scenarios (YAML suite; see qajev init). Lint a suite with qa_plan first (free, runs
  nothing): it lists the tests that do not say what they prove, whose runs could never PASS.
- qa_project_run to prove a product: a project's stored objectives (qa_projects lists them) or one ad-hoc
  objective; reports are filed per project and listed by qa_reports.
- Long runs: pass background=true to get a job id at once, then qa_job(job) for progress and partial results,
  qa_stop(job) to stop it. qa_jobs lists every QAJev run on the machine (any agent's), queued or running.
  Only one browser run executes at a time machine-wide; the others queue.
- Specific tests: qa_run_suite(only=[...]) and qa_play(suite=..., only=[...]) run chosen scenarios or game steps
  (with what they depend on); qa_rerun(job) runs a finished job's failed, stuck and harness tests again.
- Where it runs: runner="cloud" sends qa_check, qa_run_suite, qa_smoke and qa_project_run to QAJev Cloud instead of
  this machine; runner="auto" uses the cloud when it can, else this machine (the result's runner_note says why). A
  cloud run never falls back to this machine. A signed-in profile stays here, and a site on this machine's 127.0.0.1
  needs its built folder shipped with serve="DIR[:PORT]". A cloud run's id starts with r_; qa_job and qa_stop take it.
  The default is QAJEV_RUNNER, else local; qa_doctor shows it and whether the cloud key is found.
- Every run writes report.html for the person (its path is in the result as report_html).
- ALWAYS say what each test proves and why, in plain words: `about` on qa_check, qa_play and qa_project_run, and
  `about:` on each scenario or step of a suite (and on the suite). The person reads it next to the result, so a
  pass means something. A result lists the tests without one under `about_missing`.

Outcomes: pass; fail (product wrong); stuck (Jev found no way forward: verify by hand, often UX);
harness (budget/stale/model/browser trouble, Jev said DONE without a single action, a local server not answering,
or a js check whose own code broke: says nothing about the product); unverified (no checks);
skipped. Gate: PASS, FAIL or INCOMPLETE. A planted fault (expect_fail: the says text of the check it must fail on)
keeps its outcome; the gate counts its `graded`: met only when it failed on that check, else FAIL.

Safety built in: production is read-only and destructive is never. Any host that is not a local dev host
(localhost, 127.0.0.1, [::1], *.localhost, *.test) is production: mode="mutate" or allow_destructive there is
refused before Chrome starts (outcome "refused"; do not retry it another way). On production DELETE/PUT/PATCH never
leave the page, a POST only when the suite's allow_requests names it (listed in the report), and the page's
confirm/prompt are answered "no". Destructive controls (delete, remove, refund, cancel, archive, reset...) are
hidden everywhere; allow_destructive=true shows them on a local dev host only. Dangerous controls (sign out,
billing, pay, revoke, close all...) are hidden from Jev, passwords/payment fields are disabled, the microphone
is stubbed. Sign-in: qa_browser(action="login", url=...) opens
QAJev's own Chrome for the person, who signs in once (the profile keeps it); or, for a seeded TEST account on a
local dev host only, a suite/project names it (account: with password: seed:FILE#KEY, and totp: or a cookie:
value from seed: for a second factor, from a fixture that says "test_account": true) and QAJev signs in itself.
keychain:, op:// and env: passwords are refused, and so is a URL with a user name or password in it
(https://user:pass@host/). Never ask for, type or write down a password.

Images: with Clef as the decision model (QAJEV_JEV_PROVIDER=cloudflare), `expect_looks` judges plain statements
from the final screenshot (layout, a canvas, a cut-off button: what text checks cannot see), and `vision` sends the
screenshot with every decision. Jev reads text only; asking for either without Clef is refused.

Needs sign-in: when a result has `needs_sign_in`, runs met a sign-in page (those scenarios are harness, not product
failures). Do not report it as a bug. Ask the person to sign in once (qa_browser login), following its
`next_step`; a local dev site can use a seeded test account instead. Then run again.
"""

server = MCPServer(name="qajev", version=__version__, instructions=INSTRUCTIONS)
_allow_commands = False
DEFAULT_OUT = Path(os.environ.get("QAJEV_OUT", HOME / "runs"))


def _cpu_args(cpu_throttle):
    """cpu_throttle: run the page's CPU N times slower (1 to 8), e.g. 4 for a phone-like phone pass."""
    if cpu_throttle in (None, 0, 1):
        return []
    if isinstance(cpu_throttle, bool) or not isinstance(cpu_throttle, int) or not 1 <= cpu_throttle <= 8:
        raise ToolError("cpu_throttle must be a whole number from 1 to 8")
    return ["--cpu-throttle", str(cpu_throttle)]


def _scheme_args(color_scheme):
    """light | dark: the page's prefers-color-scheme answers so, for each test that names none."""
    if color_scheme in (None, ""):
        return []
    if color_scheme not in ("light", "dark"):
        raise ToolError("color_scheme must be light or dark")
    return ["--color-scheme", color_scheme]


def _motion_args(motion):
    """reduce (default): pages are told the visitor prefers reduced motion; full: as a default browser."""
    if motion in (None, ""):
        return []
    if motion not in ("reduce", "full"):
        raise ToolError("motion must be reduce or full")
    return ["--motion", motion]


def _browser_args(profile, cdp_url, headless):
    """Agents get a headless throwaway Chrome by default: nothing appears on the person's screen and nothing is
    left behind. Name a `profile` for signed-in runs (the person signs in once with qa_browser login)."""
    if cdp_url:
        return ["--cdp-url", cdp_url]
    args = ["--profile", profile] if profile else ["--ephemeral"]
    if headless:
        args.append("--headless")
    return args


async def _spawn(args, ctx=None):
    """Run `python -m qajev ARGS`; relay JSON-lines events as MCP progress. -> (exit code, stdout, stderr tail)."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "qajev", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL,
    )
    assert proc.stdout is not None and proc.stderr is not None
    stderr = proc.stderr
    tail, counts = [], {"done": 0, "total": None}

    async def pump():
        async for raw in stderr:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                tail.append(line)
                del tail[:-20]
                continue
            if ctx is not None:
                await _progress(ctx, event, counts)

    try:
        stdout, _ = await asyncio.gather(proc.stdout.read(), pump())
        await proc.wait()
    except asyncio.CancelledError:
        proc.terminate()  # the child closes its own tabs and daemon on SIGTERM, exactly like Ctrl-C
        try:
            await asyncio.wait_for(proc.wait(), 20)
        except asyncio.TimeoutError:
            proc.kill()
        raise
    return proc.returncode, stdout.decode(errors="replace").strip(), tail


async def _progress(ctx, event, counts):
    """Relay one run event as MCP progress."""
    kind = event.get("event")
    if kind == "run":
        counts["total"] = event.get("scenarios")
        await ctx.report_progress(0, counts["total"], f"qajev: {event.get('suite')} -> {event.get('run_dir')}")
    elif kind == "start":
        await ctx.report_progress(counts["done"], counts["total"], f"running {event['scenario']}")
    elif kind == "scenario":
        counts["done"] += 1
        r = event["result"]
        await ctx.report_progress(counts["done"], counts["total"], f"{r['outcome']}: {r['name']}")
    elif kind == "waiting":
        await ctx.report_progress(counts["done"], counts["total"], event.get("reason", "waiting"))
    elif kind == "cloud" and event.get("gate"):  # a cloud run: its gate, progress, or why it waits in the queue
        why = (event.get("waiting") or {}).get("text")
        detail = why or " · ".join(str(x) for x in (event.get("progress"), event.get("now_test")) if x)
        message = f"cloud {event['gate']}" + (f": {detail}" if detail else "")
        await ctx.report_progress(counts["done"], counts["total"], message)
    elif kind == "step":  # what Jev is doing, and the money spent so far
        spent = f" · ${event['spent_usd']:.4f}" if isinstance(event.get("spent_usd"), (int, float)) else ""
        await ctx.report_progress(counts["done"], counts["total"],
                                  f"{event.get('scenario')}: {event.get('doing')}{spent}")


def _refuse_user_info(argv):
    """A ToolError for a URL argument with a user name or password (cli.url_refusal), before anything keeps it."""
    from .cli import build_parser, url_refusal

    try:
        with contextlib.redirect_stderr(io.StringIO()):
            parsed = build_parser().parse_args(argv)
        parsed.argv = list(argv)
        refused = url_refusal(parsed)
    except SystemExit:  # arguments the CLI cannot read: it refuses them itself, and the "://" ones are checked here
        refused = config.user_info_in_args(argv)
    if refused:
        raise ToolError(refused)


async def _run_report(args, ctx, background=False, title=None, cwd=None, rerun=None):
    """A browser run as a QAJev job. background: return the job at once. Otherwise wait for it (relaying its
    progress) and return the report JSON; cancelling the call stops the job. cwd: where it runs (default: here);
    rerun: the job it reruns (jobs.start)."""
    _refuse_user_info(args)  # before the job record keeps the arguments as typed
    job = await asyncio.to_thread(jobs.start, args, title, cwd=cwd, rerun=rerun)
    if background:
        return {"job": job["id"], "state": job["state"], "title": job["title"],
                "next": "qa_job(job) for progress and partial results; qa_stop(job) to stop it"}
    seen, counts = 0, {"done": 0, "total": None}
    try:
        while True:
            events, seen = jobs.events_since(job["id"], seen)
            for event in events:
                await _progress(ctx, event, counts)
            data = jobs.result(job["id"])
            if data is not None:
                break
            if not await asyncio.to_thread(jobs.alive, job["pid"]):
                data = jobs.result(job["id"]) or {}
                break
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        with contextlib.suppress(jobs.NotOurs):  # the cancel goes on either way
            await asyncio.to_thread(jobs.stop, job["id"])  # the run closes its own tabs, Chrome and daemon
        raise
    if data and data.get("outcome") == "refused":  # it would have changed a production site: nothing ran
        return {**data, "job": job["id"]}
    if not data or "error" in data:
        st = jobs.status(job["id"])
        raise ToolError(data.get("error") or st.get("error") or f"qajev job {job['id']} ended {st['state']}")
    data["job"] = job["id"]
    return data


def _runner_args(runner, serve, model):
    if runner not in (None, "", "local", "cloud", "auto"):
        raise ToolError("runner must be local, cloud or auto")
    return [*(["--runner", runner] if runner else []), *(["--serve", serve] if serve else []),
            *(["--model", model] if model else [])]


async def _dispatch(args, where, ctx, background, title=None):
    """A browser run where `where` (the --runner, --serve and --model flags) and the defaults send it: this machine
    as a QAJev job (_run_report, `args` unchanged), or QAJev Cloud (_cloud_report). A refusal is a ToolError."""
    from . import cloud
    from .cli import build_parser

    _refuse_user_info([*args, *where])  # before a job record or the cloud keeps the arguments as typed
    try:
        parsed = build_parser().parse_args([*args, *where])
    except SystemExit:
        shown = [a if "@" not in str(a) else "…" for a in [*args, *where]]  # never a URL's user info
        raise ToolError(f"qajev cannot read these arguments: {shown}") from None
    parsed.argv = [*args, *where]
    note = None
    try:
        runner, source = cloud.runner_for(parsed)
        if runner != "local":
            parsed.runner_named = source == "--runner"  # the tool's `runner` argument, as the CLI's --runner
            try:
                plan = await asyncio.to_thread(cloud.plan, parsed)
            except cloud.NotAllowed as e:
                if runner == "cloud":
                    raise ToolError(f"runner cloud{'' if source == '--runner' else f' ({source})'}: {e}") from None
                note = f"runner auto: this run stays on this machine: {e}"
            else:
                return await _cloud_report(plan, ctx, background)
    except cloud.CloudError as e:
        raise ToolError(str(e) + (f" ({e.url})" if e.url and e.url not in str(e) else "")) from None
    if parsed.serve or parsed.model:
        raise ToolError("serve and model go with cloud runs, and this one runs on this machine"
                        + (f" ({note.split(': ', 1)[1]})" if note else ""))
    report = await _run_report(args, ctx, background, title)
    if note:
        report["runner_note"] = note
    return report


async def _cloud_report(plan, ctx, background):
    """Send the run to QAJev Cloud. background: its id and address at once. Otherwise follow it (relaying its progress)
    to its gate; cancelling the call stops the cloud run."""
    from . import cloud

    client = await asyncio.to_thread(cloud.connect)
    run_id = await asyncio.to_thread(cloud.submit, client, plan)
    url = cloud.run_url(client.base, run_id)
    if background:
        return {"runner": "cloud", "job": run_id, "run_id": run_id, "url": url, "state": "queued",
                "warnings": plan.warnings, "next": "qa_job(job) for progress and the result; qa_stop(job) to stop it"}
    if ctx is not None:
        await ctx.report_progress(0, None, f"cloud run {run_id}: {url}")
    deadline, last, misses = time.monotonic() + cloud.wait_seconds(), None, 0
    try:
        while True:
            try:
                run = await asyncio.to_thread(cloud.status, client, run_id)
                misses = 0
            except cloud.CloudError as e:
                misses += 1
                if misses >= 3:
                    raise ToolError(f"{e} ({url})") from None
                await asyncio.sleep(cloud.POLL_S)
                continue
            seen = (run.get("gate"), run.get("progress"), run.get("now_test"))
            if ctx is not None and seen != last:
                await ctx.report_progress(0, None, " · ".join(str(x) for x in seen if x))
            last = seen
            if cloud.done(run):
                return {**cloud.result(client.base, run), **({"warnings": plan.warnings} if plan.warnings else {})}
            if time.monotonic() >= deadline:
                raise ToolError(f"waited {cloud.wait_seconds() / 60:.0f} min and the run has not finished; it goes on "
                                f"in the cloud: qa_job({run_id!r}) follows it ({url})")
            await asyncio.sleep(cloud.POLL_S)
    except asyncio.CancelledError:
        with contextlib.suppress(cloud.CloudError):
            await asyncio.to_thread(cloud.stop, client, run_id)
        raise


async def _cloud_job(run_id, stopping=False):
    from . import cloud

    try:
        client = await asyncio.to_thread(cloud.connect)
        if stopping:
            await asyncio.to_thread(cloud.stop, client, run_id)
        run = await asyncio.to_thread(cloud.status, client, run_id)
    except cloud.CloudError as e:
        raise ToolError(str(e)) from None
    out = cloud.result(client.base, run)
    out["state"] = run.get("gate")
    for key in ("progress", "now_test", "now_step"):
        if run.get(key):
            out[key] = run[key]
    return out


def _trim(report, verbose):
    """Keep tool results small enough for a model's context; the full report stays on disk."""
    if verbose or "scenarios" not in report:
        return report
    keep = ("name", "about", "outcome", "reason", "expected", "expected_check", "graded", "graded_reason", "stop",
            "stop_detail", "not_run", "seconds", "cost_usd", "end_url",
            "checks", "findings", "shot", "jev", "blocked_writes", "allowed_writes", "dialogs", "page_says",
            "needs_sign_in", "block_list_requests", "frames_not_observed", "frames_accepted", "frame_console",
            "frame_requests")
    out = {k: v for k, v in report.items() if k not in ("scenarios", "plan")}  # the plan repeats the scenarios
    out["scenarios"] = [{k: r.get(k) for k in keep if r.get(k) not in (None, [], {})} for r in report["scenarios"]]
    for r in out["scenarios"]:
        if r.get("blocked_writes"):
            r["blocked_writes"] = len(r["blocked_writes"])
    missing = report.get("not_described") or [r["name"] for r in report["scenarios"] if not r.get("about")]
    if missing:  # the person reads what each test proves next to its result: ask for it every time
        out["about_missing"] = {"tests": missing[:20], "next": "Next time give each test an about: what it proves "
                                "and why, in plain words (about= on qa_check/qa_play/qa_project_run, about: on a "
                                "suite's scenarios or steps), and each js, url_regex, fetch or command check its "
                                "words (says: on the suite's expect, or says= with expect_js on qa_check). Until "
                                "then the gate is INCOMPLETE, never PASS."}
    out["report_md"] = str(Path(report["run_dir"]) / "report.md")
    out["report_html"] = str(Path(report["run_dir"]) / "report.html")
    return out


def _plan_args(plan):
    """--plan: the approved test plan (planfile.py) a run follows."""
    return ["--plan", str(Path(plan).expanduser())] if plan else []


def _devices_args(devices, real_devices=None):
    """Every website test runs on desktop and on a phone by default; `devices` narrows or widens that.
    `real_devices` (opt-in) also runs it in a real device browser: "android" (Chrome), "ios" (Safari: page content
    not readable yet)."""
    return ([*(["--devices", ",".join(devices)] if devices else []),
             *(["--real-devices", ",".join(real_devices)] if real_devices else [])])


@server.tool()
async def qa_check(
    url: str,
    ctx: Context,
    goal: str | None = None,
    expect_text: list[str] | None = None,
    absent_text: list[str] | None = None,
    expect_url: str | None = None,
    expect_js: str | None = None,
    says: str | None = None,
    expect_looks: list[str] | None = None,
    vision: bool = False,
    fetch: list[str] | None = None,
    mode: str = "readonly",
    device: str | None = None,
    persona: str | None = None,
    about: str | None = None,
    expect_fail: str | bool | None = None,
    hosts: list[str] | None = None,
    max_actions: int = 20,
    max_seconds: float = 90,
    settle: float = 10,
    profile: str | None = None,
    cdp_url: str | None = None,
    headless: bool = True,
    cost_cap: float | None = None,
    out_dir: str | None = None,
    verbose: bool = False,
    background: bool = False,
    devices: list[str] | None = None,
    real_devices: list[str] | None = None,
    motion: str | None = None,
    color_scheme: str | None = None,
    cpu_throttle: int | None = None,
    allow_destructive: bool = False,
    plan: str | None = None,
    runner: str | None = None,
    serve: str | None = None,
    model: str | None = None,
) -> dict:
    """Run one QA scenario: open `url`, optionally let Jev pursue `goal`, then judge the page.

    goal: plain words, e.g. "Open the pricing page. Stop when plan prices are visible."
    expect_text / absent_text: case-sensitive substrings the page must / must not show.
    expect_url: substring of the final URL. expect_js: JS expression that must return exactly true (a string it
    returns fails, and is the reason). says: what expect_js proves, in plain words (the report's test plan;
    without it the check is NOT DESCRIBED and the gate INCOMPLETE).
    expect_looks: statements judged from the final screenshot ("the Sign up button is not cut off"); vision: the
    screenshot goes with every decision. Both need Clef as the decision model (Jev reads text only).
    fetch: in-page GET checks, "URL" or "URL=STATUS". mode: readonly (default) or mutate (a local dev host only;
    any other host is refused). allow_destructive: show delete/remove/refund/cancel... controls (local dev host only).
    device: desktop | tall | phone | tablet | WIDTHxHEIGHT pins one; otherwise `devices` (default desktop and
    phone). real_devices: also in ios Safari / android Chrome on a throwaway simulator (read-only, slower).
    about: what this test proves and why, in plain words, for the person reading the report (always give it).
    expect_fail: a planted fault: the says text of the one check it must fail on (true: the test's only check). It
    counts as met only when it fails there; a pass, or a failure on another check, turns the gate FAIL.
    Returns the report with a gate and findings.
    background: return a job id at once instead (follow with qa_job, stop with qa_stop).
    runner: where the run goes: "local" (this machine), "cloud" (QAJev Cloud: refused with the reason when it cannot go,
    never run here instead) or "auto" (the cloud when it can; a signed-in profile or a run that writes to a dev server
    here runs here, and runner_note says why; a read-only 127.0.0.1 run goes through the read-only tunnel, and a suite
    or project env with tunnel: through the full tunnel). Default: QAJEV_RUNNER, else local. serve: with a cloud run, a
    built site folder, "DIR" or "DIR:PORT", that the cloud machine serves on its own 127.0.0.1:PORT. model: the cloud
    run's decision model (clef, clef-flash, clef-omni, jev, jev-openrouter; default the dashboard's). A cloud run in the
    background returns its id (r_...) and address at once.
    plan: the approved test plan this run follows (a Markdown file; overrides a suite's `plan:`). Without an approved
    one the run warns, and with QAJEV_REQUIRE_PLAN=1 it is refused (see qa_plan).
    """
    where = _runner_args(runner, serve, model)
    args = ["check", url, "--mode", mode, *(["--device", device] if device else []), "--max-actions",
            str(max_actions),
            "--max-seconds", str(max_seconds), "--settle", str(settle), "--out", out_dir or str(DEFAULT_OUT),
            *_browser_args(profile, cdp_url, headless)]
    if goal:
        args += ["--goal", goal]
    for text in expect_text or []:
        args += ["--expect-text", text]
    for text in absent_text or []:
        args += ["--absent", text]
    if expect_url:
        args += ["--expect-url", expect_url]
    if expect_js:
        args += ["--expect-js", expect_js, *(["--says", says] if says else [])]
    for statement in expect_looks or []:
        args += ["--expect-looks", statement]
    if vision:
        args.append("--vision")
    for probe in fetch or []:
        args += ["--fetch", probe]
    if persona:
        args += ["--persona", persona]
    if about:
        args += ["--about", about]
    if expect_fail:
        args += ["--expect-fail", *([] if expect_fail is True else [str(expect_fail)])]
    for host in hosts or []:
        args += ["--host", host]
    if cost_cap is not None:
        args += ["--cost-cap", str(cost_cap)]
    if allow_destructive:
        args.append("--allow-destructive")
    args += _motion_args(motion)
    args += _scheme_args(color_scheme)
    args += _cpu_args(cpu_throttle)
    args += _devices_args(devices, real_devices)
    args += _plan_args(plan)
    return _trim(await _dispatch(args, where, ctx, background), verbose)


@server.tool()
async def qa_run_suite(
    ctx: Context,
    suite_path: str | None = None,
    suite_yaml: str | None = None,
    only: list[str] | None = None,
    jobs: int = 1,
    profile: str | None = None,
    cdp_url: str | None = None,
    headless: bool = True,
    cost_cap: float | None = None,
    out_dir: str | None = None,
    verbose: bool = False,
    background: bool = False,
    devices: list[str] | None = None,
    real_devices: list[str] | None = None,
    motion: str | None = None,
    color_scheme: str | None = None,
    cpu_throttle: int | None = None,
    allow_destructive: bool = False,
    plan: str | None = None,
    runner: str | None = None,
    serve: str | None = None,
    model: str | None = None,
) -> dict:
    """Run a QAJev suite (YAML/JSON) from a file path or inline text. `only` limits to named scenarios
    (plus their dependencies). jobs > 1 runs independent chains in parallel; keep 1 on a busy machine.
    allow_destructive: show delete/remove/refund/cancel... controls (every host a local dev host, or refused).
    background: return a job id at once instead (follow with qa_job, stop with qa_stop).
    runner: where the run goes: "local" (this machine), "cloud" (QAJev Cloud: refused with the reason when it cannot go,
    never run here instead) or "auto" (the cloud when it can; a signed-in profile or a run that writes to a dev server
    here runs here, and runner_note says why; a read-only 127.0.0.1 run goes through the read-only tunnel, and a suite
    or project env with tunnel: through the full tunnel). Default: QAJEV_RUNNER, else local. serve: with a cloud run, a
    built site folder, "DIR" or "DIR:PORT", that the cloud machine serves on its own 127.0.0.1:PORT. model: the cloud
    run's decision model (clef, clef-flash, clef-omni, jev, jev-openrouter; default the dashboard's). A cloud run in the
    background returns its id (r_...) and address at once.
    plan: the approved test plan this run follows (a Markdown file; overrides a suite's `plan:`). Without an approved
    one the run warns, and with QAJEV_REQUIRE_PLAN=1 it is refused (see qa_plan).
    """
    where = _runner_args(runner, serve, model)
    if bool(suite_path) == bool(suite_yaml):
        raise ToolError("give exactly one of suite_path or suite_yaml")
    if suite_yaml:
        folder = HOME / "mcp-suites"
        folder.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), suffix=".yaml", dir=folder)
        with os.fdopen(fd, "w") as f:
            f.write(suite_yaml)
        suite_path = name
    title = None
    if suite_yaml:
        try:
            title = f"run suite {(yaml.safe_load(suite_yaml) or {}).get('name') or 'inline'}"
        except yaml.YAMLError:
            title = "run suite (inline)"
    args = ["run", suite_path, "--jobs", str(jobs), "--out", out_dir or str(DEFAULT_OUT),
            *_browser_args(profile, cdp_url, headless)]
    for name in only or []:
        args += ["--only", name]
    if cost_cap is not None:
        args += ["--cost-cap", str(cost_cap)]
    if _allow_commands:
        args.append("--allow-commands")
    if allow_destructive:
        args.append("--allow-destructive")
    args += _motion_args(motion)
    args += _scheme_args(color_scheme)
    args += _cpu_args(cpu_throttle)
    args += _devices_args(devices, real_devices)
    args += _plan_args(plan)
    return _trim(await _dispatch(args, where, ctx, background, title), verbose)


@server.tool()
async def qa_smoke(
    url: str,
    ctx: Context,
    max_pages: int = 20,
    device: str | None = None,
    check_links: bool = False,
    profile: str | None = None,
    cdp_url: str | None = None,
    headless: bool = True,
    out_dir: str | None = None,
    verbose: bool = False,
    background: bool = False,
    devices: list[str] | None = None,
    motion: str | None = None,
    color_scheme: str | None = None,
    cpu_throttle: int | None = None,
    ux: bool = True,
    plan: str | None = None,
    runner: str | None = None,
    serve: str | None = None,
    model: str | None = None,
) -> dict:
    """Crawl same-origin pages from `url` with NO model calls and lint each one: HTTP status, script
    errors, failed requests, CSP blocks and report-only violations, broken images, unlabeled fields,
    unnamed buttons, overflow, SEO basics. ux (default on, free): measured UX notes per page (WCAG AA
    contrast, keyboard reach and visible focus, 200% zoom, cut-off or overlapping text, open dialogs) and
    a design-consistency comparison across pages; they never change the gate.
    background: return a job id at once instead (follow with qa_job, stop with qa_stop).
    runner: where the run goes: "local" (this machine), "cloud" (QAJev Cloud: refused with the reason when it cannot go,
    never run here instead) or "auto" (the cloud when it can; a signed-in profile or a run that writes to a dev server
    here runs here, and runner_note says why; a read-only 127.0.0.1 run goes through the read-only tunnel, and a suite
    or project env with tunnel: through the full tunnel). Default: QAJEV_RUNNER, else local. serve: with a cloud run, a
    built site folder, "DIR" or "DIR:PORT", that the cloud machine serves on its own 127.0.0.1:PORT. model: the cloud
    run's decision model (clef, clef-flash, clef-omni, jev, jev-openrouter; default the dashboard's). A cloud run in the
    background returns its id (r_...) and address at once.
    plan: the approved test plan this run follows (a Markdown file; overrides a suite's `plan:`). Without an approved
    one the run warns, and with QAJEV_REQUIRE_PLAN=1 it is refused (see qa_plan).
    """
    where = _runner_args(runner, serve, model)
    args = ["smoke", url, "--max-pages", str(max_pages), *(["--device", device] if device else []),
            "--out", out_dir or str(DEFAULT_OUT),
            *_browser_args(profile, cdp_url, headless)]
    if check_links:
        args.append("--check-links")
    if not ux:
        args.append("--no-ux")
    args += _motion_args(motion)
    args += _scheme_args(color_scheme)
    args += _cpu_args(cpu_throttle)
    args += _devices_args(devices)
    args += _plan_args(plan)
    return _trim(await _dispatch(args, where, ctx, background), verbose)


@server.tool()
async def qa_browser(action: str, profile: str = "default", url: str | None = None, headless: bool = False) -> dict:
    """Manage QAJev's own Chrome. action: status | start | stop | login | reap. `login` opens a visible window on
    `url` so the PERSON can sign in once; the profile keeps the session. Never type credentials yourself."""
    if action not in {"status", "start", "stop", "login", "reap"}:
        raise ToolError("action must be status, start, stop, login or reap")
    args = ["browser", action, "--profile", profile, "--json"]
    if url:
        args += ["--url", url]
    if headless:
        args.append("--headless")
    code, text, tail = await _spawn(args)
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError:
        data = {"output": text}
    if code != 0:
        raise ToolError(data.get("error") if isinstance(data, dict) else " | ".join(tail[-5:]))
    return {"result": data}


@server.tool()
async def qa_project_run(
    project: str,
    ctx: Context,
    objective: str | None = None,
    about: str | None = None,
    suite: str | None = None,
    names: list[str] | None = None,
    env: str | None = None,
    url: str | None = None,
    expect_text: list[str] | None = None,
    expect_url: str | None = None,
    profile: str | None = None,
    headless: bool = True,
    verbose: bool = False,
    background: bool = False,
    devices: list[str] | None = None,
    real_devices: list[str] | None = None,
    motion: str | None = None,
    color_scheme: str | None = None,
    cpu_throttle: int | None = None,
    allow_destructive: bool = False,
    plan: str | None = None,
    runner: str | None = None,
    serve: str | None = None,
    model: str | None = None,
) -> dict:
    """Prove a product works: run a project's stored objectives (optionally only those tagged `suite`, or named
    in `names`), or one ad-hoc `objective` in plain words with `expect_text`/`expect_url` checks and an `about` (what
    it proves and why). The report is
    filed with the project and in the cross-project index. Production environments are always read-only: an env
    with mode = "mutate" on a production host is refused. allow_destructive: show delete/remove/refund/cancel...
    controls (a local dev env only).
    background: return a job id at once instead (follow with qa_job, stop with qa_stop).
    runner: where the run goes: "local" (this machine), "cloud" (QAJev Cloud: refused with the reason when it cannot go,
    never run here instead) or "auto" (the cloud when it can; a signed-in profile or a run that writes to a dev server
    here runs here, and runner_note says why; a read-only 127.0.0.1 run goes through the read-only tunnel, and a suite
    or project env with tunnel: through the full tunnel). Default: QAJEV_RUNNER, else local. serve: with a cloud run, a
    built site folder, "DIR" or "DIR:PORT", that the cloud machine serves on its own 127.0.0.1:PORT. model: the cloud
    run's decision model (clef, clef-flash, clef-omni, jev, jev-openrouter; default the dashboard's). A cloud run in the
    background returns its id (r_...) and address at once.
    plan: the approved test plan this run follows (a Markdown file; overrides a suite's `plan:`). Without an approved
    one the run warns, and with QAJEV_REQUIRE_PLAN=1 it is refused (see qa_plan).
    Without runner, the project's `runner` in project.toml counts after QAJEV_RUNNER.
    """
    where = _runner_args(runner, serve, model)
    args = ["run", "--project", project, *_browser_args(profile, None, headless)]
    for flag, value in (("--env", env), ("--objective", objective), ("--suite", suite), ("--url", url),
                        ("--expect-url", expect_url), ("--about", about)):
        if value:
            args += [flag, value]
    for name in names or []:
        args += ["--name", name]
    for text in expect_text or []:
        args += ["--expect-text", text]
    if allow_destructive:
        args.append("--allow-destructive")
    args += _motion_args(motion)
    args += _scheme_args(color_scheme)
    args += _cpu_args(cpu_throttle)
    args += _devices_args(devices, real_devices)
    args += _plan_args(plan)
    return _trim(await _dispatch(args, where, ctx, background), verbose)


@server.tool()
async def qa_projects() -> dict:
    """The projects QAJev knows: their environments, how many objectives, and where their reports go."""
    code, text, tail = await _spawn(["projects", "--json"])
    return {"projects": json.loads(text or "[]")}


@server.tool()
async def qa_reports(project: str | None = None, limit: int = 20) -> dict:
    """Recent project runs, newest first: gate, outcome per objective, cost, duration and the report path."""
    args = ["reports", "--json", "--limit", str(limit)] + (["--project", project] if project else [])
    code, text, tail = await _spawn(args)
    return {"runs": json.loads(text or "[]")}


@server.tool()
async def qa_plan(suite: str) -> dict:
    """A suite's test plan without running it (free, no browser): each test's about and its checks in plain words,
    and `not_described`, the tests that do not say what they prove (a run would be INCOMPLETE), and `absent_only`, the
    tests whose only checks say a text is absent (a blank page passes them). `suite`: a suite file
    (scenarios:) or a game's steps suite (steps:). Lint a suite with it before running or sending a PR.
    `test_plan`: the approved test plan file the suite names (`plan:`): its path, whether it is approved, the
    approval line, the sha256 of the plan text above that line, and the problem when it is not approved."""
    code, text, tail = await _spawn(["plan", str(Path(suite).expanduser()), "--json"])
    data = json.loads(text or "{}")
    if code not in (0, 2) or "plan" not in data:
        raise ToolError(data.get("error") or tail or f"qajev plan exited {code}")
    return data


@server.tool()
async def qa_report(run_dir: str, markdown: bool = False) -> dict:
    """Read a finished run's report (JSON by default, or the Markdown text). A cloud run's id (r_...) fetches its report
    and the screenshots it names with this machine's cloud key, and keeps them in ~/.qajev/cloud-reports/<id>."""
    from . import cloud

    if not Path(run_dir).expanduser().exists() and cloud.RUN_ID.match(run_dir):
        from .config import HOME

        folder = HOME / "cloud-reports" / run_dir
        try:
            data, shots = cloud.fetch_report(cloud.connect(), run_dir, folder)
        except cloud.ApiError as e:
            raise ToolError(f"cloud run {run_dir} has no report: no such run, or it has not finished" if e.status == 404
                            else str(e)) from None
        except (cloud.CloudError, ValueError) as e:
            raise ToolError(str(e)) from None
        kept = {"kept_in": str(folder), "screenshots": shots}
        md = folder / "report.md"
        return {"markdown": md.read_text(), **kept} if markdown and md.is_file() else {**data, **kept}
    folder = Path(run_dir).expanduser()
    path = folder / ("report.md" if markdown else "report.json")
    if not path.is_file():
        raise ToolError(f"no {path.name} in {folder}")
    return {"markdown": path.read_text()} if markdown else json.loads(path.read_text())


@server.tool()
async def qa_play(
    project: str,
    ctx: Context,
    goal: str | None = None,
    adapter: str | None = None,
    suite: str | None = None,
    only: list[str] | None = None,
    game_env: dict | None = None,
    game_args: list[str] | None = None,
    game_profile: str | None = None,
    reset_game_profile: bool = False,
    expect_screen: str | None = None,
    expect_text: list[str] | None = None,
    expect_state: dict | None = None,
    min_fps: float | None = None,
    allow_errors: bool = False,
    expect_closed: bool = False,
    expect_looks: list[str] | None = None,
    about: str | None = None,
    vision: bool | None = None,
    allow: list[str] | None = None,
    hide: list[str] | None = None,
    name: str | None = None,
    max_actions: int = 20,
    max_seconds: float = 90,
    headless: bool = True,
    shots: bool = True,
    cost_cap: float | None = None,
    out_dir: str | None = None,
    verbose: bool = False,
    background: bool = False,
    plan: str | None = None,
) -> dict:
    """Native: QA a game or a mobile app through QAJev's bridge: a Godot project folder, an Electron .app or project
    folder, or a mobile target (ios:<bundle id or URL>, android:<package or URL>). Jev plays the UI a player uses
    (menus, start, pause, choices), reading each screen as text; input goes into the game only, and the game gets a
    throwaway save folder. `adapter`: a bundled name (suho, hypervolley, imhim) or a path, for games that draw their
    own UI. `suite`: a YAML file of steps in one session: goal steps, real-time play steps (`play:`, needs the
    game's bot), idle steps (`idle: SECONDS`, the game runs untouched), with top-level `allow`/`hide` labels. A step's
    `before:` (screen/text/state) must hold when it starts; an idle step's `lasted: {min, max}` measures how long that
    state held with no input. A `pad:` step (Electron) presses buttons, pushes a stick or pulls a trigger on a
    virtual gamepad (`pad: A`, `{press: [A, DpadDown]}`, `{stick: left, x: 1}`, `{trigger: RT}`). A `handoff:` step
    ({who, does, until, minutes}) waits, sending no input, for a person to do a step by hand (a personal sign-in):
    pass when `until` holds, stuck when time runs out.
    `only`: run these steps of the suite, plus the steps they name in `depends_on` and every `setup: true` step.
    `game_env`: environment settings for the game; `game_args`: switches for an Electron app. `game_profile`: keep
    an Electron game's save folder between runs (~/.qajev/game-profiles/NAME, a test profile; reset_game_profile
    empties it first); default: a throwaway deleted at close. expect_state: game
    state values, e.g. {"game_over": false, "kills": ">= 1"}. Quit, exit and delete-save buttons are hidden from Jev;
    to test a normal quit pass allow=["QUIT"] and expect_closed=true (passes only on exit code 0). `name` titles the
    run in qa_jobs. Each step ends with a screenshot (shots=false skips them); headless (default) only applies to
    Godot, where it is invisible and fastest but takes no screenshots. Electron apps always open a window.
    expect_looks: statements judged from the game's screenshot at the end; vision: the screenshot goes with every
    decision. Both need Clef as the decision model and a picture, so they run the game windowed. vision is on by
    default when Clef decides and the game has a window (Electron, mobile, a windowed Godot game); vision=false
    turns it off, vision=true refuses to run without Clef. about: what this test proves and why, in plain words (with
    a suite: the whole run; each step can carry its own `about:`).
    plan: the approved test plan this run follows (a Markdown file; overrides the suite's `plan:`). Without an
    approved one the run warns, and with QAJEV_REQUIRE_PLAN=1 it is refused (see qa_plan)."""
    args = ["play", project, "--max-actions", str(max_actions), "--max-seconds", str(max_seconds),
            "--out", out_dir or str(DEFAULT_OUT)]
    for flag, value in (("--goal", goal), ("--adapter", adapter), ("--suite", suite),
                        ("--expect-screen", expect_screen), ("--name", name), ("--about", about)):
        if value:
            args += [flag, value]
    for key, value in (game_env or {}).items():
        args += ["--game-env", f"{key}={value}"]
    args += [f"--game-arg={a}" for a in game_args or []]  # one token: the switch itself starts with --
    if game_profile:
        args += ["--game-profile", game_profile]
    if reset_game_profile:
        args.append("--reset-game-profile")
    for text in expect_text or []:
        args += ["--expect-text", text]
    for key, value in (expect_state or {}).items():
        args += ["--expect-state", f"{key}={json.dumps(value) if not isinstance(value, str) else value}"]
    if min_fps is not None:
        args += ["--min-fps", str(min_fps)]
    if allow_errors:
        args.append("--allow-errors")
    if expect_closed:
        args.append("--expect-closed")
    for flag, labels in (("--allow", allow), ("--hide", hide), ("--only", only)):
        for label in labels or []:
            args += [flag, label]
    for statement in expect_looks or []:
        args += ["--expect-looks", statement]
    if vision is not None:
        args.append("--vision" if vision else "--no-vision")
    if headless and not (vision or expect_looks):  # a headless Godot game draws nothing to look at
        args.append("--headless")
    if not shots:
        args.append("--no-shots")
    if cost_cap is not None:
        args += ["--cost-cap", str(cost_cap)]
    args += _plan_args(plan)
    return _trim(await _run_report(args, ctx, background), verbose)  # titled like the CLI: game and test name


@server.tool()
async def qa_screenshot(run_dir: str, scenario: str | None = None, limit: int = 1) -> list:
    """See the page: a run's end-of-scenario screenshots as images, each with a one-line caption. By default the
    first scenario that did not pass (or the first one); name a `scenario` for another. `limit`: how many (max 4)."""
    folder = Path(run_dir).expanduser().resolve()
    try:
        data = json.loads((folder / "report.json").read_text())
    except (OSError, json.JSONDecodeError):
        raise ToolError(f"no readable report.json in {folder}") from None
    scenarios = data.get("scenarios") or []
    if scenario:
        chosen = [s for s in scenarios if s.get("name") == scenario]
        if not chosen:
            raise ToolError(f"no scenario {scenario!r}; have {[s.get('name') for s in scenarios]}")
    else:
        chosen = [s for s in scenarios if s.get("outcome") != "pass"] or scenarios
    out = []
    for s in [s for s in chosen if s.get("shot")][: max(1, min(limit, 4))]:
        path = (folder / s["shot"]).resolve()
        if folder not in path.parents or not path.is_file():  # only screenshots inside this run's folder
            continue
        out.append(f"{s['name']}: {s.get('outcome')} - {s.get('reason') or ''}")
        out.append(Image(path=str(path)))
    if not out:
        raise ToolError("no screenshot for that scenario (runs with --no-shots, or the page never loaded)")
    return out


@server.tool()
async def qa_jobs(limit: int = 20) -> dict:
    """Every QAJev run on this machine, newest first (any agent's or terminal's): state (queued, running, done,
    stopped, failed, lost), progress, gate, run folder. `holder` is the run using the browser right now."""
    rows = await asyncio.to_thread(jobs.listing, limit)
    return {"holder": jobs.holder(), "jobs": rows}


@server.tool()
async def qa_job(job: str, verbose: bool = False) -> dict:
    """One job in detail: state, progress, the scenarios finished so far (partial results while it runs) and,
    once done, the report (trimmed unless verbose). A cloud run's id (r_...): its state, tests and report address
    from QAJev Cloud."""
    if job.startswith("r_"):
        return await _cloud_job(job)
    try:
        st = await asyncio.to_thread(jobs.status, job, True)
    except jobs.NoSuchJob:
        raise ToolError(f"no job {job}") from None
    if st.get("report"):
        st["report"] = _trim(st["report"], verbose)
    if not verbose:
        keep = ("name", "about", "outcome", "reason", "seconds", "findings", "shot", "needs_sign_in")
        st["scenarios"] = [{k: r.get(k) for k in keep if r.get(k) not in (None, [])} for r in st["scenarios"]]
    return st


@server.tool()
async def qa_rerun(ctx: Context, job: str, failed: bool = True, background: bool = False) -> dict:
    """Run a finished check, run or play job again with the same settings. failed (default): only its tests that
    failed, got stuck or hit a harness limit (each still brings what it depends on; a game session its setup:
    true steps). The rerun is a new job; background=true returns its id at once."""
    try:
        argv, what = await asyncio.to_thread(jobs.rerun_argv, job, failed)
        cwd = await asyncio.to_thread(jobs.rerun_cwd, job)  # its relative paths mean the files where it ran
    except jobs.NoSuchJob:
        raise ToolError(f"no job {job}") from None
    except jobs.NothingToRerun as e:
        return {"rerun": None, "reason": str(e)}
    except ValueError as e:
        raise ToolError(str(e)) from None
    if "--allow-commands" in argv and not _allow_commands:  # a terminal run's permission is not this server's
        raise ToolError(f"job {job} ran shell commands (--allow-commands), which this MCP server does not allow: "
                        "rerun it in a terminal with `qajev rerun`")
    report = await _run_report(argv, ctx, background, title=None, cwd=cwd, rerun={"of": job, "failed": failed})
    return {"rerun": what, "of": job, **(report if background else _trim(report, False))}


@server.tool()
async def qa_stop(job: str) -> dict:
    """Stop a job (any agent's). It closes its tabs, Chrome and daemon and keeps the scenarios that finished;
    the partial report stays in its run folder. A cloud run's id (r_...): QAJev Cloud stops that run."""
    if job.startswith("r_"):
        return await _cloud_job(job, stopping=True)
    try:
        return await asyncio.to_thread(jobs.stop, job)
    except jobs.NoSuchJob:
        raise ToolError(f"no job {job}") from None
    except jobs.NotOurs as e:
        raise ToolError(str(e)) from None


@server.tool()
async def qa_nightly() -> dict:
    """The latest nightly digest: per project, the gate of its core objectives and of its smoke crawl, and what
    changed since the previous run (newly failing, fixed, new findings). `changed` is true when anything did."""
    from . import nightly

    return {"digest": nightly.last(), "schedule": nightly.scheduled()}


@server.tool()
async def qa_doctor() -> dict:
    """Check QAJev's setup: API keys present and where each came from (values never shown), Chrome, a free port,
    load average, and the production rule with the hosts that count as local dev hosts."""
    code, text, tail = await _spawn(["doctor", "--json"])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise ToolError(" | ".join(tail[-5:]) or f"doctor exited {code}") from None


@server.tool()
async def qa_mode(where: str | None = None, project: str | None = None) -> dict:
    """Where QAJev runs go: this Mac or QAJev Cloud. Without `where`, says where they go now and why. `where`:
    local (this Mac), cloud (always the cloud), auto (the cloud when it can, else here), dashboard (follow the
    dashboard's "Where runs go by default"). With `project`, sets that project only (local, cloud, auto, or default
    to follow this Mac's mode again); a project's setting wins over this Mac's mode. A run can still pass `runner`."""
    argv = ["mode", *([where] if where else []), *(["--project", project] if project else []), "--json"]
    code, text, tail = await _spawn(argv)
    try:
        return json.loads(text.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise ToolError(" | ".join(tail[-5:]) or f"mode exited {code}") from None


def serve(allow_commands=False):
    global _allow_commands
    _allow_commands = allow_commands
    server.run("stdio")
