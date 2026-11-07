"""QAJev's own Chrome: a separate profile on a free port, never the person's daily browser.

Ownership is proven from the process command line (a port number is a convention, not an owner),
and stopping kills that exact pid only. Chrome runs at low priority because the machine is shared.
"""

import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import suppress
from pathlib import Path

from .config import HOME
from .lowprio import niced

PORTS = range(9350, 9400)  # 9333 and 9340-9347 belong to other harnesses on José's machine
STATE = HOME / "chrome"
PROFILES = HOME / "profiles"
EPHEMERAL_ROOT = HOME / "tmp"  # throwaway profiles live here, deleted after every run (and reaped if a run dies)
TEMP_ROOT = Path("/tmp")  # Chrome's Unix socket must fit sun_path, even with a deeply nested QAJEV_HOME
MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
FLAGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
    # Policy token uploaders start GCM even with background networking disabled. Check-in and registration
    # use internal URLs. The raw TLS channel ignores schemes, so pin it to loopback's unusable port zero.
    "--gcm-checkin-url=chrome://qajev-disabled",
    "--gcm-registration-url=chrome://qajev-disabled",
    "--gcm-mcs-endpoint=https://127.0.0.1:0",
    "--disable-domain-reliability",
    "--disable-client-side-phishing-detection",
    "--no-pings",
    "--metrics-recording-only",
    "--safebrowsing-disable-auto-update",
    "--disable-features=Translate,MediaRouter,OptimizationHints,AutofillServerCommunication,"
    "PasswordLeakDetection,OptimizationGuideModelDownloading,NetworkTimeServiceQuerying,"
    "AimServerEligibilityEnabled,AimServerRequestOnStartupEnabled,OptimizationGuideManifestBroker,"
    "OptimizationGuideOnDeviceModel",
    "--deny-permission-prompts",
    "--password-store=basic",
    # Defence in depth for the mic: synthetic devices, and never --use-fake-ui-for-media-stream
    # (that flag GRANTS the real microphone to the page's speech recognition).
    "--use-fake-device-for-media-stream",
    "--window-size=1400,1000",
]

# Chrome needs the host's runtime, display, locale, fonts and selected graphics driver, never model/application
# authorization or Python/loader injection. Keep exact names: LC_*, XDG_* and driver prefixes can hide credentials.
CHROME_ENV_NAMES = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TMP", "TEMP", "TZ", "LANG", "LANGUAGE",
    "LC_ALL", "LC_CTYPE", "LC_COLLATE", "LC_MESSAGES", "LC_MONETARY", "LC_NUMERIC", "LC_TIME", "LC_PAPER",
    "LC_NAME", "LC_ADDRESS", "LC_TELEPHONE", "LC_MEASUREMENT", "LC_IDENTIFICATION", "__CF_USER_TEXT_ENCODING",
    "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "DESKTOP_SESSION", "XDG_CONFIG_HOME", "XDG_CONFIG_DIRS",
    "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_DATA_DIRS", "GDK_BACKEND", "GDK_SCALE", "GDK_DPI_SCALE",
    "FONTCONFIG_FILE", "FONTCONFIG_PATH", "LIBGL_DRIVERS_PATH", "LIBGL_ALWAYS_SOFTWARE",
    "MESA_LOADER_DRIVER_OVERRIDE", "__GLX_VENDOR_LIBRARY_NAME", "DRI_PRIME", "__NV_PRIME_RENDER_OFFLOAD",
    "__NV_PRIME_RENDER_OFFLOAD_PROVIDER", "__VK_LAYER_NV_optimus", "VK_ICD_FILENAMES", "VK_DRIVER_FILES",
    "GBM_BACKEND",
})


def child_env(*, temporary=None):
    """The managed browser's exact runtime allowlist; never modify the parent or command-hook environment."""
    env = {name: value for name, value in os.environ.items() if name in CHROME_ENV_NAMES}
    if temporary is not None:
        env.update(TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary))
    return env


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


