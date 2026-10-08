"""QAJev's own Chrome: a separate profile on a free port, never the person's daily browser.

Ownership is proven from the process command line (a port number is a convention, not an owner),
and stopping kills that exact pid only. Chrome runs at low priority because the machine is shared.
"""

import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from .config import HOME

PORTS = range(9350, 9400)  # 9333 and 9340-9347 belong to other harnesses on José's machine
STATE = HOME / "chrome"
PROFILES = HOME / "profiles"
EPHEMERAL_ROOT = HOME / "tmp"  # throwaway profiles live here, deleted after every run (and reaped if a run dies)
MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
FLAGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-features=Translate,MediaRouter,OptimizationHints",
    "--deny-permission-prompts",
    # Defence in depth for the mic: synthetic devices, and never --use-fake-ui-for-media-stream
    # (that flag GRANTS the real microphone to the page's speech recognition).
    "--use-fake-device-for-media-stream",
    "--window-size=1400,1000",
]


class ChromeError(RuntimeError):
    pass


def binary():
    for candidate in (os.environ.get("QAJEV_CHROME"), MAC_CHROME, shutil.which("google-chrome"),
                      shutil.which("chromium"), shutil.which("chromium-browser")):
        if candidate and Path(candidate).exists():
            return candidate
    raise ChromeError("Chrome not found; set QAJEV_CHROME to its binary")


def listening(port):
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def version(cdp_url, timeout=1.0):
    try:
        with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=timeout) as r:
            return json.load(r)
    except OSError:
        return None


def targets(cdp_url):
    with urllib.request.urlopen(f"{cdp_url}/json/list", timeout=3) as r:
        return json.load(r)


def close_target(cdp_url, target_id):
    """Chrome's HTTP endpoint works even when the harness daemon is stuck on a failed call."""
    try:
        urllib.request.urlopen(f"{cdp_url}/json/close/{target_id}", timeout=5).read()
        return True
    except OSError:
        return False


def open_visible(cdp_url, url):
    req = urllib.request.Request(f"{cdp_url}/json/new?{url}", method="PUT")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def flags():
    """QAJev's Chrome flags plus $QAJEV_CHROME_FLAGS (e.g. --no-sandbox in Docker or on CI runners that block user
    namespaces, where Chrome otherwise stops with "No usable sandbox!")."""
    import shlex

    return [*FLAGS, *shlex.split(os.environ.get("QAJEV_CHROME_FLAGS", ""))]


PROC = Path("/proc")  # Linux: the process list when there is no `ps` (slim images, minimal CI)


