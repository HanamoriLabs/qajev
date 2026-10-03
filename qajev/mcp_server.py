"""QAJev as an MCP server (stdio). Each browser run is a QAJev job: the CLI in its own process.

A process per run keeps every run isolated: its own browser_harness daemon (BU_NAME is read at import),
its own crash domain, and a clean stop on cancel. As a job it is visible to every agent and terminal on the
machine (qa_jobs, `qajev jobs`, `qajev top`) and stoppable from any of them. Only one browser run executes
at a time on the whole machine (a lock shared with the CLI); later runs queue.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import yaml
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__, jobs
from .config import HOME

INSTRUCTIONS = """\
QAJev drives a real Chrome with Jev (TypeSafe) to QA websites. Use it to check that a person could do
something on a site, and to catch errors a browser can see.

- qa_smoke first: crawls same-origin pages with no model calls (free) and lists HTTP errors, script
  errors, broken images and links, accessibility and SEO gaps.
- qa_check for one goal: say what a user wants in plain words, ending with "Stop when ...", and ALWAYS
  give expectations (expect_text / expect_url / expect_js). Jev's own DONE is only a hint; the verdict
  comes from those checks.
- qa_run_suite for many scenarios (YAML suite; see qajev init).
- qa_project_run to prove a product: a project's stored objectives (qa_projects lists them) or one ad-hoc
  objective; reports are filed per project and listed by qa_reports.
