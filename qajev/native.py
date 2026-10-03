"""Native: QA a game (or any app with a QAJev bridge) instead of a web page.

The game runs from its own project, unchanged, in its own process: QAJev adds its bridge next to the game's main
scene (bridges/godot) and talks to it over 127.0.0.1. Jev reads text only, so the bridge (and a per-game adapter
for screens the game draws itself) describes each screen as text plus labelled actions; Jev picks among those
actions with the same chooser it uses on web pages. Input is pushed into the game's own viewport and input map,
never the machine's real mouse or keyboard. Verdicts come from the game's own state, not from Jev's "done".
"""

import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import chrome, verdict
from .config import HOME
from .ledger import CostCapReached

BRIDGES = Path(__file__).parent / "bridges"
ADAPTERS = BRIDGES / "godot" / "adapters"
STATE = HOME / "native"
PORTS = range(9400, 9450)
GODOT = os.environ.get("QAJEV_GODOT") or shutil.which("godot") or "/Applications/Godot.app/Contents/MacOS/Godot"
ERROR_LINE = re.compile(r"^(SCRIPT ERROR|ERROR|USER ERROR|Parse Error)\b")
HARMLESS = re.compile(r"resources still in use at exit|ObjectDB instances leaked at exit")
# Hidden from Jev in a game, on top of the web guard's list (sign out, delete account, buy, billing...): quitting
# ends the test, and wiping saves or progress is not something a QA run should ever do.
# Native apps and games are not web pages: QAJev cannot block their network writes, so more is hidden by name.
# Signing in is the person's job; restoring purchases can raise a store sign-in; a record button listens through the
# host's real microphone (the iOS simulator records from the Mac's).
NATIVE_DENY = [r"\b(sign|log)\s*-?\s*(in|up|on)\b", r"\bcontinue\s+with\s+(google|apple|facebook|email|microsoft)\b",
               r"\bcreate\s+(an\s+)?account\b", r"\brestore\s+purchases?\b",
               r"^\W*(start\s+|stop\s+)?record(ing)?\b(?!s)", r"\bvoice\s+(capture|note|memo)\b",
               # A device's own accounts: "Continue as me@...", sync offers, account pickers (seen on Android Chrome's
               # welcome screen, which offered to sign the browser into the owner's Google account)
               r"^\W*continue\s+as\b", r"\bturn\s+on\s+sync\b", r"\bsync\s+and\s+personali[sz]e\b",
               r"\byes,?\s+i'?m\s+in\b", r"\b(choose|add|switch|use\s+another)\s+(an\s+)?account\b",
               r"[\w.+-]+@[\w-]+\.[\w.-]+"]
GAME_DENY = [r"^\W*(quit|exit)\b", r"\b(quit|exit)\s+(the\s+)?(game|to\s+desktop)\b",
             r"\b(delete|erase|wipe|reset)\s+(the\s+|my\s+|all\s+)?(save|saves|progress|data|profile)\b"]


class NativeError(RuntimeError):
    pass


def with_lists(obs, hide=(), allow=()):
    """A suite's own `hide` and `allow` labels, on top of the adapter's. `allow` lets an exact label past the
    built-in lists (e.g. QUIT, for a test that closes the game on purpose)."""
    if hide:
        obs["hide"] = [*(obs.get("hide") or []), *hide]
    if allow:
        obs["allow"] = [*(obs.get("allow") or []), *allow]
    return obs


def closed_cleanly(game, wait=5.0):
    """The game's process ended by itself with exit code 0 (a quit, not a crash)."""
    proc = getattr(game, "proc", None)
    if proc is None:
        return False
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=wait)
    return proc.poll() == 0


class NoPilot(NativeError):
    """Real-time play needs the adapter's pilot (a game's own bot, say); not a crash."""


def free_port():
    for port in PORTS:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise NativeError(f"no free port in {PORTS.start}-{PORTS.stop - 1}")


def adapter_path(adapter):
    """A bundled adapter by name (suho) or a path to one."""
    if not adapter:
        return None
    path = Path(adapter).expanduser()
    if path.suffix == ".gd" and path.is_file():
        return path.resolve()
    bundled = ADAPTERS / f"{adapter}.gd"
    if bundled.is_file():
        return bundled
    known = sorted(p.stem for p in ADAPTERS.glob("*.gd"))
    raise NativeError(f"no adapter {adapter!r}: give a .gd path or one of {known}")


SAVE_NOT_ISOLATED = "QAJEV_SAVE_NOT_ISOLATED"


def seed_folder(project, seed):
    """A suite's `seed:` (saves to start from, e.g. a legacy save) as a folder inside the game's own `qa/` folder;
    anything else is refused, symlinks that lead out of it included. qajev_boot.gd copies its contents into
    user:// once, on the first launch, after it has checked user:// is the throwaway folder."""
    qa = (Path(project) / "qa").resolve()
    folder = (Path(project) / seed).resolve()
    if not folder.is_relative_to(qa) or not folder.is_dir():
        raise NativeError(f"seed {seed!r} must be a folder inside the game's qa/ folder ({qa})")
    for path in folder.rglob("*"):
        if not path.resolve().is_relative_to(qa):
            raise NativeError(f"seed {seed!r}: {path.relative_to(folder)} leads outside the game's qa/ folder")
    return folder


def save_isolation(user_dir):
    """The environment that puts a Godot game's user:// (saves, settings) inside `user_dir`. Godot 4 has no
    --user-data-dir: it derives user:// from HOME (macOS: ~/Library/Application Support/..., also with a custom
    user dir; Linux: XDG_DATA_HOME, else ~/.local/share). qajev_boot.gd checks the result before the game runs.
    QAJEV_USER_DIR names the folder for a game that keeps files elsewhere."""
    home = str(Path(user_dir) / "home")
    Path(home).mkdir(parents=True, exist_ok=True)
    return {"HOME": home, "XDG_DATA_HOME": f"{home}/.local/share", "XDG_CONFIG_HOME": f"{home}/.config",
            "XDG_CACHE_HOME": f"{home}/.cache", "QAJEV_USER_DIR": str(user_dir)}


