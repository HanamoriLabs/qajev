"""Native, mobile: QA an app on the iOS Simulator or an Android emulator.

QAJev starts its own device (a throwaway clone of an iOS simulator, deleted afterwards; an Android emulator in
read-only mode, so nothing it does is saved to the AVD), with the microphone denied. It reads each screen from the
platform's accessibility tree (iOS: idb `ui describe-all`; Android: UI Automator) and describes it as text plus
labelled actions, so Jev can choose as it does on a web page. Taps, swipes, keys and typing go into the device only,
never the Mac's mouse or keyboard. Same interface as native.GodotGame: goal steps, reports and `qajev top` work
unchanged.

What it cannot do, unlike a web page: block network writes in the app. Password and other secure fields are never
offered to Jev, and dangerous controls (sign out, delete, pay...) are hidden by label, but tests that change data
should use a test build and a test account.
"""

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .config import HOME
from .native import STATE, NativeError

SDK = Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or Path.home() / "Library/Android/sdk")
BROWSERS = {"ios": "com.apple.mobilesafari", "android": "com.android.chrome"}
EMULATOR_PORTS = range(5580, 5600, 2)  # console ports; the serial is emulator-<port>; 5554-5578 left to people


def _run(args, timeout=60, check=True, **kw):
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kw)
    except FileNotFoundError:
        raise NativeError(f"{args[0]} not found") from None
    except subprocess.TimeoutExpired:
        raise NativeError(f"{' '.join(map(str, args[:4]))} took over {timeout} s") from None
    if check and done.returncode != 0:
        raise NativeError(f"{' '.join(map(str, args[:4]))}: {(done.stderr or done.stdout).strip()[:300]}")
    return done.stdout


def parse_target(text):
    """'ios:com.example.app' / 'android:com.example.app' / 'ios:https://...' -> (platform, app or None, url or None)."""
    platform, _, rest = str(text).partition(":")
    if platform not in BROWSERS or not rest:
        raise NativeError(f"{text!r}: a mobile target is ios:<bundle id or URL> or android:<package or URL>")
    if rest.startswith(("http://", "https://")):
        return platform, BROWSERS[platform], rest
    return platform, rest, None


def is_mobile(text):
    return str(text).startswith(("ios:", "android:"))


LISTENING = re.compile(r"microphone|speech recognition|record audio|access to your voice", re.I)
YES = re.compile(
    r"^\W*(allow|ok|okay|continue|allow while using( the)? app|only this time|while using the app)\W*$", re.I
)


def refuse_listening(obs):
    """A microphone or speech-recognition prompt: only "Don't Allow" stays on offer. QAJev never listens."""
    if any(LISTENING.search(t) for t in obs.get("texts") or []):
        obs["actions"] = [a for a in obs.get("actions") or [] if not YES.match(a.get("label") or "")]
    return obs


SYSTEM_DIALOG = re.compile(r"(keeps stopping|has stopped|isn.t responding)", re.I)


def system_dialog(obs):
    """Android's own crash / not-responding dialog -> (what it says, the action that dismisses it), else None.
    It is the system's, not the app's: QAJev records it as a crash and closes it, as a person would."""
    said = next((t for t in obs.get("texts") or [] if SYSTEM_DIALOG.search(t)), None)
    if not said:
        return None
    order = ("wait", "close app", "ok") if "respond" in said.lower() else ("close app", "ok", "close")
    labels = {a.get("label", "").strip().lower(): a for a in obs.get("actions") or []}
    choice = next((labels[k] for k in order if k in labels), None)
    return (said, choice) if choice else None


def _center(x, y, w, h):
    return x + w / 2.0, y + h / 2.0


def _actions_guard_text(el_label):
    return " ".join(str(el_label or "").split())[:120]


# ---- iOS ----