- Long runs: pass background=true to get a job id at once, then qa_job(job) for progress and partial results,
  qa_stop(job) to stop it. qa_jobs lists every QAJev run on the machine (any agent's), queued or running.
  Only one browser run executes at a time machine-wide; the others queue.
- Specific tests: qa_run_suite(only=[...]) and qa_play(suite=..., only=[...]) run chosen scenarios or game steps
  (with what they depend on); qa_rerun(job) runs a finished job's failed, stuck and harness tests again.
- Every run writes report.html for the person (its path is in the result as report_html).

Outcomes: pass; fail (product wrong); stuck (Jev found no way forward: verify by hand, often UX);
harness (budget/stale/model/browser trouble: says nothing about the product); unverified (no checks);
skipped. Gate: PASS, FAIL or INCOMPLETE.

Safety built in: read-only by default (writes are blocked in the page), dangerous controls (sign out,
delete, billing, pay, revoke, close all...) are hidden from Jev, passwords/payment fields are disabled,
the microphone is stubbed, mutate mode is loopback-only. Sign-in: qa_browser(action="login", url=...) opens
QAJev's own Chrome for the person, or a suite/project names a stored test account (account: with a keychain:,
op:// or env: password reference) and QAJev signs in itself before the scenarios. Never ask for, type or write
down a password: only ever the reference.

Images: with Clef as the decision model (QAJEV_JEV_PROVIDER=cloudflare), `expect_looks` judges plain statements
from the final screenshot (layout, a canvas, a cut-off button: what text checks cannot see), and `vision` sends the
screenshot with every decision. Jev reads text only; asking for either without Clef is refused.

Needs sign-in: when a result has `needs_sign_in`, runs met a sign-in page (those scenarios are harness, not product
failures). Do not report it as a bug. Ask the person how QAJev should get in, following its `next_step`: they sign
in once (qa_browser login), or they run `qajev account add NAME --email ... --login-url ...` in their own terminal
(the Keychain asks them for the password; in Claude Code they type it after a !). Then run again.
"""

server = MCPServer(name="qajev", version=__version__, instructions=INSTRUCTIONS)
_allow_commands = False
DEFAULT_OUT = Path(os.environ.get("QAJEV_OUT", HOME / "runs"))


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
    elif kind == "step":  # what Jev is doing, and the money spent so far
        spent = f" · ${event['spent_usd']:.4f}" if isinstance(event.get("spent_usd"), (int, float)) else ""
        await ctx.report_progress(counts["done"], counts["total"],
                                  f"{event.get('scenario')}: {event.get('doing')}{spent}")


async def _run_report(args, ctx, background=False, title=None, cwd=None, rerun=None):
    """A browser run as a QAJev job. background: return the job at once. Otherwise wait for it (relaying its
    progress) and return the report JSON; cancelling the call stops the job. cwd: where it runs (default: here);
    rerun: the job it reruns (jobs.start)."""
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
        await asyncio.to_thread(jobs.stop, job["id"])  # the run closes its own tabs, Chrome and daemon
        raise
    if not data or "error" in data:
        st = jobs.status(job["id"])
        raise ToolError(data.get("error") or st.get("error") or f"qajev job {job['id']} ended {st['state']}")
    data["job"] = job["id"]
    return data


def _trim(report, verbose):
    """Keep tool results small enough for a model's context; the full report stays on disk."""
    if verbose or "scenarios" not in report:
        return report
    keep = ("name", "outcome", "reason", "stop", "seconds", "cost_usd", "end_url", "checks", "findings", "shot",
            "jev", "blocked_writes", "page_says", "needs_sign_in")
    out = {k: v for k, v in report.items() if k != "scenarios"}
    out["scenarios"] = [{k: r.get(k) for k in keep if r.get(k) not in (None, [], {})} for r in report["scenarios"]]
    for r in out["scenarios"]:
        if r.get("blocked_writes"):
            r["blocked_writes"] = len(r["blocked_writes"])
    out["report_md"] = str(Path(report["run_dir"]) / "report.md")
    out["report_html"] = str(Path(report["run_dir"]) / "report.html")
    return out


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
    expect_looks: list[str] | None = None,
    vision: bool = False,
    fetch: list[str] | None = None,
    mode: str = "readonly",
    device: str | None = None,
    persona: str | None = None,
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
) -> dict:
    """Run one QA scenario: open `url`, optionally let Jev pursue `goal`, then judge the page.

    goal: plain words, e.g. "Open the pricing page. Stop when plan prices are visible."
    expect_text / absent_text: case-sensitive substrings the page must / must not show.
    expect_url: substring of the final URL. expect_js: JS expression that must be truthy.
    expect_looks: statements judged from the final screenshot ("the Sign up button is not cut off"); vision: the
    screenshot goes with every decision. Both need Clef as the decision model (Jev reads text only).
    fetch: in-page GET checks, "URL" or "URL=STATUS". mode: readonly (default) or mutate (loopback only).
    device: desktop | tall | phone | tablet | WIDTHxHEIGHT pins one; otherwise `devices` (default desktop and
    phone). real_devices: also in ios Safari / android Chrome on a throwaway simulator (read-only, slower).
    Returns the report with a gate and findings.
    background: return a job id at once instead (follow with qa_job, stop with qa_stop).
    """
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
        args += ["--expect-js", expect_js]
    for statement in expect_looks or []:
        args += ["--expect-looks", statement]
    if vision:
        args.append("--vision")
    for probe in fetch or []:
        args += ["--fetch", probe]
    if persona:
        args += ["--persona", persona]
    for host in hosts or []:
        args += ["--host", host]
    if cost_cap is not None:
        args += ["--cost-cap", str(cost_cap)]
    args += _motion_args(motion)
    args += _devices_args(devices, real_devices)
    return _trim(await _run_report(args, ctx, background), verbose)


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
) -> dict:
    """Run a QAJev suite (YAML/JSON) from a file path or inline text. `only` limits to named scenarios
    (plus their dependencies). jobs > 1 runs independent chains in parallel; keep 1 on a busy machine.
    background: return a job id at once instead (follow with qa_job, stop with qa_stop)."""
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
    args += _motion_args(motion)
    args += _devices_args(devices, real_devices)
    return _trim(await _run_report(args, ctx, background, title), verbose)


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
) -> dict:
    """Crawl same-origin pages from `url` with NO model calls and lint each one: HTTP status, script
    errors, failed requests, CSP blocks and report-only violations, broken images, unlabeled fields,
    unnamed buttons, overflow, SEO basics.
    background: return a job id at once instead (follow with qa_job, stop with qa_stop)."""
    args = ["smoke", url, "--max-pages", str(max_pages), *(["--device", device] if device else []),
            "--out", out_dir or str(DEFAULT_OUT),
            *_browser_args(profile, cdp_url, headless)]
    if check_links:
        args.append("--check-links")
    args += _motion_args(motion)
    args += _devices_args(devices)
    return _trim(await _run_report(args, ctx, background), verbose)


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
) -> dict:
    """Prove a product works: run a project's stored objectives (optionally only those tagged `suite`, or named
    in `names`), or one ad-hoc `objective` in plain words with `expect_text`/`expect_url` checks. The report is
    filed with the project and in the cross-project index. Production environments are always read-only.
    background: return a job id at once instead (follow with qa_job, stop with qa_stop)."""
    args = ["run", "--project", project, *_browser_args(profile, None, headless)]
    for flag, value in (("--env", env), ("--objective", objective), ("--suite", suite), ("--url", url),
                        ("--expect-url", expect_url)):
        if value:
            args += [flag, value]
    for name in names or []:
        args += ["--name", name]
    for text in expect_text or []:
        args += ["--expect-text", text]
    args += _motion_args(motion)
    args += _devices_args(devices, real_devices)
    return _trim(await _run_report(args, ctx, background), verbose)


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
async def qa_report(run_dir: str, markdown: bool = False) -> dict:
    """Read a finished run's report (JSON by default, or the Markdown text)."""
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
    expect_screen: str | None = None,
    expect_text: list[str] | None = None,
    expect_state: dict | None = None,
    min_fps: float | None = None,
    allow_errors: bool = False,
    expect_closed: bool = False,
    expect_looks: list[str] | None = None,
    vision: bool = False,
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
) -> dict:
    """Native: QA a game or a mobile app through QAJev's bridge: a Godot project folder, an Electron .app or project
    folder, or a mobile target (ios:<bundle id or URL>, android:<package or URL>). Jev plays the UI a player uses
    (menus, start, pause, choices), reading each screen as text; input goes into the game only, and the game gets a
    throwaway save folder. `adapter`: a bundled name (suho, hypervolley, imhim) or a path, for games that draw their
    own UI. `suite`: a YAML file of steps in one session: goal steps, real-time play steps (`play:`, needs the
    game's bot), idle steps (`idle: SECONDS`, the game runs untouched), with top-level `allow`/`hide` labels.
    `only`: run these steps of the suite, plus the steps they name in `depends_on` and every `setup: true` step.
    `game_env`: environment settings for the game; `game_args`: switches for an Electron app. expect_state: game
    state values, e.g. {"game_over": false, "kills": ">= 1"}. Quit, exit and delete-save buttons are hidden from Jev;
    to test a normal quit pass allow=["QUIT"] and expect_closed=true (passes only on exit code 0). `name` titles the
    run in qa_jobs. Each step ends with a screenshot (shots=false skips them); headless (default) only applies to
    Godot, where it is invisible and fastest but takes no screenshots. Electron apps always open a window.
    expect_looks: statements judged from the game's screenshot at the end; vision: the screenshot goes with every
    decision. Both need Clef as the decision model and a picture, so they run the game windowed."""
    args = ["play", project, "--max-actions", str(max_actions), "--max-seconds", str(max_seconds),
            "--out", out_dir or str(DEFAULT_OUT)]
    for flag, value in (("--goal", goal), ("--adapter", adapter), ("--suite", suite),
                        ("--expect-screen", expect_screen), ("--name", name)):
        if value:
            args += [flag, value]
    for key, value in (game_env or {}).items():
        args += ["--game-env", f"{key}={value}"]
    args += [f"--game-arg={a}" for a in game_args or []]  # one token: the switch itself starts with --
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
    if vision:
        args.append("--vision")
    if headless and not (vision or expect_looks):  # a headless Godot game draws nothing to look at
        args.append("--headless")
    if not shots:
        args.append("--no-shots")
    if cost_cap is not None:
        args += ["--cost-cap", str(cost_cap)]
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
    once done, the report (trimmed unless verbose)."""
    try:
        st = await asyncio.to_thread(jobs.status, job, True)
    except jobs.NoSuchJob:
        raise ToolError(f"no job {job}") from None
    if st.get("report"):
        st["report"] = _trim(st["report"], verbose)
    if not verbose:
        keep = ("name", "outcome", "reason", "seconds", "findings", "shot", "needs_sign_in")
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
    the partial report stays in its run folder."""
    try:
        return await asyncio.to_thread(jobs.stop, job)
    except jobs.NoSuchJob:
        raise ToolError(f"no job {job}") from None


@server.tool()
async def qa_nightly() -> dict:
    """The latest nightly digest: per project, the gate of its core objectives and of its smoke crawl, and what
    changed since the previous run (newly failing, fixed, new findings). `changed` is true when anything did."""
    from . import nightly

    return {"digest": nightly.last(), "schedule": nightly.scheduled()}


@server.tool()
async def qa_doctor() -> dict:
    """Check QAJev's setup: API keys present (values never shown), Chrome, a free port, load average."""
    code, text, tail = await _spawn(["doctor", "--json"])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise ToolError(" | ".join(tail[-5:]) or f"doctor exited {code}") from None


def serve(allow_commands=False):
    global _allow_commands
    _allow_commands = allow_commands
    server.run("stdio")