class GodotGame:
    """A Godot game started by QAJev, with its bridge. Use as a context manager."""

    def __init__(self, project, *, adapter=None, headless=False, size=(1280, 720), start_wait=60.0, env=None,
                 hide=None, allow=None, seed=None):
        self.project = Path(project).expanduser().resolve()
        if not (self.project / "project.godot").is_file():
            raise NativeError(f"{self.project} is not a Godot project (no project.godot)")
        self.seed = seed_folder(self.project, seed) if seed else None
        self.adapter = adapter_path(adapter)
        self.hide, self.allow = list(hide or []), list(allow or [])
        self.headless, self.size, self.start_wait = headless, size, start_wait
        self.env = dict(env or {})  # extra settings for the game, e.g. SUHO_FORCE_MOBILE=1
        self.proc = self.sock = self.file = None
        self.errors, self.log = [], []
        self.user_dir = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self):
        self.port = free_port()
        # A throwaway save folder: a test never touches the player's real save. The owner pid is in its name,
        # so the reaper can find what a dead run left behind. A relaunch keeps it: the saves of the first launch
        # are what the next one continues from; the seed goes in only once, on the first launch.
        STATE.mkdir(parents=True, exist_ok=True)
        (HOME / "tmp").mkdir(parents=True, exist_ok=True)
        first = self.user_dir is None
        if first:
            self.user_dir = tempfile.mkdtemp(prefix=f"qajev-native-{os.getpid()}-", dir=HOME / "tmp")
        godot_dir = BRIDGES / "godot"
        env = {**os.environ, **self.env, **save_isolation(self.user_dir), "QAJEV_BRIDGE_PORT": str(self.port),
               "QAJEV_BRIDGE_SCRIPT": str(godot_dir / "qajev_bridge.gd"),
               "QAJEV_ADAPTER": str(self.adapter or ""), "QAJEV_WINDOW": f"{self.size[0]}x{self.size[1]}",
               "QAJEV_SEED_DIR": str(self.seed) if first and self.seed else ""}
        self.errors, self.log = [], []
        args = [GODOT, "--path", str(self.project), "--script", str(godot_dir / "qajev_boot.gd")]
        args += ["--headless"] if self.headless else ["--resolution", f"{self.size[0]}x{self.size[1]}"]
        self.proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     stdin=subprocess.DEVNULL, start_new_session=True,
                                     preexec_fn=lambda: os.nice(10))
        threading.Thread(target=self._read_output, daemon=True).start()
        self.record = {"pid": self.proc.pid, "owner_pid": os.getpid(), "engine": "godot", "project": str(self.project),
                       "adapter": self.adapter.stem if self.adapter else None, "port": self.port,
                       "headless": self.headless, "started_at": time.time(), "user_dir": self.user_dir}
        (STATE / f"{self.proc.pid}.json").write_text(json.dumps(self.record))
        deadline = time.monotonic() + self.start_wait
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                time.sleep(0.2)  # its last lines
                refused = next((line for line in self.log if line.startswith(SAVE_NOT_ISOLATED)), None)
                if refused:
                    raise NativeError("refused to start the game: its save folder would be the player's real one ("
                                      + refused[len(SAVE_NOT_ISOLATED):].strip(" :") + ")")
                unseeded = next((line for line in self.log if line.startswith("QAJEV_SEED_FAILED")), None)
                if unseeded:
                    raise NativeError(f"could not seed the save folder: {unseeded.split(':', 1)[1].strip()}")
                raise NativeError(f"the game exited with {self.proc.returncode} before its bridge came up: "
                                  + " | ".join(self.log[-5:]))
            try:
                self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        else:
            self.close()
            raise NativeError(f"no bridge on port {self.port} within {self.start_wait:.0f} s")
        self.sock.settimeout(15)
        self.file = self.sock.makefile("rw")
        self.boot_seconds = round(time.monotonic() - (deadline - self.start_wait), 2)
        return self

    def _read_output(self):
        if self.proc is None or self.proc.stdout is None:
            return
        for line in self.proc.stdout:
            line = line.rstrip()
            self.log.append(line)
            del self.log[:-200]
            if ERROR_LINE.search(line) and not HARMLESS.search(line):
                self.errors.append(line[:300])

    def call(self, **request):
        if self.file is None:
            raise NativeError("the game is not running")
        try:
            self.file.write(json.dumps(request) + "\n")
            self.file.flush()
            line = self.file.readline()
        except OSError as e:
            raise NativeError(f"lost the game: {e}") from None
        if not line:
            raise NativeError("the game closed its bridge (crashed or quit)")
        return json.loads(line)

    def observe(self):
        return with_lists(self.call(op="observe"), self.hide, self.allow)

    def act(self, action):
        if action.get("kind") == "key" or "key" in action and "x" not in action:
            return self.call(op="act", key=action["key"])
        return self.call(op="act", click=[action["x"], action["y"]])

    def shot(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        answer = self.call(op="shot", path=str(path.resolve()))
        return path if answer.get("ok") else None

    def relaunch(self):
        """Quit the game and start it again on the same save folder (save, relaunch, Continue)."""
        self.close(keep_saves=True)
        self.proc = None
        return self.start()

    def close(self, keep_saves=False):
        if self.file is not None:
            with contextlib.suppress(Exception):
                self.file.write(json.dumps({"op": "quit"}) + "\n")
                self.file.flush()
        for closer in (self.file, self.sock):
            if closer is not None:
                with contextlib.suppress(Exception):
                    closer.close()
        self.file = self.sock = None
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.proc.pid, signal.SIGTERM)
                try:
                    self.proc.wait(5)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(self.proc.pid, signal.SIGKILL)
        if self.proc is not None:
            (STATE / f"{self.proc.pid}.json").unlink(missing_ok=True)
        if self.user_dir and not keep_saves:
            shutil.rmtree(self.user_dir, ignore_errors=True)


def running():
    """QAJev's native games that are up now (for `qajev top` and the reaper)."""
    out = []
    for path in STATE.glob("*.json") if STATE.exists() else []:
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        record["alive"] = chrome.alive(record["pid"])
        record["owner_alive"] = chrome.alive(record.get("owner_pid", 0))
        out.append(record)
    return out


def reap():
    """Stop games whose QAJev run died, and forget records of games that are gone."""
    reaped = []
    for record in running():
        path = STATE / f"{record['pid']}.json"
        if record.get("udid") and not record["owner_alive"]:  # an iOS simulator clone: it outlives any process
            for verb in ("shutdown", "delete"):
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    subprocess.run(["xcrun", "simctl", verb, record["udid"]], capture_output=True, timeout=60)
            reaped.append(f"ios simulator clone {record['udid']} ({record.get('project')})")
        elif record["alive"] and not record["owner_alive"]:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(record["pid"], signal.SIGKILL)
            reaped.append(f"game {record['pid']} ({Path(record['project']).name})")
        if not record["alive"] or not record["owner_alive"]:
            path.unlink(missing_ok=True)
            shutil.rmtree(record.get("user_dir") or "/nonexistent", ignore_errors=True)
    from . import mobile  # clones whose run died before it could write a record

    return reaped + [r for r in mobile.reap_clones() if r.split(" (")[0] not in {x.split(" (")[0] for x in reaped}]


