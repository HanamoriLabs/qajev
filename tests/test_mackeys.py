"""Mac keys: real macOS key presses through the qajev-keys helper, only on a QAJev Cloud Mac (Twinbox, 14 Oct)."""

import json

import pytest
import yaml

from qajev import cli, native
from qajev.cli import main
from qajev.mackeys import MAC_CODES, check_keys, chord, keys_step, plan, planned_checks, typed
from qajev.native import NativeError

PASTE = {"app": "com.hanamorilabs.twinbox", "clipboard": "qa-paste-7f3", "press": ["cmd+v"]}


class Helper:
    """A stand-in qajev-keys: answers each command as the real one does, and records every call."""

    def __init__(self, trusted=True, front=True, clip=True, sent=True, knows=("front", "clip", "click", "keys")):
        self.calls, self.trusted, self.front, self.clip, self.sent = [], trusted, front, clip, sent
        self.knows = knows  # an older helper (main before qajev-cloud #36) knows only front

    def __call__(self, args, timeout=60):
        self.calls.append(args[1:])
        head = f"trusted: {'true' if self.trusted else 'false'}"
        what = args[1]
        if what not in self.knows:
            return 2, f"{head}\nusage: qajev-keys press shift|cmd+v, or qajev-keys read"
        if what == "front":
            return (0, f"{head}\nfrontmost: {args[2]}") if self.front else (1, f"{head}\nfrontmost: com.apple.finder")
        if what == "clip":
            return (0, f"{head}\nclipboard: {len(args[2])} characters") if self.clip else (1, f"{head}\nclipboard "
                                                                                              "read back differs")
        if what == "click":
            return 0, f"{head}\nclicked at {args[3]},{args[4]}"
        if what == "keys":
            n = len(args) - 3
            return (0, f"{head}\nsent {n} of {n}") if self.sent else (1, f"{head}\nsent 0 of {n}; frontmost: "
                                                                         "com.apple.finder")
        raise AssertionError(f"unexpected helper command {args}")


class App:
    """A stand-in app for the expect checks after the keys: its state holds the pasted name, or not."""

    def __init__(self, name="qa-paste-7f3", window=None):
        self.name, self.window, self.errors, self.inputs = name, window, [], []

    def observe(self):
        field = {"id": "new-box.name", "label": "Name", "kind": "fill", "x": 20, "y": 30, "value": self.name}
        return {"screen": "new box", "texts": [], "actions": [field],
                "state": {"new_box_name": self.name, **({"window": self.window} if self.window else {})}}

    def act(self, action):
        self.inputs.append(action)

    def call(self, **request):
        self.inputs.append(request)
        return {"ok": True}


@pytest.fixture
def helper_file(tmp_path):
    path = tmp_path / "qajev-keys"
    path.write_text("#!/bin/sh\n")
    return path


def test_keys_and_chords_map_to_mac_key_codes():
    assert chord("cmd+v") == ("cmd+v", "9:cmd")  # kVK_ANSI_V, as the Namespace smoke pasted with
    assert chord("cmd+k") == ("cmd+k", "40:cmd")
    assert chord("Enter") == ("Enter", "36:")
    assert chord("shift+Tab") == ("shift+Tab", "48:shift")
    assert chord("ctrl+alt+shift+a") == ("ctrl+alt+shift+a", "0:ctrl+alt+shift")


def test_every_page_key_has_one_mac_key_code():
    from qajev import keys as page_keys

    codes = {spec["code"] for spec in page_keys.KEYS.values()}
    assert codes == set(MAC_CODES)
    assert len(set(MAC_CODES.values())) == len(MAC_CODES)


def test_text_is_typed_key_by_key_with_shift_for_capitals():
    assert typed("claude --version") == ["8:", "37:", "0:", "32:", "2:", "14:", "49:", "27:", "27:", "9:", "14:",
                                         "15:", "1:", "34:", "31:", "45:"]
    assert typed("Hi") == ["4:shift", "34:"]