def shown_cdp_url(cdp_url):
    """An attached Chrome's address as a run's record shows it: a /devtools/browser/<id> path's id is that browser's
    key, so the record keeps only `<id>` (a cloud run's relay address held a one-run key, 7 Nov 2026)."""
    return re.sub(r"(/devtools/browser/)[^/?#]+", r"\1<id>", str(cdp_url))


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


def _switch_name(value):
    # Chromium's POSIX parser accepts both prefixes; native Windows Chrome is not supported (use WSL).
    prefix = 2 if value.startswith("--") else 1 if value.startswith("-") else 0
    return value[prefix:].split("=", 1)[0] if prefix else None


def flags():
    """QAJev's Chrome flags plus $QAJEV_CHROME_FLAGS (e.g. --no-sandbox in Docker or on CI runners that block user
    namespaces, where Chrome otherwise stops with "No usable sandbox!")."""
    # Chromium trims each argument before parsing its switch prefix, including quoted arguments.
    extra = [value.strip() for value in shlex.split(os.environ.get("QAJEV_CHROME_FLAGS", ""))]
    if any(_switch_name(value) in {"gcm-checkin-url", "gcm-registration-url", "gcm-mcs-endpoint"}
           for value in extra):
        raise ChromeError("QAJev Chrome flags cannot override a messaging endpoint")
    combined = [*FLAGS, *extra]
    result, disabled = [], []
    feature_position = None
    for value in combined:
        if _switch_name(value) == "disable-features":
            if "=" not in value:
                raise ChromeError("Use --disable-features=FEATURES in QAJEV_CHROME_FLAGS")
            if feature_position is None:
                feature_position = len(result)
            disabled.extend(feature for feature in value.split("=", 1)[1].split(",") if feature)
        else:
            result.append(value)
    # Chromium keeps the last occurrence of a switch. An extra UI feature must not erase the service policy.
    if disabled and feature_position is not None:
        result.insert(feature_position, "--disable-features=" + ",".join(dict.fromkeys(disabled)))
    return result


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
    port = record.get('launch_port', record['port'])
    return f"--user-data-dir={record['profile_dir']}" in line \
        and bool(re.search(rf"(?:^|\s)--remote-debugging-port={port}(?:\s|$)", line))


def _state_file(profile):
    return STATE / f"{profile}.json"