# ---- Jev on a game ----

def guarded(label):
    from . import guard as guard_mod

    return any(re.search(p, label or "", re.I) for p in [*guard_mod.DENY, *guard_mod.MIC, *NATIVE_DENY, *GAME_DENY])


# The device browser's own controls (Safari, Chrome): not part of the site under test. Live, Jev found no page link,
# tapped Safari's address bar and typed a made-up URL.
BROWSER_CHROME = (
    r"^address$", r"^search or (enter website name|type web address)$", r"\btabs?\b", r"^share$", r"bookmarks?",
    r"^page (menu|settings)$", r"^clear text$", r"^customi[sz]e and control", r"^reader\b", r"^reload\b",
)


def visible_actions(obs):
    """The actions Jev may take: everything the game offers except guarded ones. -> (allowed, hidden count)."""
    actions = obs.get("actions") or []
    hide = {str(h).strip().lower() for h in obs.get("hide") or []}  # the game's own no-go list (e.g. online play)
    # An exact label a suite allows past the built-in lists (e.g. "See plans and subscribe" only opens a view).
    allow = {str(h).strip().lower() for h in obs.get("allow") or []}
    allowed = [a for a in actions if str(a.get("label")).strip().lower() not in hide
               and (not guarded(a.get("label")) or str(a.get("label")).strip().lower() in allow)]
    if obs.get("readonly"):  # a website on a device: no write guard in the page, so hide what looks like a write
        from . import guard as guard_mod

        allowed = [a for a in allowed if not any(re.search(p, a.get("label") or "", re.I)
                                                 for p in (*guard_mod.MUTATING, *BROWSER_CHROME))]
    return allowed, len(actions) - len(allowed)


def game_name(game):
    """What Jev may call the game in its URLs: the adapter's name or the app's, never the step's (a step named
    "... back to the title" reads to Jev as already being there)."""
    source = getattr(game, "adapter", None) or getattr(game, "project", None)
    stem = Path(source).stem if source else "game"
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "game"


def jev_state(obs, name):
    """The observation as the web-page shape Jev's chooser reads: text plus labelled actions."""
    actions = []
    for i, a in enumerate(visible_actions(obs)[0]):
        label = a["label"]
        if label.strip().upper() in ("DONE", "BLOCKED"):  # Jev's own signals: a button named so reads as "finished"
            label = f"the '{label.strip()}' button" + (" (closes this screen)" if label.strip().upper() == "DONE"
                                                       else "")
        # Every action is a "click" to Jev, except a text field ("fill", so it can choose to type): its chooser knows
        # web kinds only, and an unknown kind (a key, say) made it answer DONE instead (measured on I'm Him's
        # settings: DONE 0.53 -> the action 0.64). QAJev maps the choice back by id and sends the real input.
        if a.get("kind") == "fill":
            item = {"id": a["id"], "kind": "fill", "label": label, "node": i, "role": "textbox",
                    "value": a.get("value", "")}
        else:
            item = {"id": a["id"], "kind": "click", "label": label, "node": i}
            if "checked" in a:
                item["checked"] = a["checked"]
        actions.append(item)
    state_lines = [f"{k}: {v}" for k, v in (obs.get("state") or {}).items()]
    text = "\n".join([f"Screen: {obs.get('screen')}", *(obs.get("texts") or []), *state_lines])
    return {"url": f"game://{name}/{obs.get('screen', '').lower().replace(' ', '-')}", "title": obs.get("screen"),
            "text": text[:6000], "actions": actions}


def native_checks(expect, obs, errors):
    """Checks from the game's own state: screen, texts, state values, frame rate, errors."""
    out = []
    if "screen" in expect:
        want = expect["screen"]
        ok = obs.get("screen") == want
        out.append({"check": f"screen is {want}", "ok": ok, "detail": None if ok else f"on {obs.get('screen')}"})
    # What the screen shows, as a person reads it: its texts and what its buttons say.
    haystack = " \n".join([*(obs.get("texts") or []), *(a.get("label", "") for a in obs.get("actions") or [])])
    for t in expect.get("text") or []:
        ok = t in haystack
        what = "page" if obs.get("readonly") else "game"  # readonly: a website in a device's browser
        out.append({"check": f"{what} shows '{t}'", "ok": ok, "detail": None if ok else haystack[:200]})
    for key, want in (expect.get("state") or {}).items():
        have = (obs.get("state") or {}).get(key)
        ok = compare(have, want)
        out.append({"check": f"state {key} {want}", "ok": ok, "detail": None if ok else f"is {have!r}"})
    if expect.get("min_fps") is not None:
        fps = obs.get("fps") or 0
        ok = fps >= float(expect["min_fps"])
        out.append({"check": f"at least {expect['min_fps']} fps", "ok": ok, "detail": None if ok else f"{fps} fps"})
    if expect.get("no_errors", True):
        out.append({"check": "no engine or script errors", "ok": not errors,
                    "detail": errors[0] if errors else None})
    if expect.get("closed"):  # a game that still answers has not closed (play() records a clean exit itself)
        out.append({"check": "the game closed", "ok": False, "detail": "still running"})
    return out


def compare(have, want):
    """want is a value (equal) or a string with an operator: ">= 3", "< 100", "!= 0"."""
    if isinstance(want, str):
        m = re.fullmatch(r"\s*(>=|<=|!=|==|>|<)\s*(-?[\d.]+)\s*", want)
        if m:
            try:
                a, b = float(have), float(m.group(2))
            except (TypeError, ValueError):
                return False
            return {">=": a >= b, "<=": a <= b, "!=": a != b, "==": a == b, ">": a > b, "<": a < b}[m.group(1)]
    return have == want


def game_image(game):
    """The game's screen as a data URL, for Clef (vision.py). A headless Godot game draws nothing to show."""
    from . import vision

    fd, name = tempfile.mkstemp(suffix=".png", prefix="qajev-look-")
    os.close(fd)
    try:
        shot = game.shot(Path(name))
        if not shot:
            raise vision.VisionError("no picture of the game: it runs headless (vision and looks need its window)")
        raw = Path(shot).read_bytes()
    except NativeError as e:
        raise vision.VisionError(f"no picture of the game: {e}") from None
    finally:
        for leftover in {Path(name), Path(name).with_suffix(".png")}:
            leftover.unlink(missing_ok=True)
    return vision.data_url(raw)