@pytest.mark.parametrize("keys, says", [
    ("not a dict", "needs app:"),
    ({**PASTE, "bundle": "x"}, "unknown key"),
    ({**PASTE, "app": "com.x; rm -rf"}, "bundle id"),
    ({**PASTE, "app": "com.apple.Terminal"}, "system app"),
    ({**PASTE, "app": "com.apple.finder"}, "system app"),
    ({**PASTE, "app": "com.apple.Finder"}, "system app"),
    ({**PASTE, "app": "COM.APPLE.TERMINAL"}, "system app"),
    ({**PASTE, "app": "com.apple.keychainaccess"}, "system app"),
    ({**PASTE, "app": "com.apple.ActivityMonitor"}, "system app"),
    ({**PASTE, "app": "com.apple.Safari"}, "system app"),
    ({**PASTE, "app": "com.apple.systemsettings"}, "system app"),
    ({**PASTE, "app": "com.google.Chrome"}, "a browser"),
    ({**PASTE, "app": "com.1password.1password"}, "a password manager"),
    ({**PASTE, "press": ["cmd+alt+d"]}, "system shortcut"),
    ({**PASTE, "clipboard": ""}, "clipboard: text of 1"),
    ({**PASTE, "press": []}, "press: takes 1 to 20"),
    ({**PASTE, "press": ["cmd+q"]}, "refused: 'cmd\\+q' is a system shortcut"),
    ({**PASTE, "press": ["cmd+w"]}, "system shortcut"),
    ({**PASTE, "press": ["cmd+Tab"]}, "system shortcut"),
    ({**PASTE, "press": ["cmd+Space"]}, "system shortcut"),
    ({**PASTE, "press": ["cmd+alt+Escape"]}, "system shortcut"),
    ({**PASTE, "press": ["ctrl+ArrowLeft"]}, "system shortcut"),
    ({**PASTE, "press": ["cmd+cmd+v"]}, "each once"),
    ({**PASTE, "press": ["F13"]}, "unknown key"),
    ({**PASTE, "press": [{"type": "rm -rf /!"}]}, "cannot type '!'"),
    ({**PASTE, "press": [{"type": "x", "enter": True}]}, "a press is a key"),
])
def test_a_wrong_keys_step_is_refused_when_the_suite_loads(keys, says):
    with pytest.raises(NativeError, match=says):
        check_keys([{"name": "paste", "keys": keys}])


@pytest.mark.parametrize("acting", ["goal", "play", "js", "pad", "idle", "relaunch", "handoff"])
def test_a_keys_step_that_would_also_do_something_else_is_refused(acting):
    with pytest.raises(NativeError, match=f"only presses keys, then checks expect:; put {acting}"):
        check_keys([{"name": "paste", "keys": PASTE, acting: "x"}])


def test_the_step_brings_the_app_forward_sets_the_clipboard_and_presses_the_keys(helper_file):
    helper = Helper()
    r = keys_step(App(), name="paste", value=PASTE, helper=helper_file, run=helper)
    assert helper.calls == [["front", "com.hanamorilabs.twinbox"], ["clip", "qa-paste-7f3"],
                            ["keys", "com.hanamorilabs.twinbox", "9:cmd"]]
    assert [(c["check"], c["ok"]) for c in r["checks"]] == [
        ("com.hanamorilabs.twinbox was in front", True), ("the clipboard held the text", True),
        ("the keys were pressed in com.hanamorilabs.twinbox", True)]
    assert r["checks"][2]["says"] == "cmd+v in com.hanamorilabs.twinbox, after putting 'qa-paste-7f3' on the clipboard"
    assert r["outcome"] == "pass"


def test_without_the_helper_nothing_is_pressed_and_the_step_is_harness(tmp_path):
    helper = Helper()
    r = keys_step(App(), name="paste", value=PASTE, helper=tmp_path / "missing", run=helper)
    assert r["outcome"] == "harness" and helper.calls == []
    assert "only on a QAJev Cloud Mac" in r["reason"] and "nothing was pressed" in r["reason"]


def test_an_untrusted_helper_presses_nothing_and_the_step_is_harness(helper_file):
    helper = Helper(trusted=False)
    r = keys_step(App(), name="paste", value=PASTE, helper=helper_file, run=helper)
    assert r["outcome"] == "harness" and "not trusted for Accessibility" in r["reason"]
    assert [c[0] for c in helper.calls] == ["front"]  # no clipboard, no keys