def _write_state(key, record):
    """Other runs scan STATE during startup: publish a complete record in one rename."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=STATE, prefix=".qajev-state-", delete=False) as output:
            temporary = Path(output.name)
            json.dump(record, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, _state_file(key))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
            if running[0].get("password_services_disabled") is not True:
                raise ChromeError("Restart this QAJev Chrome profile to disable its password services")
            if running[0].get("background_services_disabled") is not True:
                raise ChromeError("Restart this QAJev Chrome profile to disable its background services")
            if running[0].get("environment_isolated") is not True:
                command = shlex.join(["qajev", "browser", "stop", f"--profile={profile}"])
                raise ChromeError(f"Restart this QAJev Chrome profile to isolate its environment: run {command}")
            return running[0]
    # A loaded machine (load 300+) has taken over 30 s to open the debugging port.
    wait = wait or float(os.environ.get("QAJEV_CHROME_START_WAIT", "60"))
    port = free_port() if port is None else port
    if port and listening(port):
        raise ChromeError(f"port {port} is already in use")
    if ephemeral:
        # The owner's pid is in the name, so reap() can find this profile if its owner dies without cleaning up.
        EPHEMERAL_ROOT.mkdir(parents=True, exist_ok=True)
        profile_dir = Path(tempfile.mkdtemp(prefix=f"qajev-{profile}-{os.getpid()}-", dir=EPHEMERAL_ROOT))
    else:
        profile_dir = PROFILES / profile
        profile_dir.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        if ephemeral and port == 0:
            temporary = Path(tempfile.mkdtemp(prefix=f"qajev-tmp-{os.getpid()}-", dir=TEMP_ROOT))
        return _launch(profile, profile_dir, port, headless, visible, ephemeral, wait,
                       **({"temporary": temporary} if temporary is not None else {}))
    except BaseException:
        if ephemeral:
            shutil.rmtree(profile_dir, ignore_errors=True)  # a failed start must not leave its temp profile
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        raise


def run_record(owned):
    """What a run's report says about the Chrome it started (or reused): a throwaway one is called that, not by the
    profile name it was started under."""
    return {"cdp_url": owned["cdp_url"], "managed": True, "profile": owned["profile"], "headless": owned["headless"],
            "port": owned["port"], "ephemeral": bool(owned.get("ephemeral")),
            **({k: owned[k] for k in ("state_key", "profile_dir", "temp_dir")}
               if owned.get("launch_port") == 0 else {})}


def _disable_password_services(profile_dir: Path) -> None:
    """Prepare password and dictionary-service preferences only in this owned profile."""
    default = profile_dir / "Default"
    preferences = default / "Preferences"
    if profile_dir.is_symlink() or default.is_symlink() or preferences.is_symlink():
        raise ChromeError("QAJev refuses linked Chrome preferences; use a dedicated profile")
    temporary = None
    try:
        default.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            data = json.loads(preferences.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except ValueError:
            raise ChromeError("QAJev Chrome preferences are not valid JSON") from None
        if not isinstance(data, dict) or any(
            not isinstance(data.get(key, {}), dict) for key in ("profile", "browser", "spellcheck", "net")
        ):
            raise ChromeError("QAJev Chrome preferences must contain JSON objects")
        data["credentials_enable_service"] = False
        data["credentials_enable_autosignin"] = False
        data.setdefault("profile", {})["password_manager_leak_detection"] = False
        data.setdefault("browser", {})["enable_spellchecking"] = False
        data.setdefault("spellcheck", {}).update(dictionary="", dictionaries=[], use_spelling_service=False)
        data.setdefault("net", {})["network_prediction_options"] = 2  # Chromium: no speculative preloads/preconnects.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=default,
                                         prefix=".qajev-preferences-", delete=False) as output:
            temporary = Path(output.name)
            json.dump(data, output)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, preferences)
    except OSError:
        raise ChromeError("QAJev could not prepare its Chrome password privacy settings") from None
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


NO_DISPLAY = ("a visible Chrome needs a display, and this Linux has none (DISPLAY and WAYLAND_DISPLAY are unset): "
              "run headless (--headless), or start an X server such as Xvfb")


def display_missing(environ=None):
    """NO_DISPLAY when a visible Chrome cannot start here (Linux with no X or Wayland display), else None."""
    environ = os.environ if environ is None else environ
    if not sys.platform.startswith("linux") or environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"):
        return None
    return NO_DISPLAY


def exit_reason(code, log, log_from=0):
    """Why Chrome stopped before its debugging port opened. Linux Chrome with no display only logs "Missing X server
    or $DISPLAY" and exits with 1, so that case is said in plain words."""
    said = f"Chrome exited with {code} (see {log})"
    try:
        with open(log, "rb") as f:
            f.seek(log_from)
            written = f.read(65536).decode(errors="replace")
    except OSError:
        return said
    return f"{said}: {NO_DISPLAY}" if "Missing X server or $DISPLAY" in written else said