def looks_checks(game, statements, ledger, obs):
    """`expect: {looks: [...]}` on a game or app screen: each statement judged by Clef from its screenshot."""
    from . import session as session_mod
    from . import vision

    jev = session_mod.load(ledger)
    return vision.look(jev.model.post_json, game_image(game), statements,
                       {"game": game_name(game), "screen": obs.get("screen")})


def _fingerprint(obs):
    return obs.get("screen"), json.dumps(obs.get("state"), sort_keys=True, default=str)


def _step(emit, name, ledger, doing, **extra):
    """A progress event: what is happening right now and the money spent so far (qajev top, MCP progress)."""
    if callable(emit):
        emit({"event": "step", "scenario": name, "doing": doing, "at": time.time(),
              "spent_usd": round(ledger.spent(), 5) if ledger is not None else None, **extra})


def _decided(emit, name, decision, actions, screen):
    """Each decision as it is made, for qajev top's decisions view: what it chose (by label), how sure, the
    runner-up, how many options it had and how long the model took."""
    if not callable(emit):
        return
    labels = {a["id"]: a["label"] for a in actions}
    probs = decision.get("probabilities") or {}
    chose = decision.get("choice")
    runner = next(((k, p) for k, p in sorted(probs.items(), key=lambda kv: kv[1] or 0, reverse=True) if k != chose),
                  (None, None))
    emit({"event": "decision", "scenario": name, "at": time.time(), "screen": screen,
          "chose": labels.get(chose, chose), "p": probs.get(chose), "runner_up": labels.get(runner[0], runner[0]),
          "runner_up_p": runner[1], "options": len(actions), "ms": decision.get("latency_ms")})


def _playing(t, obs):
    state = obs.get("state") or {}
    parts = [f"{k.replace('_', ' ')} {round(state[k]) if isinstance(state[k], float) else state[k]}"
             for k in TIMELINE_KEYS if k in state]
    if isinstance(state.get("core_hp"), (int, float)) and state.get("core_max_hp"):
        parts = [x if not x.startswith("core hp") else f"core {round(state['core_hp'])}/{round(state['core_max_hp'])}"
                 for x in parts]
    return f"playing {t:.0f}s: " + " · ".join(parts + [f"{round(obs.get('fps') or 0)} fps"])


def play(game, *, name, goal, expect, budget, ledger, run_dir=None, shots=True, settle=0.4, emit=None, wait=10.0,
         poll=0.5, vision=False):
    """One scenario on a running game: Jev pursues `goal` (if any), then the checks judge the game's state.
    vision: Clef sees the game's screenshot with every decision."""
    from . import session as session_mod
    from . import vision as vision_mod

    os.environ.setdefault("BU_NAME", f"qajev-native-{os.getpid()}")  # the jev modules read it at import
    started = time.monotonic()
    result = {"name": name, "url": f"game://{name}", "goal": goal, "mode": "native", "checks": [], "findings": [],
              "screens": [], "history": [], "jev": None, "shot": None}
    decisions, history, stop, detail = [], [], None, None
    where = game_name(game)
    obs = game.observe()
    # looks are judged once, at the end (a model call each): never a reason to stop early
    early = {k: v for k, v in (expect or {}).items() if k != "looks"}
    seeing = vision_mod.seeing((lambda: game_image(game)) if vision else None)
    seeing.__enter__()
    try:
        if goal:
            jev = session_mod.load(ledger)
            reasks = 0
            while True:
                if early and verdict.all_ok(native_checks(early, obs, game.errors)):
                    stop = "reached"
                    break
                if len(history) >= budget["actions"]:
                    stop, detail = "budget_actions", f"{len(history)} actions"
                    break
                if time.monotonic() - started > budget["seconds"]:
                    stop, detail = "budget_seconds", f"{budget['seconds']:.0f} s"
                    break
                state = jev_state(obs, where)
                decision = jev.model.choose(state, goal, history)
                decisions.append(decision)
                _decided(emit, name, decision, visible_actions(obs)[0], obs.get("screen"))
                choice = decision["choice"]
                p = (decision.get("probabilities") or {}).get(choice)
                if choice == "DONE":
                    _step(emit, name, ledger, f"said DONE on {obs.get('screen')}", p=p, n=len(history) + 1)
                    stop = "done"
                    break
                if choice == "BLOCKED":
                    _step(emit, name, ledger, f"said BLOCKED on {obs.get('screen')}; looking again", p=p)
                    if reasks < 2:  # a screen mid-transition: look again
                        reasks += 1
                        time.sleep(1.0)
                        obs = game.observe()
                        continue
                    stop = "blocked"
                    break
                allowed, hidden = visible_actions(obs)
                result["guard_hidden"] = max(result.get("guard_hidden", 0), hidden)
                action = next((a for a in allowed if a["id"] == choice), None)
                if action is None:
                    stop, detail = "browser_error", f"Jev chose {choice!r}, not on the screen"
                    break
                typed = None
                if action.get("kind") == "fill":  # what to type: Jev's own text helper, from the goal
                    page = {"title": state["title"], "text": state["text"]}
                    typed, _helper = jev.model.field_text(jev.model.field_context(goal, action, page, history))
                    _step(emit, name, ledger, f"type {typed!r} into {action['label']!r}", p=p, n=len(history) + 1)
                    action = {**action, "text": typed}
                else:
                    _step(emit, name, ledger, f"{action.get('kind', 'click')} {action['label']!r} on "
                                              f"{obs.get('screen')}", p=p, n=len(history) + 1)
                before = _fingerprint(obs)
                game.act(action)
                time.sleep(settle)
                obs = game.observe()
                history.append({"step": len(history) + 1, "action": action["label"],
                                "kind": action.get("kind", "click"), "text": typed,
                                "url": f"game://{where}/{obs.get('screen')}",
                                "page_changed": _fingerprint(obs) != before,
                                "probability": (decision.get("probabilities") or {}).get(choice),
                                "latency_ms": decision.get("latency_ms")})
        else:
            # A check-only step may land on a screen still loading (a page opening in the device's browser): give
            # the expectations a few seconds, as a web run does.
            until = time.monotonic() + wait
            while early and not verdict.all_ok(native_checks(early, obs, game.errors)) and time.monotonic() < until:
                time.sleep(poll)
                obs = game.observe()
            stop = "checked"
    except CostCapReached as e:
        stop, detail = "cost_cap", str(e)
    except NativeError as e:
        stop, detail = "browser_error", str(e)
        if (expect or {}).get("closed") and closed_cleanly(game):  # the step's own aim: the game quit, exit 0
            stop, detail, result["closed"] = "reached", None, True
    except (RuntimeError, ValueError) as e:
        stop, detail = ("model_error" if "Model" in str(e) or "TypeSafe" in str(e) else "browser_error"), str(e)
    finally:
        seeing.__exit__(None, None, None)
    if stop != "browser_error" and not result.get("closed"):
        with contextlib.suppress(NativeError):
            obs = game.observe()
        if shots and run_dir is not None:
            with contextlib.suppress(NativeError):
                shot = game.shot(Path(run_dir) / "shots" / f"{re.sub(r'[^A-Za-z0-9._-]+', '-', name)}.jpg")
                result["shot"] = str(shot.relative_to(run_dir)) if shot else None
    if result.get("closed"):
        checks = [{"check": "the game closed", "ok": True, "detail": "exit code 0"},
                  {"check": "no engine or script errors", "ok": not game.errors,
                   "detail": game.errors[0] if game.errors else None}]
    else:
        checks = native_checks(expect or {}, obs, game.errors) if stop != "browser_error" else []
        if (expect or {}).get("looks") and stop not in verdict.HARNESS_STOPS:
            try:
                checks += looks_checks(game, expect["looks"], ledger, obs)
            except (RuntimeError, ValueError) as e:  # no picture, or Clef unreachable: QAJev's side, no verdict
                stop, detail = "model_error", f"could not judge looks: {e}"
    outcome, reason = verdict.classify(stop, checks, has_checks=bool(expect), stop_detail=detail)
    result.update(checks=checks, stop=stop, outcome=outcome, reason=reason,
                  end_url=f"game://{name}/{obs.get('screen')}",
                  page_says=" | ".join([f"Screen: {obs.get('screen')}", *(obs.get("texts") or [])])[:600],
                  history=history[-15:], screens=verdict.screens(decisions),
                  findings=[{"severity": "S2", "kind": "engine error", "detail": e, "scenario": name, "url": None}
                            for e in game.errors[:10]]
                  + [{**f, "scenario": name, "url": None} for f in obs.get("findings") or []],
                  state=obs.get("state"), fps=obs.get("fps"), seconds=round(time.monotonic() - started, 2))
    if goal:
        result["jev"] = {"status": stop, "actions": len(history), "decisions": len(decisions), "text_calls": 0,
                         "elapsed_ms": round((time.monotonic() - started) * 1000)}
    return result