def test_an_app_that_is_not_in_front_gets_no_keys_and_fails_by_name(helper_file):
    helper = Helper(front=False)
    r = keys_step(App(), name="paste", value=PASTE, helper=helper_file, run=helper)
    assert r["outcome"] == "fail"
    assert r["checks"] == [{"check": "com.hanamorilabs.twinbox was in front", "ok": False,
                            "detail": "frontmost: com.apple.finder"}]
    assert [c[0] for c in helper.calls] == ["front"]


def test_keys_the_helper_could_not_send_fail_by_name(helper_file):
    r = keys_step(App(), name="paste", value=PASTE, helper=helper_file, run=Helper(sent=False))
    assert r["outcome"] == "fail"
    assert r["checks"][-1]["ok"] is False and "sent 0 of 1" in r["checks"][-1]["detail"]


def test_the_expect_checks_judge_what_the_keys_did(helper_file, tmp_path):
    from qajev.ledger import Ledger

    expect = {"state": {"new_box_name": "qa-paste-7f3"}, "no_errors": False}
    ok = keys_step(App(), name="paste", value=PASTE, expect=expect, helper=helper_file, run=Helper(),
                   ledger=Ledger(1.0, 0.0), run_dir=tmp_path, shots=False)
    assert ok["outcome"] == "pass" and len(ok["checks"]) == 4
    bad = keys_step(App(name=""), name="paste", value=PASTE, expect=expect, helper=helper_file, run=Helper(),
                    ledger=Ledger(1.0, 0.0), run_dir=tmp_path, shots=False)
    assert bad["outcome"] == "fail"
    assert [c["ok"] for c in bad["checks"]] == [True, True, True, False]


def test_a_keys_step_in_a_session_runs_as_its_own_step(helper_file, tmp_path, monkeypatch):
    from qajev import mackeys
    from qajev.ledger import Ledger

    helper = Helper()
    monkeypatch.setattr(mackeys, "HELPER", helper_file)
    monkeypatch.setattr(mackeys, "_run", helper)
    steps = [{"name": "paste", "about": "A real Cmd+V pastes the copied name.", "keys": PASTE,
              "expect": {"state": {"new_box_name": "qa-paste-7f3"}, "no_errors": False}}]
    results = native.run_session(App(), steps, ledger=Ledger(1.0, 0.0), run_dir=tmp_path, shots=False)
    assert results[0]["outcome"] == "pass" and [c[0] for c in helper.calls] == ["front", "clip", "keys"]


def test_the_plan_lists_the_keys_checks_before_the_expect_checks():
    from qajev.plan import from_steps

    item = from_steps([{"name": "paste", "about": "x", "keys": PASTE, "expect": {"state": {"new_box_name": "a"}}}])[0]
    names = [c["check"] for c in item["checks"]]
    assert names[:3] == [c["check"] for c in planned_checks(PASTE)]
    assert names[3] == "state new_box_name a"


def test_qajev_play_refuses_a_system_shortcut_when_the_suite_loads(tmp_path, capsys):
    suite = tmp_path / "s.yaml"
    suite.write_text("steps:\n  - name: quit\n    keys: {app: com.hanamorilabs.twinbox, press: [cmd+q]}\n")
    env = tmp_path / ".env"
    env.write_text("")
    assert main(["play", str(tmp_path), "--suite", str(suite), "--json", "--env-file", str(env)]) == cli.EXIT_CONFIG
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "system shortcut" in out["error"]


def test_qajev_plan_refuses_a_wrong_keys_step(tmp_path):
    suite = tmp_path / "s.yaml"
    suite.write_text("steps:\n  - name: paste\n    about: x\n    keys: {app: com.apple.finder, press: [cmd+v]}\n")
    assert main(["plan", str(suite)]) != 0


def test_plan_is_the_same_whatever_the_press_form():
    assert plan({"app": "a.b", "press": "cmd+v"})["codes"] == plan({"app": "a.b", "press": ["cmd+v"]})["codes"]


CLICK = {**PASTE, "click": "new-box.name"}


