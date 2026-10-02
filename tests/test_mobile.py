import pytest

from qajev import mobile, native

IOS_ELEMENTS = [
    {"type": "Application", "AXLabel": "Foley", "frame": {"x": 0, "y": 0, "width": 402, "height": 874}},
    {"type": "Heading", "AXLabel": "Settings", "frame": {"x": 16, "y": 60, "width": 200, "height": 40}},
    {"type": "StaticText", "AXLabel": "Plan your week", "frame": {"x": 16, "y": 120, "width": 300, "height": 20}},
    {
        "type": "Button",
        "AXLabel": "Privacy policy",
        "enabled": True,
        "frame": {"x": 16, "y": 200, "width": 200, "height": 44},
    },
    {
        "type": "Button",
        "AXLabel": "Off screen",
        "enabled": True,
        "frame": {"x": 16, "y": 2000, "width": 200, "height": 44},
    },
    {"type": "Button", "AXLabel": "Greyed", "enabled": False, "frame": {"x": 16, "y": 260, "width": 200, "height": 44}},
    {
        "type": "TextField",
        "AXLabel": "Task name",
        "AXValue": "",
        "enabled": True,
        "frame": {"x": 16, "y": 320, "width": 300, "height": 44},
    },
    {
        "type": "SecureTextField",
        "AXLabel": "Password",
        "enabled": True,
        "frame": {"x": 16, "y": 380, "width": 300, "height": 44},
    },
    {
        "type": "Switch",
        "AXLabel": "Reminders",
        "AXValue": "1",
        "enabled": True,
        "frame": {"x": 300, "y": 440, "width": 60, "height": 30},
    },
]

ANDROID_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node index="0" text="" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" clickable="false" enabled="true">
 <node text="Settings" class="android.widget.TextView" bounds="[40,100][600,180]" clickable="false" enabled="true"/>
 <node text="" content-desc="" class="android.widget.LinearLayout" bounds="[0,300][1080,450]" clickable="true"
  enabled="true">
  <node text="About phone" class="android.widget.TextView" bounds="[40,320][600,380]" clickable="false" enabled="true"/>
 </node>
 <node text="" class="android.widget.EditText" hint="Search settings" bounds="[40,500][1040,600]" clickable="true"
  enabled="true" password="false"/>
 <node text="" class="android.widget.EditText" hint="PIN" bounds="[40,700][1040,800]" clickable="true" enabled="true"
  password="true"/>
 <node text="Off" class="android.widget.Switch" checkable="true" checked="false" bounds="[900,900][1040,980]"
  clickable="true" enabled="true"/>
