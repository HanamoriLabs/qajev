"""`qajev nightly`: every project's core objectives and a smoke crawl, one after another, each compared with the
previous run of its kind. A digest goes to ~/.qajev/nightly/<date>.{json,md}; a notification only when something
changed (or a run could not complete). `--install` schedules it with launchd.

Each run is an ordinary `qajev` run in its own process: it queues for the browser, respects the load gate, runs
headless in a throwaway profile, read-only on production, and files its report with its project.
"""

import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import HOME

NIGHTLY = HOME / "nightly"
LABEL = "com.qajev.nightly"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
RUN_TIMEOUT = 3600


def plan(names=None, smoke_pages=10):
    """-> [(project, kind, argv)] for the chosen projects (default: all that load)."""
    from . import project as project_mod

    steps = []
    for item in project_mod.listing():
        if item.get("error") or (names and item["name"] not in names):
            continue
        proj = project_mod.load(item["name"])
        core = any("core" in (o.get("tags") or []) for o in proj.objectives
                   if (o.get("env") or proj.default_env) == proj.default_env)
        steps.append((proj.name, "objectives", ["run", "--project", proj.name, *(["--suite", "core"] if core else [])]))
        steps.append((proj.name, "smoke", ["smoke", "--project", proj.name, "--max-pages", str(smoke_pages),
                                           "--check-links"]))
    return steps


def _run(argv) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "QAJEV_JOB"}
    try:
        out = subprocess.run([sys.executable, "-m", "qajev", *argv, "--ephemeral", "--headless", "--json", "--quiet"],
                             capture_output=True, text=True, timeout=RUN_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return {"error": f"did not finish within {RUN_TIMEOUT} s"}
    try:
        return json.loads(out.stdout) if out.stdout.strip() else {"error": out.stderr.strip()[-300:] or "no output"}
    except json.JSONDecodeError:
        return {"error": out.stderr.strip()[-300:] or "unreadable output"}


def run(names=None, smoke_pages=10, notify=True, emit=print):
    started = time.time()
    entries = []
    for project, kind, argv in plan(names, smoke_pages):
        emit(f"{time.strftime('%H:%M:%S')} {project} {kind}: running")
        report = _run(argv)
        ch: dict = report.get("changes") or {}
        # a refused run (a project env that would change production, exit 5) started nothing: it says so in the gate
        gate = report.get("gate") or ("REFUSED" if report.get("outcome") == "refused" else None)
        entry = {"project": project, "kind": kind, "gate": gate, "error": report.get("error"),
                 "cost_usd": (report.get("cost") or {}).get("usd"),
                 "html": str(Path(report["run_dir"]) / "report.html") if report.get("run_dir") else None,
                 "changes": ch.get("summary") or "first run: nothing to compare with",
                 "changed": bool(ch.get("changed")),
                 "newly_failing": [i["name"] for i in ch.get("newly_failing") or []]}
        entries.append(entry)
        emit(f"{time.strftime('%H:%M:%S')} {project} {kind}: {entry['error'] or entry['gate']} · {entry['changes']}")
    digest = {"date": time.strftime("%Y-%m-%d", time.localtime(started)),
              "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
              "seconds": round(time.time() - started), "entries": entries,
              "changed": any(e["changed"] or e["error"] for e in entries),
              "cost_usd": round(sum(e["cost_usd"] or 0 for e in entries), 4)}
    path = write(digest)
    digest["path"] = str(path)
    if notify and digest["changed"]:
        _notify(digest, path)
    return digest


def markdown(d):
    lines = [f"# QAJev nightly {d['date']}", "",
             f"**{'Something changed' if d['changed'] else 'No changes'}** · {len(d['entries'])} run(s) · "
             f"{d['seconds']} s · ${d['cost_usd']:.4f}", "",
             "| Project | Kind | Gate | Since the previous run | Report |", "|---|---|---|---|---|"]
    for e in d["entries"]:
        what = f"**{e['changes']}**" if e["changed"] else e["changes"]
        if e["error"]:
            what = f"**could not run:** {e['error']}"
        lines.append(f"| {e['project']} | {e['kind']} | {e['gate'] or '-'} | {what} | {e['html'] or ''} |")
    return "\n".join(lines) + "\n"


def write(digest):
    NIGHTLY.mkdir(parents=True, exist_ok=True)
    path = NIGHTLY / f"{digest['date']}.json"
    path.write_text(json.dumps(digest, indent=2))
    (NIGHTLY / f"{digest['date']}.md").write_text(markdown(digest))
    (NIGHTLY / "latest.json").write_text(json.dumps(digest, indent=2))
    return path


def last():
    path = NIGHTLY / "latest.json"
    return json.loads(path.read_text()) if path.exists() else None


def headline(d):
    changed = [e for e in d["entries"] if e["changed"] or e["error"]]
    if not changed:
        return "no changes"
    return "; ".join(f"{e['project']} {e['kind']}: {'could not run' if e['error'] else e['changes']}"
                     for e in changed)


def _notify(digest, path):
    text = headline(digest)
    if shutil.which("osascript"):  # macOS
        script = f"display notification {json.dumps(text[:230])} with title \"QAJev nightly\""
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    elif shutil.which("notify-send"):  # Linux desktops
        subprocess.run(["notify-send", "QAJev nightly", text[:230]], capture_output=True, timeout=10)
    hook = os.environ.get("QAJEV_NOTIFY_CMD")  # e.g. a script that posts the digest somewhere
    if hook:
        subprocess.run(hook, shell=True, env={**os.environ, "QAJEV_DIGEST": str(path), "QAJEV_DIGEST_TEXT": text},
                       timeout=120)


# ---- schedule ----

def install(at="03:30", names=None):
    hour, minute = (int(x) for x in at.split(":"))
    if not shutil.which("launchctl"):
        raise RuntimeError(f"--install uses launchd (macOS). Elsewhere, add a cron line instead: "
                           f"{minute} {hour} * * * {sys.executable} -m qajev nightly")
    args = [sys.executable, "-m", "qajev", "nightly"] + [a for n in names or [] for a in ("--project", n)]
    NIGHTLY.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL, "ProgramArguments": args,
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(NIGHTLY / "launchd.log"), "StandardErrorPath": str(NIGHTLY / "launchd.log"),
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                 "HOME": str(Path.home())},
        "Nice": 10, "LowPriorityIO": True, "ProcessType": "Background",
    }
    if os.environ.get("QAJEV_NOTIFY_CMD"):
        plist["EnvironmentVariables"]["QAJEV_NOTIFY_CMD"] = os.environ["QAJEV_NOTIFY_CMD"]
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(PLIST)], capture_output=True)
    PLIST.write_bytes(plistlib.dumps(plist))
    done = subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {done.stderr.strip()}")
    return {"plist": str(PLIST), "at": f"{hour:02d}:{minute:02d}", "command": args}


def uninstall():
    if shutil.which("launchctl"):  # macOS; on Linux the nightly is a cron line the person removes
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST)], capture_output=True)
    existed = PLIST.exists()
    PLIST.unlink(missing_ok=True)
    return existed


def scheduled():
    if not PLIST.exists():
        return None
    data = plistlib.loads(PLIST.read_bytes())
    when = data.get("StartCalendarInterval") or {}
    loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True).returncode == 0
    return {"plist": str(PLIST), "at": f"{when.get('Hour', 0):02d}:{when.get('Minute', 0):02d}", "loaded": loaded}