IOS_TAPPABLE = {
    "Button",
    "Link",
    "Cell",
    "Switch",
    "Tab",
    "MenuItem",
    "Image",
    "Slider",
    "PopUpButton",
    "Toggle",
    "SegmentedControl",
    "Icon",
    "CheckBox",
    "RadioButton",
    "DisclosureTriangle",
    "PageIndicator",
}
IOS_ROLES = {"AXButton", "AXLink", "AXTab", "AXRadioButton", "AXCheckBox", "AXSwitch", "AXToggle", "AXMenuItem",
             "AXPopUpButton", "AXCell"}
IOS_TRAITS = {"Button", "Link", "Tab", "TabBar", "Adjustable", "Toggle"}
IOS_GENERIC = {"GenericElement", "Other", "Group"}
IOS_FIELDS = {"TextField", "SearchField", "TextView", "TextArea"}
IOS_SECRET = {"SecureTextField"}


def ios_screen(elements, size):
    """The accessibility elements of an iOS screen -> {screen, texts, actions}."""
    texts, actions, seen, unexposed = [], [], set(), []
    heading = None
    w, h = size
    for el in elements:
        kind = el.get("type") or ""
        label = _actions_guard_text(el.get("AXLabel") or el.get("title") or el.get("AXValue"))
        value = _actions_guard_text(el.get("AXValue"))
        f = el.get("frame") or {}
        x, y, fw, fh = f.get("x", 0), f.get("y", 0), f.get("width", 0), f.get("height", 0)
        on_screen = fw > 0 and fh > 0 and x < w and y < h and x + fw > 0 and y + fh > 0
        if kind == "Application" or not on_screen:
            continue
        if kind == "Heading" and label and heading is None:
            heading = label
        # What it does wins over what it is: a tab or a text that acts as a button is tappable (Foley's tab bar was
        # typed as text with a button role).
        traits = {str(t) for t in el.get("traits") or []}
        acts = el.get("role") in IOS_ROLES or bool(traits & IOS_TRAITS)
        if kind in ("StaticText", "Heading") and label and not acts:
            texts.append(label)
            continue
        if kind in IOS_SECRET:
            texts.append(f"{label or 'secure field'} (secure field: QAJev never types into it)")
            continue
        if not el.get("enabled", True) or not label:
            continue
        cx, cy = _center(x, y, fw, fh)
        if kind in IOS_FIELDS:
            item = {
                "kind": "fill",
                "label": label,
                "role": "textbox",
                "value": value if value != label else "",
                "x": cx,
                "y": cy,
            }
        elif kind in IOS_TAPPABLE or acts:
            item = {"kind": "click", "label": label, "role": kind.lower(), "x": cx, "y": cy}
            if kind in ("Switch", "Toggle", "CheckBox"):
                item["checked"] = value in ("1", "on", "true")
        elif kind in IOS_GENERIC and fw < w * 0.9 and fh < h * 0.5:
            # A labelled element with no role: people tap it (Foley's tab bar), but VoiceOver does not say it can be
            # activated. Jev may tap it, and the screen reports it.
            item = {"kind": "click", "label": label, "role": "generic", "x": cx, "y": cy}
            if label not in unexposed:
                unexposed.append(label)
        else:
            if label not in texts:
                texts.append(label)
            continue
        key = (item["kind"], label, round(cx), round(cy))
        if key in seen:
            continue
        seen.add(key)
        item["id"] = f"{item['kind']}:{len(actions)}:{label[:40]}"
        actions.append(item)
    findings = []
    if unexposed:
        findings.append({"severity": "S3", "kind": "not exposed as a button to VoiceOver",
                         "detail": ", ".join(repr(x) for x in unexposed[:8])
                         + ": labelled and on screen, but with no button, tab or link role or trait"})
    return {"screen": (heading or (texts[0] if texts else "")).upper()[:60], "texts": texts, "actions": actions,
            "findings": findings}


# ---- Android ----

BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def android_screen(xml_text):
    """A UI Automator dump -> {screen, texts, actions}. Coordinates are device pixels."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        raise NativeError(f"UI Automator gave no readable screen: {xml_text.strip()[:160]!r}") from None
    texts, actions, seen = [], [], set()
    for node in root.iter("node"):
        text = _actions_guard_text(node.get("text"))
        desc = _actions_guard_text(node.get("content-desc"))
        label = text or desc
        m = BOUNDS.match(node.get("bounds") or "")
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        cls = (node.get("class") or "").rsplit(".", 1)[-1]
        clickable = node.get("clickable") == "true" or node.get("long-clickable") == "true"
        enabled = node.get("enabled") != "false"
        cx, cy = _center(x1, y1, x2 - x1, y2 - y1)
        if node.get("password") == "true":
            name = label or _actions_guard_text(node.get("hint")) or "password field"
            texts.append(f"{name} (secure field: QAJev never types into it)")
            continue
        if cls in ("EditText", "AutoCompleteTextView") and enabled:
            hint = (
                _actions_guard_text(node.get("hint")) or desc or text or node.get("resource-id", "").rsplit("/", 1)[-1]
            )
            item = {
                "kind": "fill",
                "label": hint or "text field",
                "role": "textbox",
                "value": text if text != hint else "",
                "x": cx,
                "y": cy,
            }
        elif clickable and enabled:
            if not label:  # an unlabelled clickable: its labelled children name it
                inner = [
                    _actions_guard_text(c.get("text") or c.get("content-desc"))
                    for c in node.iter("node")
                    if c is not node
                ]
                label = " ".join(t for t in inner if t)[:120]
            if not label:
                continue
            item = {"kind": "click", "label": label, "role": cls.lower(), "x": cx, "y": cy}
            if node.get("checkable") == "true":
                item["checked"] = node.get("checked") == "true"
        else:
            if label and label not in texts:
                texts.append(label)
            continue
        key = (item["kind"], item["label"], round(cx), round(cy))
        if key in seen:
            continue
        seen.add(key)
        item["id"] = f"{item['kind']}:{len(actions)}:{item['label'][:40]}"
        actions.append(item)
    return {"screen": (texts[0] if texts else "").upper()[:60], "texts": texts, "actions": actions}


class MobileApp:
    """An app on a QAJev-owned iOS simulator or Android emulator. Use as a context manager."""

    def __init__(self, target, *, device=None, adapter=None, headless=True, start_wait=240, env=None, hide=(),
                 allow=(), install=None, readonly=False, **_):
        self.platform, self.app, self.url = parse_target(target)
        self.engine = self.platform
        self.project = Path(str(target).replace("/", "_"))  # names the run (game_name) and the report line
        self.adapter = None
        self.hide, self.allow = list(hide or []), list(allow or [])  # a suite's own labels: never / exactly allowed
        self.device_name = device
        self.install_path = install  # an .apk / .app put on the throwaway device before the app opens
        self.readonly = readonly  # a website in the device's browser: write-like buttons are hidden too
        self.headless, self.start_wait = headless, start_wait
        self.proc = None
        self.errors, self.log = [], []
        self.udid = self.serial = self.user_dir = None
        self.size = (0, 0)
        self._last_crash_check = 0.0

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- lifecycle ----
    def start(self):
        started = time.monotonic()
        STATE.mkdir(parents=True, exist_ok=True)
        (HOME / "tmp").mkdir(parents=True, exist_ok=True)
        self.user_dir = tempfile.mkdtemp(prefix=f"qajev-native-{os.getpid()}-", dir=HOME / "tmp")
        try:
            if self.platform == "ios":
                self._start_ios()
            else:
                self._start_android()
            self._record()
            if self.install_path:
                self.install(self.install_path)
            self._open()
        except BaseException:
            self.close()
            raise
        self.boot_seconds = round(time.monotonic() - started, 2)
        return self

    def _record(self):
        pid = self.proc.pid if self.proc else os.getpid()
        self.record = {
            "pid": pid,
            "owner_pid": os.getpid(),
            "engine": self.platform,
            "project": f"{self.app}",
            "adapter": None,
            "port": self.serial or self.udid,
            "headless": self.headless,
            "started_at": time.time(),
            "user_dir": self.user_dir,
            "udid": self.udid,
        }
        (STATE / f"{pid}.json").write_text(json.dumps(self.record))

    def _start_ios(self):
        devices = json.loads(_run(["xcrun", "simctl", "list", "devices", "available", "-j"]))["devices"]
        phones = [d for runtime, ds in devices.items() if "iOS" in runtime for d in ds]
        if not phones:
            raise NativeError("no iOS simulator available (Xcode > Settings > Components)")
        wanted = self.device_name or os.environ.get("QAJEV_IOS_DEVICE")
        source = next((d for d in phones if wanted and wanted in (d["name"], d["udid"])), None)
        if wanted and source is None:
            raise NativeError(f"no iOS simulator named {wanted!r}")
        source = source or next((d for d in phones if d["name"].startswith("iPhone")), phones[0])
        # A throwaway clone: whatever the run does stays in it, and it is deleted afterwards.
        self.udid = _run(["xcrun", "simctl", "clone", source["udid"], f"qajev-{os.getpid()}"]).strip()
        self.source_device = source["name"]
        self._record()  # from here on, a run that dies leaves a record the reaper can act on
        _run(["xcrun", "simctl", "boot", self.udid])
        _run(["xcrun", "simctl", "bootstatus", self.udid, "-b"], timeout=self.start_wait)
        if not self.headless:
            subprocess.run(["open", "-a", "Simulator", "--args", "-CurrentDeviceUDID", self.udid], check=False)

    def _adb(self, *args, timeout=60, check=True):
        # Always -s <this emulator>: a person's own phone is often attached, and a bare adb would pick it.
        if not self.serial:
            raise NativeError("no emulator serial: refusing a bare adb command")
        return _run([adb_binary(), "-s", self.serial, *args], timeout=timeout, check=check)

    def _start_android(self):
        emulator = SDK / "emulator" / "emulator"
        if not emulator.exists():
            raise NativeError(f"no Android emulator at {emulator} (set ANDROID_HOME)")
        avds = _run([str(emulator), "-list-avds"]).split()
        wanted = self.device_name or os.environ.get("QAJEV_ANDROID_AVD")
        if wanted and wanted not in avds:
            raise NativeError(f"no AVD named {wanted!r}; have {avds}")
        avd = wanted or (avds[0] if avds else None)
        if not avd:
            raise NativeError("no Android virtual device (create one in Android Studio)")
        port = next((p for p in EMULATOR_PORTS if not _port_busy(p)), None)
        if port is None:
            raise NativeError("no free emulator port")
        self.serial = f"emulator-{port}"
        self.source_device = avd
        # -read-only: nothing is written back to the AVD; -no-audio: no microphone, no sound.
        args = [
            str(emulator),
            "-avd",
            avd,
            "-port",
            str(port),
            "-read-only",
            "-no-snapshot-save",
            "-no-audio",
            "-no-boot-anim",
            "-netdelay",
            "none",
            "-netspeed",
            "full",
        ]
        if self.headless:
            args.append("-no-window")
        self.proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            preexec_fn=lambda: os.nice(10),
        )
        self._record()  # from here on, a run that dies leaves a record the reaper can act on
        deadline = time.monotonic() + self.start_wait
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise NativeError(f"the emulator exited with {self.proc.returncode} while booting")
            if self._adb("shell", "getprop", "sys.boot_completed", check=False, timeout=10).strip() == "1":
                break
            time.sleep(2)
        else:
            raise NativeError(f"the emulator did not boot within {self.start_wait:.0f} s")
        with contextlib.suppress(NativeError):  # no animations: screens settle at once
            for key in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale"):
                self._adb("shell", "settings", "put", "global", key, "0")

    def _open(self):
        if self.platform == "ios":
            # Every permission denied for the app under test, before it starts: above all the microphone, which on
            # the simulator is the Mac's own. A failure here stops the run rather than risk listening.
            # A just-booted simulator can take a while to answer (seen: over 120 s on a loaded machine).
            _run(["xcrun", "simctl", "privacy", self.udid, "revoke", "all", self.app], timeout=300)
            if self.url:
                _run(["xcrun", "simctl", "openurl", self.udid, self.url])
            else:
                _run(["xcrun", "simctl", "launch", self.udid, self.app])
        else:
            if self.app != BROWSERS["android"] and "package:" + self.app not in self._adb(
                "shell", "pm", "list", "packages", self.app
            ):
                raise NativeError(f"{self.app} is not installed on {self.source_device}")
            if self.url:
                parts = urlsplit(self.url)
                if parts.hostname in ("127.0.0.1", "localhost"):  # the Mac's loopback, reached through adb reverse
                    port = parts.port or 80
                    self._adb("reverse", f"tcp:{port}", f"tcp:{port}")
                    self.url = urlunsplit(parts._replace(netloc=f"127.0.0.1:{port}"))
                self._adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", self.url, self.app)
            else:
                self._adb("shell", "monkey", "-p", self.app, "-c", "android.intent.category.LAUNCHER", "1")
        time.sleep(2.0)
        if self.url:
            self._browser_intro()
            self._page_or_explain()

    # A browser's own first-run screens, passed the one safe way: never an account, never sync.
    INTRO = re.compile(r"welcome to chrome|\bsync\b|make chrome your own|chrome notifications|default browser", re.I)
    INTRO_SAFE = {"use without an account", "no thanks", "no, thanks", "not now", "skip", "no thanks, continue"}

    def _browser_intro(self, passes=4):
        for _ in range(passes):
            try:
                obs = self.observe()
            except NativeError:
                return
            if not any(self.INTRO.search(t or "") for t in obs.get("texts") or []):
                return
            safe = next((a for a in obs.get("actions") or []
                         if str(a.get("label") or "").strip().lower() in self.INTRO_SAFE), None)
            if not safe:
                return  # an intro QAJev does not know: Jev sees it, with account controls hidden
            self.act(safe)
            time.sleep(1.5)

    # Browser controls that may also be page links, so Jev still sees them; they just don't prove a page loaded.
    CHROME_HINTS = {"back", "forward", "refresh", "reload", "more", "page menu", "address", "tabs", "share",
                    "bookmarks"}

    def _page_or_explain(self):
        """Wait for the page; on iOS a page that never shows is QAJev's limit, said plainly (harness, not a fail)."""
        if self._wait_for_page() or self.platform != "ios":
            return
        raise NativeError("Safari's page content is not readable here: on the iOS Simulator QAJev's accessibility "
                          "reader (idb) sees only Safari's own controls, not the page. iOS Safari cannot be checked "
                          "yet; desktop and phone view in Chrome, and Android Chrome, can.")

    def _wait_for_page(self, timeout=25.0):
        """A web page in the device's browser: wait until it shows something besides the browser's own controls and
        holds still (two looks alike). On a just-booted, loaded simulator Safari was still blank 2 s after opening,
        and Jev's first look saw nothing to do. -> whether the page appeared in time."""
        from .native import BROWSER_CHROME

        def content(obs):
            labels = [a.get("label") or "" for a in obs.get("actions") or []]
            own = [x for x in labels if not any(re.search(p, x, re.I) for p in BROWSER_CHROME)
                   and x.strip().lower() not in self.CHROME_HINTS]
            return (tuple(obs.get("texts") or []), tuple(own)) if (obs.get("texts") or own) else None

        deadline, last = time.monotonic() + timeout, None
        while time.monotonic() < deadline:
            try:
                now = content(self.observe())
            except NativeError:
                now = None  # the accessibility tree may not answer while the page loads
            if now and now == last:
                return True
            last = now
            time.sleep(1.5)
        return False

    def install(self, path):
        """Install an app file on this throwaway device (an emulator booted read-only, or a simulator clone), so
        nothing of it outlives the run."""
        path = Path(path).expanduser()
        if not path.exists():
            raise NativeError(f"no app file at {path}")
        if self.platform == "android":
            self._adb("install", "-r", str(path), timeout=300)
        else:
            _run(["xcrun", "simctl", "install", self.udid, str(path)], timeout=300)

    # ---- the GodotGame interface ----
    def call(self, **request):
        if request.get("op") == "pilot":
            return {"ok": True, "pilot": False}  # no pilot on mobile yet: goal steps only
        if request.get("op") == "open":  # another app or page on the same device: one boot for a whole session
            self.app, self.url = self._resolve(str(request["target"]))
            self.errors = []  # crashes belong to the app that had them (earlier steps' reports keep their own)
            self._open()
            return {"ok": True}
        raise NativeError(f"unknown op {request.get('op')!r}")

    def _resolve(self, target):
        """'com.example.app' or a web address -> (app, url) on this device's platform."""
        if target.startswith(("http://", "https://")):
            return BROWSERS[self.platform], target
        return target, None

    def observe(self):
        started = time.monotonic()
        if self.platform == "ios":
            raw = self._describe_ios()
            self._keep_dump(json.dumps(raw, indent=1), "json")
            elements = raw if isinstance(raw, list) else raw.get("elements", [])
            if not self.size[0]:
                app = next((e for e in elements if e.get("type") == "Application"), None)
                f = (app or {}).get("frame") or {}
                self.size = (f.get("width") or 430, f.get("height") or 932)
            obs = ios_screen(elements, self.size)
            foreground = None
        else:
            xml_text = self._dump_android()
            self._keep_dump(xml_text, "xml")
            if not self.size[0]:
                m = re.search(r"(\d+)x(\d+)", self._adb("shell", "wm", "size"))
                self.size = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
            obs = android_screen(xml_text)
            dialog = system_dialog(obs)
            if dialog:  # a crash or ANR dialog: record it, close it, look again
                said, close = dialog
                msg = f"app crash: {said}"
                if msg not in self.errors:
                    self.errors.append(msg)
                self.act(close)
                time.sleep(1.0)
                xml_text = self._dump_android()
                obs = android_screen(xml_text)
            focus = self._adb("shell", "dumpsys", "window", check=False)
            m = re.search(r"mCurrentFocus=\S+ \S+ ([\w.]+)/([\w.$]+)", focus)
            foreground = m.group(1) if m else None
            self._check_crashes()
        obs["actions"] += self._system_actions()
        obs["hide"], obs["allow"], obs["readonly"] = self.hide, self.allow, self.readonly
        refuse_listening(obs)
        obs.update(
            ok=True,
            state={"app": self.app, "foreground": foreground},
            fps=None,
            paused=False,
            size=list(self.size),
            perf={"look_ms": round((time.monotonic() - started) * 1000)},
        )
        if self.url and obs["texts"]:
            obs["texts"].insert(0, f"(in the device's browser, at {self.url})")
        return obs

    def _keep_dump(self, text, ext):
        """QAJEV_MOBILE_DUMP=<folder>: keep every raw accessibility dump, to see how an app describes itself."""
        folder = os.environ.get("QAJEV_MOBILE_DUMP")
        if folder:
            self._dumps = getattr(self, "_dumps", 0) + 1
            Path(folder).mkdir(parents=True, exist_ok=True)
            (Path(folder) / f"{self.platform}-{self._dumps:03d}.{ext}").write_text(text)

    def _dump_android(self, patience=30.0):
        # Into a file on the device, then read back (a dump to /dev/tty came back empty on Android 14), and again for
        # a while: right after boot or between screens UI Automator may report no idle state yet.
        deadline = time.monotonic() + patience
        while True:
            said = self._adb("shell", "uiautomator", "dump", "/data/local/tmp/qajev-ui.xml", check=False, timeout=60)
            xml_text = self._adb("exec-out", "cat", "/data/local/tmp/qajev-ui.xml", check=False, timeout=30)
            start, end = xml_text.find("<?xml"), xml_text.rfind("</hierarchy>")
            if start >= 0 and end > start:
                return xml_text[start : end + len("</hierarchy>")]
            if time.monotonic() > deadline:
                raise NativeError(f"UI Automator gave no screen: {said.strip()[:200]}")
            time.sleep(1.5)

    def _describe_ios(self, patience=20.0):
        # Between screens (an app still drawing, a system sheet sliding in) idb has nothing to translate yet: look
        # again for a few seconds before calling it a failure.
        deadline = time.monotonic() + patience
        while True:
            try:
                return json.loads(_run(["idb", "ui", "describe-all", "--udid", self.udid, "--json"], timeout=60))
            except NativeError as e:
                if "No translation object" not in str(e) or time.monotonic() > deadline:
                    raise
                time.sleep(1.5)

    def _system_actions(self):
        out = [
            {"id": "swipe_up", "label": "Scroll down", "kind": "click", "swipe": "up"},
            {"id": "swipe_down", "label": "Scroll up", "kind": "click", "swipe": "down"},
        ]
        if self.platform == "android":
            out.append({"id": "back", "label": "Go back (the phone's back button)", "kind": "key", "key": "BACK"})
        return out

    def _check_crashes(self):
        if time.monotonic() - self._last_crash_check < 2:
            return
        self._last_crash_check = time.monotonic()
        crash = self._adb("logcat", "-b", "crash", "-d", check=False, timeout=20)
        for line in crash.splitlines():
            if self.app in line and ("FATAL" in line or "Process:" in line):
                msg = f"app crash: {line.strip()[:240]}"
                if msg not in self.errors:
                    self.errors.append(msg)

    def act(self, action):
        if action.get("swipe"):
            w, h = self.size
            y1, y2 = (h * 0.7, h * 0.3) if action["swipe"] == "up" else (h * 0.3, h * 0.7)
            if self.platform == "ios":
                _run(["idb", "ui", "swipe", "--udid", self.udid, str(round(w / 2)), str(round(y1)), str(round(w / 2)),
                      str(round(y2))])
            else:
                self._adb(
                    "shell", "input", "swipe", str(int(w / 2)), str(int(y1)), str(int(w / 2)), str(int(y2)), "300"
                )
            return {"ok": True}
        if action.get("kind") == "key":
            if self.platform == "android":
                self._adb("shell", "input", "keyevent", f"KEYCODE_{action['key']}")
            else:
                _run(["idb", "ui", "key-sequence", "--udid", self.udid, str(action["key"])])
            return {"ok": True}
        x, y = action["x"], action["y"]
        if self.platform == "ios":
            _run(["idb", "ui", "tap", "--udid", self.udid, str(round(x)), str(round(y))])  # idb takes whole points
        else:
            self._adb("shell", "input", "tap", str(int(x)), str(int(y)))
        if action.get("kind") == "fill" and action.get("text") is not None:
            time.sleep(0.4)
            text = str(action["text"])
            if self.platform == "ios":
                _run(["idb", "ui", "text", "--udid", self.udid, text])
            else:  # input text: spaces as %s, shell metacharacters escaped
                safe = re.sub(r"([\\'\"`$&|;<>()*?!#~])", r"\\\1", text).replace(" ", "%s")
                self._adb("shell", "input", "text", safe)
        return {"ok": True}

    def shot(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        png = path.with_suffix(".png")
        try:
            if self.platform == "ios":
                _run(["xcrun", "simctl", "io", self.udid, "screenshot", str(png)], timeout=30)
            else:
                if not self.serial:
                    return None
                with open(png, "wb") as handle:
                    subprocess.run(
                        [adb_binary(), "-s", self.serial, "exec-out", "screencap", "-p"],
                        stdout=handle,
                        timeout=30,
                        check=True,
                    )
            if path.suffix.lower() in (".jpg", ".jpeg") and shutil.which("sips"):
                subprocess.run(
                    ["sips", "-s", "format", "jpeg", "-Z", "1200", str(png), "--out", str(path)],
                    capture_output=True,
                    timeout=30,
                    check=True,
                )
                png.unlink(missing_ok=True)
                return path
            return png
        except (NativeError, OSError, subprocess.SubprocessError):
            return None

    def close(self):
        pid = self.proc.pid if self.proc else os.getpid()
        if self.platform == "ios" and self.udid:
            with contextlib.suppress(NativeError):
                _run(["xcrun", "simctl", "shutdown", self.udid], timeout=60, check=False)
            with contextlib.suppress(NativeError):
                _run(["xcrun", "simctl", "delete", self.udid], timeout=60, check=False)
        if self.platform == "android" and self.proc is not None:
            if self.proc.poll() is None:
                with contextlib.suppress(NativeError):
                    self._adb("emu", "kill", check=False, timeout=20)
                try:
                    self.proc.wait(20)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(self.proc.pid, signal.SIGKILL)
        (STATE / f"{pid}.json").unlink(missing_ok=True)
        if self.user_dir:
            shutil.rmtree(self.user_dir, ignore_errors=True)


CLONE = re.compile(r"^qajev-(\d+)$")


def reap_clones():
    """Delete iOS simulator clones whose QAJev run is gone (named qajev-<owner pid>), even with no record left."""
    if not shutil.which("xcrun"):
        return []
    from .chrome import alive

    try:
        listing = subprocess.run(["xcrun", "simctl", "list", "devices", "-j"], capture_output=True, text=True,
                                 timeout=60).stdout
        devices = [d for ds in json.loads(listing or "{}").get("devices", {}).values() for d in ds]
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    reaped = []
    for d in devices:
        m = CLONE.match(d.get("name") or "")
        if m and not alive(int(m.group(1))):
            for verb in ("shutdown", "delete"):
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    subprocess.run(["xcrun", "simctl", verb, d["udid"]], capture_output=True, timeout=60)
            reaped.append(f"ios simulator clone {d['udid']} ({d['name']})")
    return reaped


BROWSER_NAMES = {"ios": "iOS Safari", "android": "Android Chrome"}
DEVICES = ("desktop", "tall", "phone", "tablet")


def web_steps(suite, platform):
    """A website suite's scenarios as one session in a real device browser: each scenario with its own url opens it;
    a continuation stays on the page, as on desktop. Text checks carry over (the device has no DOM access);
    scenarios that change data are skipped, since a device browser has no in-page write guard."""
    steps = []
    for s in suite.scenarios:
        copy = re.search(r" \(([\w\d]+)\)$", s.name)
        if s.device.get("mobile") or (copy and copy.group(1) in DEVICES):
            continue  # the phone-view copies (and any other device's): on a device, the device is the phone
        step = {"name": f"{s.name} ({BROWSER_NAMES[platform]})", "goal": s.task,
                "expect": {"text": [*s.expect.get("text", []), *s.expect.get("visible", [])]}}
        if not step["expect"]["text"]:
            step["expect"] = {}
        if s.url and not s.depends_on:
            step["open"] = s.url
        if s.mode == "mutate":
            step["skip"] = "changes data: a device browser has no in-page write guard, so it runs on desktop only"
        steps.append(step)
    return steps


def adb_binary():
    bundled = SDK / "platform-tools" / "adb"
    return str(bundled) if bundled.exists() else shutil.which("adb") or "adb"


def _port_busy(port):
    import socket

    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0