</node></hierarchy>"""


def test_an_ios_screen_becomes_text_and_tappable_actions():
    obs = mobile.ios_screen(IOS_ELEMENTS, (402, 874))
    assert obs["screen"] == "SETTINGS" and "Plan your week" in obs["texts"]
    by_label = {a["label"]: a for a in obs["actions"]}
    assert set(by_label) == {"Privacy policy", "Task name", "Reminders"}  # not off screen, disabled or secret
    assert by_label["Privacy policy"]["kind"] == "click" and (
        by_label["Privacy policy"]["x"],
        by_label["Privacy policy"]["y"],
    ) == (116, 222)
    assert by_label["Task name"]["kind"] == "fill" and by_label["Reminders"]["checked"] is True
    assert any("Password" in t and "never types" in t for t in obs["texts"])


def test_an_android_screen_names_unlabelled_rows_by_their_text_and_never_offers_a_password():
    obs = mobile.android_screen(ANDROID_XML)
    by_label = {a["label"]: a for a in obs["actions"]}
    assert set(by_label) == {"About phone", "Search settings", "Off"}
    assert (by_label["About phone"]["x"], by_label["About phone"]["y"]) == (540, 375)
    assert by_label["Search settings"]["kind"] == "fill" and by_label["Off"]["checked"] is False
    assert any("PIN" in t and "never types" in t for t in obs["texts"])


def test_mobile_targets():
    assert mobile.parse_target("ios:com.hanamorilabs.foley") == ("ios", "com.hanamorilabs.foley", None)
    assert mobile.parse_target("android:https://example.com/") == (
        "android",
        "com.android.chrome",
        "https://example.com/",
    )
    assert mobile.is_mobile("ios:x") and not mobile.is_mobile("/games/x")
    with pytest.raises(native.NativeError):
        mobile.parse_target("windows:x")


def test_native_apps_never_offer_sign_in_restore_or_recording():
    for label in (
        "Continue with Google",
        "Sign in",
        "Sign up with Apple",
        "Restore purchases",
        "Record",
        "Start recording",
        "Tap to speak",
        "Subscribe to Pro",
    ):
        assert native.guarded(label), label
    for label in ("Settings", "Privacy policy", "Plan", "Calendar", "Recordings list header"):
        assert not native.guarded(label), label


def test_a_microphone_prompt_only_offers_dont_allow():
    obs = {
        "texts": ['"Foley" Would Like to Access the Microphone'],
        "actions": [
            {"id": "a", "label": "Allow", "kind": "click"},
            {"id": "b", "label": "Don’t Allow", "kind": "click"},
            {"id": "c", "label": "OK", "kind": "click"},
        ],
    }
    assert [a["label"] for a in mobile.refuse_listening(obs)["actions"]] == ["Don’t Allow"]
    calm = {"texts": ["Settings"], "actions": [{"id": "c", "label": "OK", "kind": "click"}]}
    assert mobile.refuse_listening(calm)["actions"] == calm["actions"]


def test_the_reaper_deletes_a_dead_runs_simulator_clone(tmp_path, monkeypatch):
    # Seen for real: a run killed mid-probe left its clone booted, and the reaper only forgot the record.
    import json

    monkeypatch.setattr(native, "STATE", tmp_path)
    (tmp_path / "999999.json").write_text(json.dumps({
        "pid": 999999, "owner_pid": 999999, "engine": "ios", "project": "com.apple.Preferences",
        "udid": "AAAA-BBBB", "user_dir": str(tmp_path / "gone")}))
    calls = []
    monkeypatch.setattr(native.subprocess, "run", lambda args, **kw: calls.append(args)
                        or native.subprocess.CompletedProcess(args, 0, "{}", ""))
    reaped = native.reap()
    assert ["xcrun", "simctl", "shutdown", "AAAA-BBBB"] in calls and ["xcrun", "simctl", "delete", "AAAA-BBBB"] in calls
    assert reaped == ["ios simulator clone AAAA-BBBB (com.apple.Preferences)"]
    assert not (tmp_path / "999999.json").exists()


def test_the_reaper_finds_clones_that_have_no_record(monkeypatch):
    import json as _json

    listing = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-5": [
        {"name": "qajev-999999", "udid": "DEAD-1", "state": "Booted"},       # its run is gone
        {"name": f"qajev-{__import__('os').getpid()}", "udid": "LIVE-1", "state": "Booted"},  # this process: alive
        {"name": "Foley release check iPhone", "udid": "KEEP-1", "state": "Shutdown"}]}}
    calls = []

    def run(args, **kw):
        calls.append(args)
        out = _json.dumps(listing) if args[:3] == ["xcrun", "simctl", "list"] else ""
        return __import__("subprocess").CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(mobile.subprocess, "run", run)
    monkeypatch.setattr(mobile.shutil, "which", lambda name: "/usr/bin/xcrun")
    assert mobile.reap_clones() == ["ios simulator clone DEAD-1 (qajev-999999)"]
    touched = {a[3] for a in calls if a[:2] == ["xcrun", "simctl"] and a[2] in ("shutdown", "delete")}
    assert touched == {"DEAD-1"}  # never a live run's clone, never anyone else's simulator


def test_a_session_step_can_open_another_app_or_page_on_the_same_device(monkeypatch):
    opened = []

    class Device:
        errors = []

        def call(self, **request):
            opened.append(request)
            return {"ok": True}

        def observe(self):
            return {"screen": "HOME", "texts": ["Welcome"], "actions": [], "state": {}}

        def shot(self, _path):
            return None

    class Ledger:
        def spent(self):
            return 0.0

    results = native.run_session(Device(), [{"name": "safari", "open": "http://127.0.0.1:8765/",
                                             "expect": {"text": ["Welcome"]}}], ledger=Ledger(), run_dir=None,
                                 shots=False)
    assert {"op": "open", "target": "http://127.0.0.1:8765/"} in opened and results[0]["outcome"] == "pass"


def test_open_resolves_an_app_or_a_page_in_the_platforms_browser():
    app = mobile.MobileApp.__new__(mobile.MobileApp)
    app.platform = "ios"
    assert app._resolve("com.hanamorilabs.foley") == ("com.hanamorilabs.foley", None)
    assert app._resolve("http://127.0.0.1:8765/") == ("com.apple.mobilesafari", "http://127.0.0.1:8765/")


def test_a_suite_hides_app_specific_controls_and_can_allow_one_exact_label():
    # Foley: the plan composer would send to its organiser; "See plans and subscribe" only opens the offer view.
    obs = {"texts": ["Settings"], "hide": ["Plan my day, or ask for a change", "Share a project"],
           "allow": ["See plans and subscribe"],
           "actions": [{"id": "a", "label": "Plan my day, or ask for a change", "kind": "fill"},
                       {"id": "b", "label": "Share a project", "kind": "click"},
                       {"id": "c", "label": "See plans and subscribe", "kind": "click"},
                       {"id": "d", "label": "Subscribe now", "kind": "click"},
                       {"id": "e", "label": "Privacy policy", "kind": "click"}]}
    allowed, hidden = native.visible_actions(obs)
    assert [a["label"] for a in allowed] == ["See plans and subscribe", "Privacy policy"] and hidden == 3


def test_an_empty_or_broken_android_dump_is_a_device_problem_not_a_crash():
    for bad in ("", "<hierarchy><node", "ERROR: could not get idle state."):
        with pytest.raises(native.NativeError, match="UI Automator"):
            mobile.android_screen(bad)


def test_ios_tabs_and_text_that_acts_as_a_button_are_tappable():
    # Foley's tab bar read as plain text: the element type said text, its role and traits said button.
    els = [{"type": "Application", "frame": {"x": 0, "y": 0, "width": 402, "height": 874}},
           {"type": "StaticText", "AXLabel": "Timeline", "role": "AXButton", "enabled": True,
            "frame": {"x": 100, "y": 800, "width": 80, "height": 49}},
           {"type": "Other", "AXLabel": "Plan", "traits": ["Button", "Selected"], "enabled": True,
            "frame": {"x": 200, "y": 800, "width": 80, "height": 49}},
           {"type": "StaticText", "AXLabel": "Keep your tasks", "role": "AXStaticText",
            "frame": {"x": 16, "y": 100, "width": 300, "height": 20}}]
    obs = mobile.ios_screen(els, (402, 874))
    assert [a["label"] for a in obs["actions"]] == ["Timeline", "Plan"] and obs["texts"] == ["Keep your tasks"]


def test_taps_and_swipes_go_to_idb_as_whole_points(monkeypatch):
    sent = []
    monkeypatch.setattr(mobile, "_run", lambda args, **kw: sent.append(args) or "")
    app = mobile.MobileApp.__new__(mobile.MobileApp)
    app.platform, app.udid, app.size = "ios", "U", (402, 874)
    app.act({"kind": "click", "x": 201.4, "y": 824.6})
    app.act({"kind": "click", "swipe": "up"})
    assert sent[0][-2:] == ["201", "825"] and all(s.lstrip("-").isdigit() for s in sent[1][-4:])


def test_an_android_app_counts_as_started_when_its_process_runs_whatever_the_launcher_exits(monkeypatch):
    # Issue #1: on an Android 15 image monkey launched the app but exited 251, and QAJev called the launch failed.
    def device(resolve, running=True):
        sent = []

        def run(args, check=True, **kw):
            sent.append(args[3:])
            if args[3:5] == ["shell", "monkey"] and check:
                raise native.NativeError("monkey: ** SYS_KEYS has no physical keys bu")
            if args[3:5] == ["shell", "cmd"]:
                return resolve
            if args[3:5] == ["shell", "pidof"]:
                return "4242\n" if running else ""
            return "Status: ok\n"

        monkeypatch.setattr(mobile, "_run", run)
        monkeypatch.setattr(mobile.time, "sleep", lambda s: None)
        app = mobile.MobileApp.__new__(mobile.MobileApp)
        app.platform, app.serial, app.app = "android", "emulator-5580", "com.hanamorilabs.idoughmath"
        return app, sent

    app, sent = device("priority=0 preferredOrder=0\ncom.hanamorilabs.idoughmath/.MainActivity\n")
    app._launch_android()
    assert ["shell", "am", "start", "-W", "-n", "com.hanamorilabs.idoughmath/.MainActivity"] in sent
    app, sent = device("No activity found\n")  # no resolve-activity: monkey, whose exit code no longer decides
    app._launch_android()
    assert sent[1][:2] == ["shell", "monkey"] and sent[-1][:2] == ["shell", "pidof"]
    app, _ = device("No activity found\n", running=False)
    with pytest.raises(native.NativeError, match="did not start"):
        app._launch_android(wait=0)


def test_an_app_file_is_installed_on_the_throwaway_device_before_it_opens(monkeypatch, tmp_path):
    apk = tmp_path / "foley.apk"
    apk.write_bytes(b"PK")
    sent = []
    monkeypatch.setattr(mobile, "_run", lambda args, **kw: sent.append(args) or "")
    app = mobile.MobileApp.__new__(mobile.MobileApp)
    app.platform, app.serial, app.udid = "android", "emulator-5580", None
    app.install(str(apk))
    assert sent == [[mobile.adb_binary(), "-s", "emulator-5580", "install", "-r", str(apk)]]
    sent.clear()
    app.platform, app.udid = "ios", "U"
    bundle = tmp_path / "Foley.app"
    bundle.mkdir()
    app.install(str(bundle))
    assert sent == [["xcrun", "simctl", "install", "U", str(bundle)]]
    with pytest.raises(native.NativeError, match="no app file"):
        app.install(str(tmp_path / "missing.app"))


def test_labelled_generic_elements_are_tappable_and_reported_as_hidden_from_voiceover():
    # Foley 1.0.66: its tab bar items are GenericElement with no button/tab role or trait.
    els = [{"type": "Application", "frame": {"x": 0, "y": 0, "width": 402, "height": 874}},
           {"type": "GenericElement", "AXLabel": "Timeline", "role": "AXGenericElement", "traits": ["Scrollable"],
            "enabled": True, "frame": {"x": 109, "y": 796, "width": 91, "height": 44}},
           {"type": "GenericElement", "AXLabel": "", "traits": [], "enabled": True,
            "frame": {"x": 0, "y": 0, "width": 402, "height": 874}}]
    obs = mobile.ios_screen(els, (402, 874))
    assert [a["label"] for a in obs["actions"]] == ["Timeline"]
    (finding,) = obs["findings"]
    assert finding["severity"] == "S3" and finding["kind"] == "not exposed as a button to VoiceOver"
    assert "Timeline" in finding["detail"]


def test_a_goal_step_keeps_the_findings_a_screen_reports():
    class Device:
        errors = []

        def observe(self):
            return {"screen": "HOME", "texts": ["Home"], "actions": [], "state": {},
                    "findings": [{"severity": "S3", "kind": "not exposed as a button to VoiceOver",
                                  "detail": "'Timeline'"}]}

        def shot(self, _path):
            return None

    r = native.play(Device(), name="home", goal=None, expect={"text": ["Home"]},
                    budget={"actions": 1, "seconds": 5}, ledger=None)
    assert r["outcome"] == "pass" and [f["kind"] for f in r["findings"]] == ["not exposed as a button to VoiceOver"]


def test_a_check_only_step_waits_for_a_page_still_loading():
    class Loading:
        errors, looks = [], 0

        def observe(self):
            self.looks += 1
            return {"screen": "SAFARI", "texts": ["QAJev fixture"] if self.looks >= 3 else ["Start Page"],
                    "actions": [], "state": {}}

        def shot(self, _path):
            return None

    r = native.play(Loading(), name="site", goal=None, expect={"text": ["QAJev fixture"]},
                    budget={"actions": 1, "seconds": 5}, ledger=None, wait=5, poll=0.01)
    assert r["outcome"] == "pass"


def test_an_android_crash_dialog_is_recorded_and_closed():
    obs = {"texts": ["Chrome keeps stopping"],
           "actions": [{"id": "a", "label": "App info", "kind": "click", "x": 1, "y": 2},
                       {"id": "b", "label": "Close app", "kind": "click", "x": 3, "y": 4}]}
    crash = mobile.system_dialog(obs)
    assert crash == ("Chrome keeps stopping", {"id": "b", "label": "Close app", "kind": "click", "x": 3, "y": 4})
    anr = mobile.system_dialog({"texts": ["Foley isn't responding"],
                                "actions": [{"id": "w", "label": "Wait", "kind": "click"},
                                            {"id": "c", "label": "Close app", "kind": "click"}]})
    assert anr[0] == "Foley isn't responding" and anr[1]["label"] == "Wait"
    assert mobile.system_dialog({"texts": ["Settings"], "actions": []}) is None


def test_a_website_on_a_device_hides_the_browsers_own_controls():
    # iOS Safari, live: Jev found no page link, tapped Safari's address bar and typed a made-up URL. The browser's
    # own controls are not part of the site under test.
    from qajev import native

    labels = ["Address", "Search or enter website name", "Show Tabs", "Tab Overview", "Share", "Bookmarks",
              "Page Menu", "Clear text", "Search or type web address", "Switch or close tabs",
              "Customize and control Google Chrome", "New tab", "Pricing", "Back", "Help"]
    obs = {"readonly": True, "actions": [{"label": label, "kind": "click"} for label in labels]}
    allowed, hidden = native.visible_actions(obs)
    assert [a["label"] for a in allowed] == ["Pricing", "Back", "Help"]
    assert hidden == len(labels) - 3
    # in a native app, its own Share or Address controls stay usable
    app = {"actions": [{"label": "Share", "kind": "click"}, {"label": "Address", "kind": "fill"}]}
    assert len(native.visible_actions(app)[0]) == 2


def test_text_checks_on_a_website_in_a_device_browser_say_page():
    from qajev import native

    web = native.native_checks({"text": ["$29 per month"]}, {"readonly": True, "texts": ["$29 per month"]}, [])
    game = native.native_checks({"text": ["READY"]}, {"texts": ["READY"]}, [])
    assert web[0]["check"] == "page shows '$29 per month'" and web[0]["ok"]
    assert game[0]["check"] == "game shows 'READY'"


def test_a_web_page_is_waited_for_before_jev_looks(monkeypatch):
    # iOS Safari on a just-booted, loaded simulator: 2 s after openurl the page was still blank, so Jev's first
    # decision was BLOCKED. Opening a page now waits until it shows something besides the browser's own controls.
    app = mobile.MobileApp("ios:https://shop.example/", readonly=True)
    blank = {"texts": [], "actions": [{"label": "Address", "kind": "fill"}]}
    page = {"texts": ["Plans"],
            "actions": [{"label": "Address", "kind": "fill"}, {"label": "Pricing", "kind": "click"}]}
    seen = iter([blank, blank, page, page, page])
    monkeypatch.setattr(app, "observe", lambda: next(seen))
    monkeypatch.setattr(mobile.time, "sleep", lambda s: None)
    assert app._wait_for_page(timeout=30) is True
    assert next(seen) == page  # stopped once the page held still: two looks alike after it appeared

    never = mobile.MobileApp("ios:https://shop.example/", readonly=True)
    monkeypatch.setattr(never, "observe", lambda: blank)
    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr(mobile.time, "monotonic", lambda: next(clock))
    assert never._wait_for_page(timeout=30) is False  # gives up; Jev then sees what is there


def test_jev_is_never_offered_to_sign_in_as_a_real_account():
    # Launch check, Android Chrome's welcome screen on the emulator: "Continue as me@<owner>" was offered to Jev,
    # which would sign the device's browser into the owner's Google account.
    from qajev import native

    labels = ["Continue as me@example.com", "me@example.com", "Use without an account", "Yes, I'm in",
              "Turn on sync", "Sync and personalize", "Choose an account", "Add account", "Pricing"]
    obs = {"actions": [{"label": label, "kind": "click"} for label in labels]}
    allowed = [a["label"] for a in native.visible_actions(obs)[0]]
    assert allowed == ["Use without an account", "Pricing"]


def test_chrome_welcome_is_passed_without_an_account(monkeypatch):
    app = mobile.MobileApp("android:https://shop.example/", readonly=True)
    screens = iter([{"texts": ["Welcome to Chrome"], "actions": [
        {"label": "Continue as me@example.com", "kind": "click", "x": 1, "y": 1},
        {"label": "Use without an account", "kind": "click", "x": 2, "y": 2}]},
        {"texts": ["Turn on sync?"], "actions": [{"label": "Yes, I'm in", "kind": "click", "x": 3, "y": 3},
                                                 {"label": "No thanks", "kind": "click", "x": 4, "y": 4}]},
        {"texts": ["Plans"], "actions": [{"label": "Pricing", "kind": "click", "x": 5, "y": 5}]}])
    tapped = []
    monkeypatch.setattr(app, "observe", lambda: next(screens))
    monkeypatch.setattr(app, "act", lambda a: tapped.append(a["label"]))
    monkeypatch.setattr(mobile.time, "sleep", lambda s: None)
    app._browser_intro()
    assert tapped == ["Use without an account", "No thanks"]


def test_an_unreadable_safari_page_is_qajevs_problem_not_the_sites(monkeypatch):
    # iOS 26 Simulator: idb lists only Safari's own controls; the page (drawn in another process) never appears.
    # That must read as harness with a plain reason, never as the site failing.
    app = mobile.MobileApp("ios:https://shop.example/", readonly=True)
    controls = ("Back", "Page Menu", "refresh", "More")
    safari_only = {"texts": [], "actions": [{"label": label, "kind": "click"} for label in controls]}
    monkeypatch.setattr(app, "observe", lambda: safari_only)
    monkeypatch.setattr(mobile.time, "sleep", lambda s: None)
    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr(mobile.time, "monotonic", lambda: next(clock))
    assert app._wait_for_page(timeout=30) is False  # Back / refresh / More are Safari's, not the page's
    with pytest.raises(mobile.NativeError, match="page content"):
        app._page_or_explain()