# ---- real-time play: the pilot steers, Jev decides, QAJev watches ----

TIMELINE_KEYS = ("score", "kills", "level", "core_hp", "enemies", "weapons")
# After a pick, the same decision (same screen, same offers) may stay up while the game closes it; within this
# window it is not a new decision. Two real decisions in a row with identical offers wait this long at most.
PICK_SETTLE_S = 2.5


def play_for(game, *, name, seconds, until=None, decide=None, expect=None, ledger=None, run_dir=None, shots=True,
             sample=0.5, stall_after=6.0, emit=None, every=2.0, overlay_limit=90.0, pilot_wait=30.0,
             pilot_poll=0.5, strict_decisions=False, vision=False, seen=None):
    """Play in real time for up to `seconds` (or `until` the game state matches): the adapter's pilot steers each
    frame; Jev makes every decision the game stops for (`decide` is its goal there); every `sample` seconds QAJev
    records fps, frame time, memory and the game state, and flags a soft-lock (the game stops advancing while
    nothing is waiting for the player), a crash and engine errors. A decision Jev does not make is taken with the
    first offer to keep the game going; with `strict_decisions` it instead ends the step as a fail, nothing
    clicked, so a route run is evidence only when Jev made every choice. vision: Clef sees the screen with each
    decision. seen: the game's problems already reported in this session (run_session shares one set), so a problem
    fails the step it happened in, not every step after it."""
    from . import session as session_mod
    from . import vision as vision_mod

    os.environ.setdefault("BU_NAME", f"qajev-native-{os.getpid()}")
    started = time.monotonic()
    result = {"name": name, "url": f"game://{name}", "goal": decide, "mode": "native", "checks": [], "findings": [],
              "screens": [], "history": [], "jev": None, "shot": None}
    timeline, decisions, history, findings = [], [], [], []
    where = game_name(game)
    stop, detail, obs = None, None, {}
    jev = session_mod.load(ledger) if decide else None
    last_tick, still_since, paused_since, overlay_since, told = None, None, None, None, None
    picked = None  # (the decision's screen and offers, when): the last decision QAJev acted on
    fell_back = None  # strict_decisions: the decision Jev did not make, in words
    reported = set()  # the problems this step reports
    seen = set() if seen is None else seen
    seeing = vision_mod.seeing((lambda: game_image(game)) if vision and decide else None)
    seeing.__enter__()
    try:
        until_pilot = time.monotonic() + pilot_wait  # a game still loading has not mounted its bot yet
        while not game.call(op="pilot", on=True).get("pilot"):
            if time.monotonic() > until_pilot:
                raise NoPilot(f"no pilot after {pilot_wait:.0f} s: real-time play needs the adapter's pilot")
            time.sleep(pilot_poll)
        while True:
            t = time.monotonic() - started
            obs = game.observe()
            state = obs.get("state") or {}
            perf = obs.get("perf") or {}
            timeline.append({"t": round(t, 1), "screen": obs.get("screen"), "fps": obs.get("fps"),
                             "frame_ms": round(perf.get("frame_ms", 0), 1),
                             "memory_mb": round(perf.get("memory_mb", 0), 1),
                             **{k: state.get(k) for k in TIMELINE_KEYS if k in state}})
            if told is None or t - told >= every:  # a pulse for qajev top: the pilot's play, live
                told = t
                _step(emit, name, ledger, _playing(t, obs), pulse=True)
            # What the game's own watchdog says, once each. An adapter lists its recent problems on every look
            # (I'M HIM!: the last 20), so one from an earlier step comes back here: it was reported then. Its time
            # is part of what it is, so the same kind happening again later is a new problem.
            for problem in obs.get("problems") or []:
                key = (problem.get("kind"), problem.get("detail"), problem.get("t"))
                if key not in seen:
                    seen.add(key)
                    reported.add(key)
                    when = f" (game t={problem['t']:.0f}s)" if isinstance(problem.get("t"), (int, float)) else ""
                    findings.append({"severity": "S2", "kind": f"game reported: {problem.get('kind')}",
                                     "detail": f"{problem.get('detail')}"[:300] + when, "scenario": name,
                                     "url": None})
            if until and all(compare(state.get(k), v) for k, v in until.items()):
                stop = "reached"
                break
            if t >= seconds:
                stop = "played"
                break
            if state.get("game_over"):
                stop = "game_over"
                break
            if obs.get("decision"):
                allowed, _hidden = visible_actions(obs)
                offer = (obs.get("screen"), tuple((a.get("id"), a.get("label")) for a in allowed))
                if picked and picked[0] == offer and time.monotonic() - picked[1] < PICK_SETTLE_S:
                    # The decision just picked is still closing (I'M HIM!'s HIRE CV stays ~1 s): asking again got
                    # DONE from Jev, which matched no offer, so the first offer was clicked a second time.
                    last_tick, still_since = None, None
                    time.sleep(0.15)
                    continue
                pick, why, answer = None, None, None
                if jev is not None:
                    decision = jev.model.choose(jev_state(obs, where), decide, history)
                    decisions.append(decision)
                    _decided(emit, name, decision, allowed, obs.get("screen"))
                    answer = decision["choice"]
                    pick = next((a for a in allowed if a["id"] == answer), None)
                    why = decision.get("probabilities", {}).get(answer)
                note = ""
                if pick is None:  # Jev did not pick (or was not asked): the first offer keeps the run going
                    if jev is None:
                        note = " (first offer: Jev was not asked)"
                    elif answer in ("DONE", "BLOCKED"):
                        note = f" (first offer: Jev answered {answer})"
                    else:
                        note = " (first offer: Jev's pick was not on screen)"
                    pick, why = (allowed[0] if allowed else None), None  # a probability for the offer Jev did not pick
                    if strict_decisions:  # the route would no longer be Jev's: stop before taking any offer
                        fell_back = f"at {obs.get('screen')} t={t:.0f}s{note.replace('first offer: ', '')}"
                        stop = "strict"
                        break
                    findings.append({"severity": "S3", "kind": "decision not made by Jev",
                                     "detail": f"at {obs.get('screen')} t={t:.0f}s the first offer was taken"
                                               + (f": Jev answered {answer}" if answer in ("DONE", "BLOCKED") else ""),
                                     "scenario": name, "url": None})
                if pick is not None:
                    picked = (offer, time.monotonic())
                    _step(emit, name, ledger, f"picked {pick['label']}{note}", p=why if not note else None,
                          n=len(history) + 1)
                    game.act(pick)
                    history.append({"step": len(history) + 1, "action": pick["label"], "kind": "click",
                                    "text": None, "url": f"game://{where}/{obs.get('screen')}", "page_changed": True,
                                    "probability": why, "latency_ms": decisions[-1].get("latency_ms")
                                    if decisions else None, "t": round(t, 1)})
                last_tick, still_since = None, None
                time.sleep(0.15)
                continue
            tick = state.get("tick")
            waiting = obs.get("paused") or state.get("settings_open")
            paused_since = (paused_since or time.monotonic()) if waiting else None
            if paused_since and time.monotonic() - paused_since > stall_after:
                # Nobody is at the controls to unpause it: playing on would only watch a still frame. QAJev's
                # trouble (the step before left it paused), not a finding about the game.
                stop, detail = "stale", f"the game stayed paused on {obs.get('screen')}: play needs it running"
                break
            # An overlay the pilot is working through (a dialogue, a QTE, a stall) may hold the clock: not a
            # soft-lock, unless it never ends.
            busy = bool(state.get("overlay"))
            overlay_since = (overlay_since or time.monotonic()) if busy else None
            if overlay_since and time.monotonic() - overlay_since > overlay_limit:
                findings.append({"severity": "S1", "kind": "soft-lock",
                                 "detail": f"stuck on {obs.get('screen')} for {overlay_limit:.0f}s at t={t:.0f}s",
                                 "scenario": name, "url": None})
                stop, detail = "stale", f"stuck on {obs.get('screen')}"
                break
            if tick is not None and tick == last_tick and not waiting and not busy:
                still_since = still_since or time.monotonic()
                if time.monotonic() - still_since > stall_after:
                    findings.append({"severity": "S1", "kind": "soft-lock",
                                     "detail": f"the game stopped advancing at t={t:.0f}s on {obs.get('screen')} "
                                               f"with nothing waiting for the player", "scenario": name, "url": None})
                    stop, detail = "stale", "the game stopped advancing"
                    break
            else:
                still_since = None
            last_tick = tick
            time.sleep(sample)
    except CostCapReached as e:
        stop, detail = "cost_cap", str(e)
    except NoPilot as e:
        stop, detail = "browser_error", str(e)
    except NativeError as e:
        stop, detail = "browser_error", str(e)
        findings.append({"severity": "S1", "kind": "game crashed or closed", "detail": str(e)[:200],
                         "scenario": name, "url": None})
    except (RuntimeError, ValueError) as e:  # the decision model (or vision's picture): QAJev's side
        stop, detail = "model_error", str(e)
    finally:
        seeing.__exit__(None, None, None)
    with contextlib.suppress(NativeError):
        game.call(op="pilot", on=False)
        obs = game.observe()
        if shots and run_dir is not None:
            shot = game.shot(Path(run_dir) / "shots" / f"{re.sub(r'[^A-Za-z0-9._-]+', '-', name)}.jpg")
            result["shot"] = str(shot.relative_to(run_dir)) if shot else None
    stats = _stats(timeline)
    checks = native_checks(expect or {}, obs, game.errors) if stop != "browser_error" else []
    if expect and expect.get("min_fps") is not None and stats:
        low = stats["fps_p10"]
        checks.append({"check": f"frame rate held: 90% of samples at or above {expect['min_fps']} fps",
                       "ok": low >= float(expect["min_fps"]), "detail": f"10th percentile {low} fps"})
    if expect and expect.get("max_memory_growth_mb") is not None and stats:
        grew = stats["memory_growth_mb"]
        checks.append({"check": f"memory grew at most {expect['max_memory_growth_mb']} MB", "ok":
                       grew <= float(expect["max_memory_growth_mb"]), "detail": f"grew {grew} MB"})
    held = stop == "stale" and "paused" in (detail or "")  # never got to play: QAJev's trouble, no verdict on it
    locked = any(f["kind"] == "soft-lock" for f in findings)  # the game froze: a product failure
    if stop != "browser_error" and not held:
        checks.append({"check": "the game kept running (no soft-lock)", "ok": not locked,
                       "detail": detail if locked else None})
    if reported:
        first = next(f for f in findings if f["kind"].startswith("game reported"))
        checks.append({"check": "the game reported no problems (its own checks)", "ok": False,
                       "detail": f"{len(reported)} problem(s), first: {first['detail']}"})
    if strict_decisions:
        checks.append({"check": "Jev made every decision (strict_decisions)", "ok": fell_back is None,
                       "detail": f"a decision Jev did not make, {fell_back}; the step stopped there"
                       if fell_back else None})
    looks = (expect or {}).get("looks")
    if looks and stop not in {"browser_error", "model_error"} and not held:
        try:
            checks += looks_checks(game, looks, ledger, obs)
        except (RuntimeError, ValueError) as e:  # no picture, or Clef unreachable: QAJev's side, no verdict
            stop, detail = "model_error", f"could not judge looks: {e}"
    run_stop = "checked" if locked else {"played": "checked", "game_over": "checked", "strict": "checked",
                                         "reached": "reached"}.get(stop, stop)
    outcome, reason = verdict.classify(run_stop, [] if held else checks, has_checks=bool(checks) and not held,
                                       stop_detail=detail)
    reason += f"; played {stats.get('seconds', 0):.0f} s" + (f", {len(history)} decision(s)" if history else "")
    if stop == "game_over":
        reason += ", ended at game over"
    result.update(checks=checks, stop=stop, outcome=outcome, reason=reason, history=history[-30:],
                  screens=verdict.screens(decisions), findings=findings + [
                      {"severity": "S2", "kind": "engine error", "detail": e, "scenario": name, "url": None}
                      for e in game.errors[:10]],
                  state=obs.get("state"), fps=obs.get("fps"), timeline=timeline, stats=stats,
                  end_url=f"game://{name}/{obs.get('screen')}",
                  page_says=" | ".join([f"Screen: {obs.get('screen')}", *(obs.get("texts") or [])])[:600],
                  seconds=round(time.monotonic() - started, 2))
    result["jev"] = {"status": stop, "actions": len(history), "decisions": len(decisions), "text_calls": 0,
                     "elapsed_ms": round((time.monotonic() - started) * 1000)}
    return result