def _proc_cmdline(pid):
    try:
        return (PROC / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def cmdline(pid):
    try:
        # ww: procps-ng cuts at 80 columns when stdout is not a terminal (MCP server, background jobs, CI)
        out = subprocess.run(["ps", "ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return _proc_cmdline(pid)
    return out.stdout.strip()


def started(pid):
    """When process `pid` started (ps lstart; /proc's start ticks where there is no ps), or None when it is gone."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        return "ticks " + (PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def identity(pid):
    """What makes `pid` this process and not a later one with the same number: its start and its command."""
    return {"proc_start": started(pid), "command": cmdline(pid)[:400]}


def still(pid, known):
    """`pid` is still the process `known` (identity()) recorded: a pid reused by another process never matches.
    Without a recorded identity nothing matches, so nothing is killed on a guess (the Orchestrator, 6 Oct)."""
    return bool(known and known.get("proc_start")) and started(pid) == known["proc_start"] \
        and cmdline(pid)[:400] == known.get("command")


def processes():
    """Every process as (pid, command): from `ps`, or from /proc where there is no `ps`."""
    try:
        out = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = None
    if out is not None:
        rows = [line.strip().partition(" ") for line in out.splitlines()]
        return [(int(pid), cmd.strip()) for pid, _, cmd in rows if pid.isdigit()]
    if not PROC.is_dir():
        return []
    found = [(int(d.name), _proc_cmdline(d.name)) for d in PROC.iterdir() if d.name.isdigit()]
    return sorted((pid, cmd) for pid, cmd in found if cmd)


def owns(record):
    line = cmdline(record["pid"])
    return f"--user-data-dir={record['profile_dir']}" in line and f"--remote-debugging-port={record['port']}" in line


def _state_file(profile):
    return STATE / f"{profile}.json"


def status(profile=None):
    STATE.mkdir(parents=True, exist_ok=True)
    records = []
    for path in sorted(STATE.glob("*.json")):
        if profile and path.stem.split(".ephemeral-")[0] != profile:
            continue
        record = json.loads(path.read_text())
        record["alive"] = owns(record) and version(record["cdp_url"]) is not None
        if not record["alive"] and not owns(record):
            path.unlink(missing_ok=True)  # stale: the pid is gone or belongs to something else now
            continue
        try:
            record["tabs"] = sum(1 for t in targets(record["cdp_url"]) if t.get("type") == "page")
        except OSError:
            record["tabs"] = None
        records.append(record)
    return records


def free_port():
    for port in PORTS:
        if not listening(port):
            return port
    raise ChromeError(f"no free port in {PORTS.start}-{PORTS.stop - 1}")


def start(profile="default", *, headless=False, port=None, ephemeral=False, visible=False, wait=None):
    """Start (or reuse) the Chrome for a profile. Returns its record."""
    if not ephemeral:
        running = [r for r in status(profile) if not r.get("ephemeral")]
        if running and running[0]["alive"]:
            if headless != running[0]["headless"]:
                raise ChromeError(
                    f"profile {profile!r} already runs {'headless' if running[0]['headless'] else 'headed'} "
                    f"on port {running[0]['port']}; stop it first"
                )
            return running[0]
    # A loaded machine (load 300+) has taken over 30 s to open the debugging port.
    wait = wait or float(os.environ.get("QAJEV_CHROME_START_WAIT", "60"))
    port = port or free_port()
    if listening(port):
        raise ChromeError(f"port {port} is already in use")
    if ephemeral:
        # The owner's pid is in the name, so reap() can find this profile if its owner dies without cleaning up.
        EPHEMERAL_ROOT.mkdir(parents=True, exist_ok=True)
        profile_dir = Path(tempfile.mkdtemp(prefix=f"qajev-{profile}-{os.getpid()}-", dir=EPHEMERAL_ROOT))
    else:
        profile_dir = PROFILES / profile
        profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        return _launch(profile, profile_dir, port, headless, visible, ephemeral, wait)
    except BaseException:
        if ephemeral:
            shutil.rmtree(profile_dir, ignore_errors=True)  # a failed start must not leave its temp profile
        raise


def run_record(owned):
    """What a run's report says about the Chrome it started (or reused): a throwaway one is called that, not by the
    profile name it was started under."""
    return {"cdp_url": owned["cdp_url"], "managed": True, "profile": owned["profile"], "headless": owned["headless"],
            "port": owned["port"], "ephemeral": bool(owned.get("ephemeral"))}


def _app_bundle(path):
    """The .app a macOS binary lives in (.../Google Chrome.app/Contents/MacOS/Google Chrome), or None."""
    return next((p for p in Path(path).parents if p.suffix == ".app"), None)


def _open_behind(app, argv, log_path):
    """Start a new Chrome through LaunchServices, hidden (-j) and not brought forward (-g). Started directly, Chrome
    comes to the front, ignores --start-minimized, and what the person types lands in it (José, 7 Oct). Measured on
    one run each: Chrome held the front ~7.2 s started directly, ~5.9 s with -g alone, ~0.2 s with -g -j; pages and
    screenshots worked in all three. Hidden, the window still draws: a Verse 3D page ran at 182 frames a second (8 Oct;
    minimised, 0). -n: always a new instance, never the person's own Chrome. Returns the `open` process, not
    Chrome's."""
    return subprocess.Popen(["/usr/bin/open", "-n", "-g", "-j", "-a", str(app), "--stderr", str(log_path), "--args",
                             *argv], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


def _browser_pid(exe, profile_dir, port):
    """The browser process of the Chrome on this profile and port (not a helper: those carry --type=; not the `open`
    that started it: its command line starts with /usr/bin/open), or None."""
    marks = (f"--user-data-dir={profile_dir}", f"--remote-debugging-port={port}")
    return next((pid for pid, cmd in processes()
                 if cmd.startswith(exe) and all(m in cmd for m in marks) and "--type=" not in cmd), None)


def _launch(profile, profile_dir, port, headless, visible, ephemeral, wait):
    args = [binary(), f"--user-data-dir={profile_dir}", f"--remote-debugging-port={port}", *flags()]
    if headless:
        args.append("--headless=new")
    # Never --start-minimized: a minimised window draws no frames, so a 3D page stalls (8 Oct). On a Mac the window
    # stays off the person's screen because it starts hidden (_open_behind).
    args.append("about:blank")
    STATE.mkdir(parents=True, exist_ok=True)
    log_path = STATE / f"{profile}.log"
    # A window the person did not ask for must not take their keyboard; `visible` (a sign-in) is meant to come forward.
    app = _app_bundle(args[0]) if sys.platform == "darwin" and not headless and not visible else None
    if app:
        proc, pid = _open_behind(app, args[1:], log_path), None
    else:
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
                preexec_fn=(lambda: os.nice(10)) if sys.platform != "win32" else None,
            )
        pid = proc.pid
    cdp_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if pid is None and (pid := _browser_pid(args[0], profile_dir, port)) is not None:
            with contextlib.suppress(OSError):  # LaunchServices started it: lower its priority as Popen's nice did
                os.setpriority(os.PRIO_PROCESS, pid, 10)
        if pid is not None and version(cdp_url, timeout=0.5):
            break
        if pid == proc.pid and proc.poll() is not None:
            raise ChromeError(f"Chrome exited with {proc.returncode} (see {log_path})")
        if pid is None and proc.poll() not in (None, 0):
            raise ChromeError(f"macOS could not start Chrome (open exited with {proc.returncode}; see {log_path})")
        if pid is not None and pid != proc.pid and reaped(pid):
            raise ChromeError(f"Chrome exited (see {log_path})")
        time.sleep(0.2)
    else:
        if pid is not None:
            _kill(pid)
        raise ChromeError(f"Chrome did not open its debugging port {port} within {wait}s")
    state_key = f"{profile}.ephemeral-{os.getpid()}" if ephemeral else profile
    record = {
        "profile": profile, "state_key": state_key, "pid": pid, "port": port, "cdp_url": cdp_url,
        "profile_dir": str(profile_dir), "headless": headless, "ephemeral": ephemeral, "owner_pid": os.getpid(),
        "started_at": time.time(),
    }
    _state_file(state_key).write_text(json.dumps(record, indent=2))
    return {**record, "alive": True}


def reaped(pid):
    """True once `pid` is gone. A child of ours that exited stays a zombie until it is reaped, and a zombie still
    answers kill(pid, 0); so reap it here, or every stop waits out its whole grace period."""
    try:
        if os.waitpid(pid, os.WNOHANG)[0]:
            return True
    except ChildProcessError:
        pass  # not our child: whoever started it reaps it
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _kill(pid, grace=5.0):
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if reaped(pid):
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    time.sleep(0.1)
    reaped(pid)


def stop(profile="default"):
    """Stop by state key: a profile name, or the `state_key` of an ephemeral Chrome."""
    path = _state_file(profile)
    if not path.exists():
        return False
    record = json.loads(path.read_text())
    stopped = False
    if owns(record):
        _kill(record["pid"])
        stopped = True
    path.unlink(missing_ok=True)
    if record.get("ephemeral"):
        shutil.rmtree(record["profile_dir"], ignore_errors=True)
    return stopped


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


EPHEMERAL = re.compile(r"--user-data-dir=(\S*/qajev-[\w.]+?-(\d+)-[\w]+)\b")
DAEMON_FILE = re.compile(r"^bu-(qajev-(\d+)-[0-9a-f]+)\.pid$")


def reap():
    """Stop what a dead QAJev run left behind: ephemeral Chromes and harness daemons whose owning process is gone
    (killed, crashed, or interrupted twice mid-cleanup). Only QAJev-named resources whose owner pid is dead."""
    reaped = []
    for pid_num, command in processes():
        pid_text = str(pid_num)
        match = EPHEMERAL.search(command)
        if not match or "--type=" in command or "--remote-debugging-port=" not in command:
            continue  # helpers die with their browser; only the main process is ours to stop
        profile_dir, owner = match.group(1), int(match.group(2))
        if alive(owner):
            continue
        _kill(int(pid_text))
        shutil.rmtree(profile_dir, ignore_errors=True)
        reaped.append(f"chrome {pid_text} ({Path(profile_dir).name})")
    stray = [*EPHEMERAL_ROOT.glob("qajev-*-*-*")] if EPHEMERAL_ROOT.exists() else []
    for folder in stray + [*Path(tempfile.gettempdir()).glob("qajev-*-*-*")]:
        match = re.fullmatch(r"qajev-[\w.]+?-(\d+)-\w+", folder.name)
        if match and folder.is_dir() and not alive(int(match.group(1))):
            shutil.rmtree(folder, ignore_errors=True)
            reaped.append(f"profile {folder.name}")
    for path in STATE.glob("*.ephemeral-*.json") if STATE.exists() else []:
        record = json.loads(path.read_text())
        if not alive(record.get("owner_pid", 0)):
            shutil.rmtree(record["profile_dir"], ignore_errors=True)
            path.unlink(missing_ok=True)
    runtime = Path(os.environ.get("BH_RUNTIME_DIR") or Path.home() / ".config" / "browser-harness" / "runtime")
    for path in runtime.glob("bu-qajev-*.pid") if runtime.exists() else []:
        match = DAEMON_FILE.match(path.name)
        if not match or alive(int(match.group(2))):
            continue
        name = match.group(1)
        try:
            daemon_pid = int(json.loads(path.read_text()).get("pid") if path.read_text().strip().startswith("{")
                             else path.read_text().split()[0])
        except (ValueError, OSError, AttributeError):
            daemon_pid = None
        if daemon_pid and alive(daemon_pid) and "browser_harness.daemon" in cmdline(daemon_pid):
            _kill(daemon_pid)
            reaped.append(f"daemon {daemon_pid} ({name})")
        for leftover in runtime.glob(f"bu-{name}.*"):
            leftover.unlink(missing_ok=True)
    # A daemon can outlive its run and its runtime files; then only the process itself is left to find.
    for pid, command in processes():
        if "browser_harness.daemon" not in command:
            continue
        if any(r.startswith(f"daemon {pid} ") for r in reaped):
            continue
        name = _daemon_name(pid)
        match = re.fullmatch(r"qajev-(\d+)-\w+", name or "")
        if match and not alive(int(match.group(1))) and alive(pid):
            _kill(pid)
            reaped.append(f"daemon {pid} ({name})")
    return reaped


def _daemon_name(pid):
    """A daemon's BU_NAME, from its environment (ps can read it for our own user's processes)."""
    try:
        out = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        try:  # no ps: the environment straight from /proc
            out = " ".join((PROC / str(pid) / "environ").read_bytes().decode(errors="replace").split("\0"))
        except OSError:
            return None
    match = re.search(r"(?:^|\s)BU_NAME=(\S+)", out)
    return match.group(1) if match else None