def _launch(profile, profile_dir, port, headless, visible, ephemeral, wait, *, temporary=None):
    _disable_password_services(profile_dir)
    isolated = port == 0 and ephemeral
    extra = flags()
    if isolated and any(_switch_name(flag) in {"user-data-dir", "remote-debugging-port",
                                              "remote-debugging-pipe"} for flag in extra):
        raise ChromeError("parallel Chrome flags cannot override its profile or debug port")
    args = [binary(), f"--user-data-dir={profile_dir}", f"--remote-debugging-port={port}", *extra]
    if headless:
        args.append("--headless=new")
    elif not visible:
        # Off-screen positions get clamped back onto the display on macOS; minimise over CDP instead.
        args.append("--start-minimized")
    args.append("about:blank")
    STATE.mkdir(parents=True, exist_ok=True)
    state_key = f"{profile}.ephemeral-{os.getpid()}-{profile_dir.name.rsplit('-', 1)[-1]}" if ephemeral else profile
    launch_port = port
    env = child_env(temporary=temporary)
    with open(STATE / f"{state_key}.log", "ab") as log:
        log_from = log.tell()  # a profile's log keeps earlier starts: read only what this one writes
        proc = subprocess.Popen(
            niced(args), stdout=subprocess.DEVNULL, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
            env=env,
        )
    deadline = time.monotonic() + wait
    try:
        while time.monotonic() < deadline:
            if launch_port == 0:
                try:
                    port = int((profile_dir / "DevToolsActivePort").read_text().splitlines()[0])
                    if not 1 <= port <= 65535:
                        raise ValueError
                except (OSError, ValueError, IndexError):
                    port = 0
            cdp_url = f"http://127.0.0.1:{port}"
            if port and version(cdp_url, timeout=0.5):
                break
            if proc.poll() is not None:
                raise ChromeError(exit_reason(proc.returncode, STATE / f"{state_key}.log", log_from))
            time.sleep(0.2)
        else:
            raise ChromeError(f"Chrome did not open its debugging port within {wait}s")
    except BaseException:
        _kill(proc.pid)
        raise
    record = {
        "profile": profile, "state_key": state_key, "pid": proc.pid, "port": port, "cdp_url": cdp_url,
        "profile_dir": str(profile_dir), "headless": headless, "ephemeral": ephemeral, "owner_pid": os.getpid(),
        "started_at": time.time(),
        "password_services_disabled": True,
        "background_services_disabled": True,
        "environment_isolated": True,
        "launch_port": launch_port, "temp_dir": str(temporary) if temporary else None,
        "pid_ns": pid_namespace(),  # its pids mean these processes only in this namespace (reap)
    }
    try:
        _write_state(state_key, record)
    except BaseException:
        _kill(proc.pid)
        _state_file(state_key).unlink(missing_ok=True)
        raise
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


def _kill(pid, grace=5.0, group=False):
    """group: the whole process group Chrome leads (start() starts it in a session of its own), helpers too."""
    send = os.killpg if group else os.kill
    try:
        send(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if reaped(pid):
            break
        time.sleep(0.05)
    else:
        with contextlib.suppress(ProcessLookupError):
            send(pid, signal.SIGKILL)
        time.sleep(0.1)
        reaped(pid)
    if group:  # helpers that outlived their leader: the group stays theirs until the last one exits
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)


def stop(profile="default", group=False):
    """Stop by state key: a profile name, or the `state_key` of an ephemeral Chrome. group: its whole process group
    (a fresh Chrome whose chain was stopped at its cap or crashed)."""
    path = _state_file(profile)
    if not path.exists():
        return False
    record = json.loads(path.read_text())
    stopped = False
    if owns(record):
        _kill(record["pid"], group=group)
        stopped = True
    path.unlink(missing_ok=True)
    if record.get("ephemeral"):
        shutil.rmtree(record["profile_dir"], ignore_errors=True)
        _remove_temp(record)
    return stopped


def _remove_temp(record):
    path = Path(record["temp_dir"]) if record.get("temp_dir") else None
    if path and path.parent == TEMP_ROOT and re.fullmatch(rf"qajev-tmp-{record.get('owner_pid')}-\w+", path.name):
        shutil.rmtree(path, ignore_errors=True)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pid_namespace(pid="self"):
    """The PID namespace process `pid` lives in (Linux), or None where there are none or it cannot be read."""
    try:
        return os.readlink(PROC / str(pid) / "ns" / "pid")
    except OSError:
        return None


