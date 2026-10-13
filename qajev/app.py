"""App: QA any native app that carries a QAJev bridge, started from a play suite's `app:`.

The app speaks the Godot bridge's protocol (bridges/godot/qajev_bridge.gd): one JSON line per request on
127.0.0.1:$QAJEV_BRIDGE_PORT, with observe, act, shot and quit. So everything after the launch is the Godot driver's:
Jev's choices, the checks on the app's own state, shots, relaunch. Only how the app starts differs: the suite names
its command, and QAJev gives it a throwaway home folder (Twinbox Desktop, 13 Oct).

    app:
      command: ["target/debug/twinbox-desktop"]   # relative to the project folder, or absolute
      env: {TWINBOX_API: "http://127.0.0.1:8790"}
      home_env: TWINBOX_DESKTOP_HOME               # also named the throwaway folder, for an app that reads its own
      start_wait: 60                               # seconds until the bridge must answer
"""

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import chrome
from .config import HOME
from .lowprio import niced
from .native import STATE, GodotGame, NativeError, free_port, save_isolation

APP_KEYS = {"command", "env", "home_env", "start_wait"}
# An app's own log: a line logged at ERROR or FATAL (Rust's tracing and log, most loggers), and a Rust panic. Not a
# line that only says "error" in its text.
APP_ERROR = re.compile(r"(^|\s)(ERROR|FATAL)(\s|:|$)|\bpanicked at\b")
NOTHING = re.compile(r"(?!)")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def app_spec(value):
    """A suite's `app:` checked, or NativeError naming what is wrong."""
    if not isinstance(value, dict):
        raise NativeError("app: must be a mapping with command: (and env:, home_env:, start_wait:)")
    unknown = sorted(set(value) - APP_KEYS)
    if unknown:
        raise NativeError(f"app: unknown key(s) {unknown}; known: {sorted(APP_KEYS)}")
    command = value.get("command")
    if isinstance(command, str):
        command = [command]
    if not isinstance(command, list) or not command or not all(isinstance(c, (str, int, float)) for c in command):
        raise NativeError("app.command: the program and its arguments, as a list of strings")
    env = value.get("env") or {}
    if not isinstance(env, dict) or not all(ENV_NAME.fullmatch(str(k)) for k in env):
        raise NativeError("app.env: a mapping of environment variable names to values")
    home_env = value.get("home_env")
    if home_env is not None and not ENV_NAME.fullmatch(str(home_env)):
        raise NativeError(f"app.home_env: {home_env!r} is not an environment variable name")
    if home_env and str(home_env) in env:
        raise NativeError(f"app.home_env: QAJev sets {home_env} to the throwaway folder; do not also set it in app.env")
    try:
        start_wait = float(value.get("start_wait", 60))
    except (TypeError, ValueError):
        start_wait = 0
    if not 1 <= start_wait <= 600:
        raise NativeError("app.start_wait: seconds, from 1 to 600")
    return {"command": [str(c) for c in command], "env": {str(k): str(v) for k, v in env.items()},
            "home_env": str(home_env) if home_env else None, "start_wait": start_wait}


class AppProcess(GodotGame):
    """A native app started by QAJev from its command, with its bridge. Use as a context manager."""

    engine = "app"
    error_line, harmless = APP_ERROR, NOTHING

    def __init__(self, project, spec, *, size=(1280, 720), env=None, args=None, hide=None, allow=None):
        self.project = Path(project).expanduser().resolve()
        if not self.project.is_dir():
            raise NativeError(f"{self.project} is not a folder: give the app's project folder")
        self.spec = app_spec(spec)
        exe = Path(self.spec["command"][0]).expanduser()
        if not exe.is_absolute():
            exe = self.project / exe
        if not exe.is_file() or not os.access(exe, os.X_OK):
            raise NativeError(f"app.command: no program at {exe} (build the app first, or fix the path)")
        self.command = [str(exe), *self.spec["command"][1:], *(args or [])]
        self.adapter = self.seed = None
        self.hide, self.allow = list(hide or []), list(allow or [])
        self.headless, self.size, self.start_wait = False, size, self.spec["start_wait"]
        self.env = {**self.spec["env"], **(env or {})}
        self.proc = self.sock = self.file = None
        self.errors, self.log = [], []
        self.user_dir = None

    def start(self):
        self.port = free_port()
        STATE.mkdir(parents=True, exist_ok=True)
        (HOME / "tmp").mkdir(parents=True, exist_ok=True)
        # A throwaway home: the app never reads or writes the person's real one. A relaunch keeps it, so what the
        # first launch saved is what the next one reads (a setting that must survive a restart).
        if self.user_dir is None:
            self.user_dir = tempfile.mkdtemp(prefix=f"qajev-app-{os.getpid()}-", dir=HOME / "tmp")
        env = {**os.environ, **self.env, **save_isolation(self.user_dir), "QAJEV_BRIDGE_PORT": str(self.port),
               "QAJEV_WINDOW": f"{self.size[0]}x{self.size[1]}"}
        if self.spec["home_env"]:
            app_home = Path(self.user_dir) / "app"
            app_home.mkdir(exist_ok=True)
            env[self.spec["home_env"]] = str(app_home)
        self.errors, self.log = [], []
        self.proc = subprocess.Popen(niced(self.command), cwd=self.project, env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, stdin=subprocess.DEVNULL,
                                     start_new_session=True)
        threading.Thread(target=self._read_output, daemon=True).start()
        self.record = {"pid": self.proc.pid, "owner_pid": os.getpid(), "engine": "app", "project": str(self.project),
                       "command": Path(self.command[0]).name, "adapter": None, "port": self.port, "headless": False,
                       "started_at": time.time(), "user_dir": self.user_dir,
                       "identity": chrome.identity(self.proc.pid)}  # the reaper kills only this very process
        (STATE / f"{self.proc.pid}.json").write_text(json.dumps(self.record))
        return self._wait_for_bridge()

    def _exited(self):
        return (f"the app exited with {self.proc.returncode} before its bridge came up: "
                + (" | ".join(self.log[-5:]) or "it printed nothing"))