def _stats(timeline):
    if not timeline:
        return {}
    fps = sorted(x["fps"] or 0 for x in timeline)
    frame = sorted(x["frame_ms"] or 0 for x in timeline)
    mem = [x["memory_mb"] or 0 for x in timeline]
    last = timeline[-1]
    return {"seconds": last["t"], "samples": len(timeline), "fps_min": fps[0], "fps_p10": fps[len(fps) // 10],
            "fps_median": fps[len(fps) // 2], "frame_ms_p95": frame[min(len(frame) - 1, int(len(frame) * 0.95))],
            "memory_start_mb": mem[0], "memory_peak_mb": max(mem), "memory_end_mb": mem[-1],
            "memory_growth_mb": round(mem[-1] - mem[0], 1),
            **{f"end_{k}": last.get(k) for k in TIMELINE_KEYS if k in last}}


def _step_result(name, stop, checks, reason=None, **extra):
    outcome, why = verdict.classify(stop, checks, has_checks=bool(checks), stop_detail=reason)
    return {"name": name, "outcome": outcome, "reason": why, "stop": stop, "checks": checks, "findings": [],
            "screens": [], **extra}


def js_step(game, *, name, expression, emit=None):
    """A suite's `js:` step: run an expression in the game's page (a promise is awaited) and record its value. An
    error the script schedules (setTimeout(() => { throw ... })) reaches the page as uncaught, like a real one."""
    run = getattr(game, "run_js", None)
    if run is None:
        return _step_result(name, "browser_error", [], "js steps need an Electron app (a page to run the script in)")
    _step(emit, name, None, f"js {expression[:80]}")
    errors = getattr(game, "errors", [])
    before = len(errors)
    try:
        value = run(expression)
    except NativeError as e:
        if not str(e).startswith("page script failed"):
            return _step_result(name, "browser_error", [], str(e))
        return _step_result(name, "reached", [{"check": "the script ran", "ok": False, "detail": str(e)[:300]}])
    shown = value if isinstance(value, (int, float, bool, type(None))) else str(value)[:500]
    time.sleep(0.5)  # an error the script scheduled lands now: recorded, so a probe can be seen to have fired
    return _step_result(name, "reached", [{"check": "the script ran", "ok": True, "detail": None}], js_result=shown,
                        page_errors=list(getattr(game, "errors", errors)[before:])[:10])


def crash_step(game, *, name, emit=None):
    """A suite's `crash_renderer:` step: crash the game's page renderer on purpose (a crash-report proof). It passes
    when the renderer is gone and the app's main process runs on."""
    crash = getattr(game, "crash_renderer", None)
    if crash is None:
        return _step_result(name, "browser_error", [], "crash_renderer needs an Electron app (a renderer to crash)")
    _step(emit, name, None, "crashing the renderer (Page.crash)")
    alive = crash()
    gone = bool(getattr(game, "renderer_gone", False))
    return _step_result(name, "reached", [
        {"check": "the renderer crashed", "ok": gone, "detail": None if gone else "the page still answers"},
        {"check": "the app is still running", "ok": alive, "detail": None if alive else "the app exited"}])


def idle_for(game, *, name, seconds, emit=None, every=10.0):
    """No input for `seconds` while the game runs on its own (a release build has no bot for a `play` step); it must
    keep answering. -> None, or the step's harness result when it stopped (crashed or closed). After a
    crash_renderer step there is no page to ask: the app's process must stay up instead."""
    started = told = time.monotonic()
    gone = getattr(game, "renderer_gone", False)
    while (left := seconds - (time.monotonic() - started)) > 0:
        time.sleep(min(1.0, left))
        try:
            if gone:
                if game.proc.poll() is not None:
                    raise NativeError(f"the app exited (code {game.proc.poll()})")
            else:
                game.observe()
        except NativeError as e:
            waited = time.monotonic() - started
            return {"name": name, "outcome": "harness", "stop": "browser_error", "checks": [], "screens": [],
                    "findings": [{"severity": "S1", "kind": "game crashed or closed", "detail": str(e)[:200],
                                  "scenario": name, "url": None}],
                    "reason": f"the game stopped answering after {waited:.0f} s of {seconds:.0f} s idle: {e}"}
        if time.monotonic() - told >= every:
            told = time.monotonic()
            _step(emit, name, None, f"idle {told - started:.0f} of {seconds:.0f} s, no input")
    return None


def _answers(game):
    """Is the game or device still there? A failed tap or launch is not a lost device."""
    try:
        game.observe()
        return True
    except NativeError:
        return False


def relaunch_step(game, step, *, name, ledger, run_dir, shots, emit, vision=False):
    """A `relaunch:` step: quit the game and start it again on the same save folder (save, relaunch, Continue);
    then the step's goal and checks, if it has any."""
    if callable(emit):
        emit({"event": "start", "scenario": name})
    spent = ledger.spent()
    again = getattr(game, "relaunch", None)
    if again is None:
        return _step_result(name, "browser_error", [], "relaunch needs a Godot game")
    _step(emit, name, ledger, "relaunching on the same saves")
    try:
        again()
    except NativeError as e:
        return _step_result(name, "browser_error", [], f"the game did not start again: {e}")
    started = {"check": "the game started again on the same saves", "ok": True, "detail": None}
    if step.get("goal") or step.get("expect"):
        with contextlib.suppress(NativeError):
            game.call(op="pilot", on=False)
        r = play(game, name=name, goal=step.get("goal"), expect=step.get("expect") or {},
                 budget={"actions": 20, "seconds": 90, **(step.get("budget") or {})}, ledger=ledger,
                 run_dir=run_dir, shots=shots, emit=emit, vision=bool(step.get("vision", vision)))
        r["checks"] = [started, *r.get("checks", [])]
    else:
        r = _step_result(name, "reached", [started])
    r["cost_usd"] = round(ledger.spent() - spent, 5)
    return r


def select_steps(steps, only):
    """--only NAME: the named steps of a session, plus the steps they name in `depends_on` and every `setup: true`
    step (a launch check, a seeded save), in the suite's order. Steps keep their names, numbered as in the full
    suite, so a report and a rerun say the same thing."""
    names = [s.get("name") or f"step {i + 1}" for i, s in enumerate(steps)]
    by_name = dict(zip(names, steps))
    missing = [n for n in only if n not in by_name]
    if missing:
        raise NativeError(f"no step named {missing}; the suite has {names}")
    keep, todo = set(), list(only)
    while todo:
        name = todo.pop()
        if name in keep:
            continue
        keep.add(name)
        needs = by_name[name].get("depends_on") or []
        unknown = [n for n in needs if n not in by_name]
        if unknown:
            raise NativeError(f"step {name!r} depends on {unknown}, which the suite does not have")
        todo.extend(needs)
    return [{**s, "name": n} for n, s in zip(names, steps) if n in keep or s.get("setup")]


def run_session(game, steps, *, ledger, run_dir, shots=True, emit=None, vision=False):
    """Several steps in one game session, in order: `goal` steps (Jev on the UI) and `play` steps (real-time
    play). A step that loses the game ends the session; the rest are skipped."""
    results = []
    unopened = None  # an app or page that would not open: the steps that use it are skipped until the next open
    fresh = 0  # where the current launch's steps begin: a relaunch revives a game an earlier step closed or lost
    problems = set()  # the game's problems already reported this launch (play_for's `seen`)
    for i, step in enumerate(steps):
        name = step.get("name") or f"step {i + 1}"
        if step.get("skip"):
            r = {"name": name, "outcome": "skipped", "reason": step["skip"], "checks": [], "findings": [],
                 "screens": []}
            results.append(r)
            if callable(emit):
                emit({"event": "scenario", "result": r})
            continue
        if step.get("relaunch"):
            r = relaunch_step(game, step, name=name, ledger=ledger, run_dir=run_dir, shots=shots, emit=emit,
                              vision=vision)
            if r.get("stop") != "browser_error":
                fresh = len(results)
                problems = set()  # a new process: its watchdog starts again, and so do its problems
            results.append(r)
            if callable(emit):
                emit({"event": "scenario", "result": r})
            continue
        if step.get("open"):
            unopened = None
        elif unopened:
            results.append({"name": name, "outcome": "skipped", "reason": f"{unopened} did not open",
                            "checks": [], "findings": [], "screens": []})
            continue
        if any(r.get("closed") for r in results[fresh:]):
            results.append({"name": name, "outcome": "skipped", "reason": "the game was closed by an earlier step",
                            "checks": [], "findings": [], "screens": []})
            continue
        if getattr(game, "renderer_gone", False) and not step.get("idle"):  # only waiting makes sense now
            results.append({"name": name, "outcome": "skipped", "checks": [], "findings": [], "screens": [],
                            "reason": "the game's renderer was crashed by an earlier step"})
            continue
        if results[fresh:] and results[-1].get("stop") == "browser_error" and not _answers(game):
            results.append({"name": name, "outcome": "skipped", "reason": "the game was lost in an earlier step",
                            "checks": [], "findings": [], "screens": []})
            continue
        if callable(emit):
            emit({"event": "start", "scenario": name})
        spent = ledger.spent()
        if step.get("open"):  # mobile: switch to another app or page on the same device first
            try:
                game.call(op="open", target=step["open"])
            except NativeError as e:
                r = {"name": name, "outcome": "harness", "stop": "browser_error", "checks": [], "findings": [],
                     "screens": [], "reason": f"could not open {step['open']}: {e}"}
                unopened = step["open"]
                results.append(r)
                if callable(emit):
                    emit({"event": "scenario", "result": r})
                continue
        if step.get("js") is not None:
            r = js_step(game, name=name, expression=str(step["js"]), emit=emit)
        elif step.get("crash_renderer"):
            r = crash_step(game, name=name, emit=emit)
        elif step.get("idle") and getattr(game, "renderer_gone", False):
            r = idle_for(game, name=name, seconds=float(step["idle"]), emit=emit) or _step_result(
                name, "reached", [{"check": "the app kept running", "ok": True, "detail": None}])
        elif step.get("play"):
            p = step["play"]
            r = play_for(game, name=name, seconds=float(p.get("seconds", 60)), until=p.get("until"),
                         decide=p.get("decide"), expect=step.get("expect"), ledger=ledger, run_dir=run_dir,
                         shots=shots, emit=emit, strict_decisions=bool(p.get("strict_decisions")),
                         vision=bool(step.get("vision", vision)), seen=problems)
        else:
            with contextlib.suppress(NativeError):  # Jev has the controls: a game's bot would undo its moves
                game.call(op="pilot", on=False)
            budget = {"actions": 20, "seconds": 90, **(step.get("budget") or {})}
            # idle: the game runs untouched for that long first (then the step's goal, if any, and its checks)
            r = (step.get("idle") and idle_for(game, name=name, seconds=float(step["idle"]), emit=emit)) or play(
                game, name=name, goal=step.get("goal"), expect=step.get("expect") or {}, budget=budget,
                ledger=ledger, run_dir=run_dir, shots=shots, emit=emit, vision=bool(step.get("vision", vision)))
        r["cost_usd"] = round(ledger.spent() - spent, 5)
        results.append(r)
        if callable(emit):
            emit({"event": "scenario", "result": r})
    return results