def test_a_click_lands_on_the_control_at_its_real_screen_point(helper_file):
    helper = Helper()
    r = keys_step(App(window={"x": 100, "y": 200, "zoom": 2.0}), name="paste", value=CLICK, helper=helper_file,
                  run=helper)
    assert helper.calls == [["front", "com.hanamorilabs.twinbox"], ["clip", "qa-paste-7f3"],
                            ["click", "com.hanamorilabs.twinbox", "140", "260"],
                            ["keys", "com.hanamorilabs.twinbox", "9:cmd"]]
    assert ("new-box.name was clicked", True) in [(c["check"], c["ok"]) for c in r["checks"]]
    assert r["outcome"] == "pass"
    assert "after putting 'qa-paste-7f3' on the clipboard and a click on new-box.name" in r["checks"][-1]["says"]


@pytest.mark.parametrize("window, click, says", [
    (None, "new-box.name", "does not say where its window is"),
    ({"x": 0, "y": 0, "zoom": 0}, "new-box.name", "does not say where its window is"),
    ({"x": 0, "y": 0, "zoom": 1}, "new-box.missing", "no control 'new-box.missing'"),
])
def test_a_click_that_cannot_be_placed_fails_by_name_and_presses_no_keys(helper_file, window, click, says):
    helper = Helper()
    r = keys_step(App(window=window), name="paste", value={**PASTE, "click": click}, helper=helper_file, run=helper)
    assert r["outcome"] == "fail"
    assert r["checks"][-1]["check"] == f"{click} was clicked" and says in r["checks"][-1]["detail"]
    assert [c[0] for c in helper.calls] == ["front", "clip"]  # no click, no keys


@pytest.mark.parametrize("old", ["clip", "keys"])
def test_a_helper_that_lacks_a_command_is_harness_not_a_product_failure(helper_file, old):
    knows = tuple(c for c in ("front", "clip", "click", "keys") if c != old)
    r = keys_step(App(), name="paste", value=PASTE, helper=helper_file, run=Helper(knows=knows))
    assert r["outcome"] == "harness"
    assert "could not run this command (an older helper?)" in r["reason"] and "usage: qajev-keys" in r["reason"]
    assert all(c["ok"] for c in r["checks"])  # nothing it did is counted against the app


def test_a_helper_that_answers_front_with_its_usage_is_harness(helper_file):
    r = keys_step(App(), name="paste", value=PASTE, helper=helper_file, run=Helper(knows=()))
    assert r["outcome"] == "harness" and r["checks"] == [] and "nothing was pressed" in r["reason"]


def _app_bundle(tmp_path, bundle):
    app = tmp_path / "Twinbox.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    import plistlib

    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle}))
    return {"command": [str(app / "Contents" / "MacOS" / "twinbox-desktop")]}


def test_keys_go_only_to_the_suites_own_app_when_it_is_known(tmp_path):
    spec = _app_bundle(tmp_path, "com.hanamorilabs.twinbox")
    check_keys([{"name": "paste", "keys": PASTE}], spec)  # the same app: fine
    check_keys([{"name": "paste", "keys": {**PASTE, "app": "COM.hanamorilabs.TWINBOX"}}], spec)
    with pytest.raises(NativeError, match=r"refused: com\.other\.app is not the suite's own app \(com\.hanamorilabs"):
        check_keys([{"name": "paste", "keys": {**PASTE, "app": "com.other.app"}}], spec)
    check_keys([{"name": "paste", "keys": {**PASTE, "app": "com.other.app"}}], {"command": ["/nowhere/X.app/x"]})


def test_qajev_play_refuses_keys_for_another_app_than_the_suites_own(tmp_path, capsys):
    spec = _app_bundle(tmp_path, "com.hanamorilabs.twinbox")
    suite = tmp_path / "s.yaml"
    suite.write_text(yaml.safe_dump({"app": spec, "steps": [
        {"name": "paste", "about": "x", "keys": {"app": "com.other.app", "press": ["cmd+v"]}}]}))
    env = tmp_path / ".env"
    env.write_text("")
    assert main(["play", str(tmp_path), "--suite", str(suite), "--json", "--env-file", str(env)]) == cli.EXIT_CONFIG
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "com.other.app is not the suite's own app (com.hanamorilabs.twinbox)" in out["error"]