def user_of(pid):
    """The user id process `pid` runs as, or None when it is gone or hidden from this user."""
    try:
        return (PROC / str(pid)).stat().st_uid
    except OSError:
        pass
    try:  # no /proc (macOS)
        out = subprocess.run(["ps", "-o", "uid=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
        return int(out.strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def ours(pid):
    """Process `pid` runs as this user and in this PID namespace: only then is it QAJev's to stop. Seen from another
    namespace, a live run's owner pid can look dead (dash, 2 Nov: a boxtest job's Chrome, as another user)."""
    return user_of(pid) == os.getuid() and pid_namespace(pid) == pid_namespace()


def _stop_orphan(pid):
    """Stop a process a dead run left; False when the system refuses it. A refusal never aborts the run that cleans
    up (dash, 2 Nov: a PermissionError on another user's Chrome stopped a QAJev run)."""
    try:
        _kill(pid)
    except PermissionError:
        return False
    return True


def _own_folder(folder):
    """A real folder (not a link) of this user: another user's leftovers in a shared /tmp are not ours to delete."""
    try:
        info = folder.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()


DAEMON_FILE = re.compile(r"^bu-(qajev-(\d+)-[0-9a-f]+)\.pid$")


def reap():
    """Stop what a dead QAJev run left behind: ephemeral Chromes and harness daemons whose owning process is gone
    (killed, crashed, or interrupted twice mid-cleanup). A Chrome is stopped only by the pid in its own run's state
    record, never found by its command line, and only when it runs as this user in this PID namespace."""
    reaped, kept = [], set()
    here = pid_namespace()
    for path in STATE.glob("*.ephemeral-*.json") if STATE.exists() else []:
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue  # another run's reaper took it first
        files = {record.get("profile_dir"), record.get("temp_dir")} - {None}
        if record.get("pid_ns", here) != here or alive(record.get("owner_pid", 0)):
            kept |= files
            continue  # a live run's, or one written in another PID namespace: its pids mean nothing here
        if owns(record):
            if not ours(record["pid"]) or not _stop_orphan(record["pid"]):
                kept |= files
                continue  # still up, and not this user's or namespace's to stop
            reaped.append(f"chrome {record['pid']} ({Path(record['profile_dir']).name})")
        shutil.rmtree(record["profile_dir"], ignore_errors=True)
        _remove_temp(record)
        path.unlink(missing_ok=True)
    stray = [*EPHEMERAL_ROOT.glob("qajev-*-*-*")] if EPHEMERAL_ROOT.exists() else []
    short = [*TEMP_ROOT.glob("qajev-tmp-*-*")]
    for folder in stray + short + [*Path(tempfile.gettempdir()).glob("qajev-*-*-*")]:
        match = re.fullmatch(r"qajev-[\w.]+?-(\d+)-\w+", folder.name)
        if not match or str(folder) in kept or not _own_folder(folder):
            continue
        if not alive(int(match.group(1))):
            shutil.rmtree(folder, ignore_errors=True)
            reaped.append(f"profile {folder.name}")
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
            if not ours(daemon_pid) or not _stop_orphan(daemon_pid):
                continue  # not this user's or namespace's: its files stay too
            reaped.append(f"daemon {daemon_pid} ({name})")
        for leftover in runtime.glob(f"bu-{name}.*"):
            leftover.unlink(missing_ok=True)
    # A daemon can outlive its run and its runtime files; then only the process itself is left to find, by the
    # BU_NAME its run gave it (its owner pid and a random part), which only this user's processes show.
    for pid, command in processes():
        if "browser_harness.daemon" not in command:
            continue
        if any(r.startswith(f"daemon {pid} ") for r in reaped):
            continue
        name = _daemon_name(pid)
        match = re.fullmatch(r"qajev-(\d+)-\w+", name or "")
        if match and ours(pid) and not alive(int(match.group(1))) and alive(pid) and _stop_orphan(pid):
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
