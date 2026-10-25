"""Managed Chrome must disable password services before it can start."""

import json
import os
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from qajev import chrome


@pytest.fixture
def launch(monkeypatch, tmp_path):
    seen = {"calls": [], "before_spawn": None}
    monkeypatch.setattr(chrome, "STATE", tmp_path / "state")
    monkeypatch.setattr(chrome, "PROFILES", tmp_path / "profiles")
    monkeypatch.setattr(chrome, "EPHEMERAL_ROOT", tmp_path / "ephemeral")
    monkeypatch.setattr(chrome, "binary", lambda: "/fixture/chrome")
    monkeypatch.setattr(chrome, "free_port", lambda: 9398)
    monkeypatch.setattr(chrome, "listening", lambda _: False)
    monkeypatch.setattr(chrome, "version", lambda *_args, **_kwargs: {"Browser": "Fixture Chrome"})
    monkeypatch.setattr(chrome, "identity", lambda _pid: {"proc_start": "fixture", "command": "fixture"})

    def spawn(args, **_kwargs):
        seen["calls"].append(args)
        profile = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--user-data-dir=")))
        if seen["before_spawn"]:
            seen["before_spawn"](profile, args)
        return SimpleNamespace(pid=99999999, poll=lambda: None)

    monkeypatch.setattr(chrome.subprocess, "Popen", spawn)
    previous = os.umask(0o022)
    try:
        yield seen
    finally:
        os.umask(previous)


@pytest.mark.parametrize("ephemeral", [True, False])
def test_password_services_are_disabled_in_private_preferences_before_spawn(launch, ephemeral):
    def before_spawn(profile, args):
        path = profile / "Default" / "Preferences"
        assert path.is_file(), "password-service preferences must exist before Chrome starts"
        preferences = json.loads(path.read_text())
        assert preferences["credentials_enable_service"] is False
        assert preferences["credentials_enable_autosignin"] is False
        assert preferences["profile"]["password_manager_leak_detection"] is False
        assert path.stat().st_mode & 0o777 == 0o600
        assert "--password-store=basic" in args

    launch["before_spawn"] = before_spawn
    record = chrome.start("privacy", headless=True, ephemeral=ephemeral)
    assert record["password_services_disabled"] is True
    assert len(launch["calls"]) == 1


@pytest.mark.parametrize("required", [
    "--disable-background-networking", "--disable-component-update", "--disable-sync", "--disable-default-apps",
    "--disable-domain-reliability", "--disable-client-side-phishing-detection", "--no-pings",
    "--metrics-recording-only", "--safebrowsing-disable-auto-update",
    "--disable-extensions", "--disable-component-extensions-with-background-pages",
])
def test_background_network_switches_reach_the_managed_browser(launch, monkeypatch, required):
    monkeypatch.delenv("QAJEV_CHROME_FLAGS", raising=False)
    chrome.start("network", headless=True)
    assert required in launch["calls"][0]


@pytest.mark.parametrize("endpoint", ["gcm-checkin-url", "gcm-registration-url", "gcm-mcs-endpoint"])
def test_cloud_messaging_endpoints_are_nonnetwork_urls_before_spawn(launch, monkeypatch, endpoint):
    monkeypatch.delenv("QAJEV_CHROME_FLAGS", raising=False)
    chrome.start("network", headless=True)
    url = "https://127.0.0.1:0" if endpoint == "gcm-mcs-endpoint" else "chrome://qajev-disabled"
    assert f"--{endpoint}={url}" in launch["calls"][0]


@pytest.mark.parametrize("endpoint", ["gcm-checkin-url", "gcm-registration-url", "gcm-mcs-endpoint"])
@pytest.mark.parametrize("value", ["", "=https://example.test/service"])
@pytest.mark.parametrize("prefix", ["--", "-", " --", " -"])
def test_extra_flags_cannot_restore_cloud_messaging_endpoints(launch, monkeypatch, endpoint, value, prefix):
    monkeypatch.setenv("QAJEV_CHROME_FLAGS", shlex.quote(f"{prefix}{endpoint}{value}"))
    with pytest.raises(chrome.ChromeError, match="messaging endpoint"):
        chrome.start("network", headless=True)
    assert launch["calls"] == []


@pytest.mark.parametrize("required", [
    "Translate", "MediaRouter", "OptimizationHints", "AutofillServerCommunication", "PasswordLeakDetection",
    "OptimizationGuideModelDownloading", "NetworkTimeServiceQuerying", "AimServerEligibilityEnabled",
    "AimServerRequestOnStartupEnabled", "OptimizationGuideManifestBroker", "OptimizationGuideOnDeviceModel",
])
@pytest.mark.parametrize("prefix", ["--", "-", " --", " -"])
def test_extra_disabled_features_keep_the_product_network_policy(launch, monkeypatch, required, prefix):
    flags = shlex.quote(f"{prefix}disable-features=FixtureUiFeature,Translate") + " --no-pings"
    monkeypatch.setenv("QAJEV_CHROME_FLAGS", flags)
    chrome.start("network", headless=True)
    flags = launch["calls"][0]
    combined = [arg.split("=", 1)[1] for arg in flags if arg.startswith("--disable-features=")]
    assert len(combined) == 1, "Chromium uses the last repeated switch"
    features = combined[0].split(",")
    assert required in features
    assert "FixtureUiFeature" in features
    assert features.count("Translate") == 1
    assert "--no-pings" in flags
    assert not any(arg.startswith("-disable-features") for arg in flags)


@pytest.mark.parametrize("extra", ["--disable-features", "--disable-features FixtureUiFeature",
                                  "-disable-features", "-disable-features FixtureUiFeature"])
def test_bare_disabled_feature_switch_refuses_before_spawn(launch, monkeypatch, extra):
    monkeypatch.setenv("QAJEV_CHROME_FLAGS", extra)
    with pytest.raises(chrome.ChromeError, match="--disable-features="):
        chrome.start("network", headless=True)
    assert launch["calls"] == []


@pytest.mark.parametrize("ephemeral", [False, True])
def test_dictionary_downloads_are_disabled_before_spawn(launch, ephemeral):
    def before_spawn(profile, _args):
        preferences = json.loads((profile / "Default/Preferences").read_text())
        assert preferences.get("browser", {}).get("enable_spellchecking") is False
        assert preferences.get("spellcheck", {}).get("dictionary") == ""
        assert preferences.get("spellcheck", {}).get("dictionaries") == []
        assert preferences.get("spellcheck", {}).get("use_spelling_service") is False

    launch["before_spawn"] = before_spawn
    record = chrome.start("network", headless=True, ephemeral=ephemeral)
    assert record.get("background_services_disabled") is True


@pytest.mark.parametrize("ephemeral", [False, True])
def test_speculative_preconnections_are_disabled_before_spawn(launch, ephemeral):
    def before_spawn(profile, _args):
        preferences = json.loads((profile / "Default/Preferences").read_text())
        # Chromium's persisted enum: kDisabled = 2, kStandard = 0, kExtended = 3.
        assert preferences.get("net", {}).get("network_prediction_options") == 2

    launch["before_spawn"] = before_spawn
    chrome.start("network", headless=True, ephemeral=ephemeral)


def test_existing_preconnection_setting_is_replaced_without_losing_other_network_preferences(launch):
    path = chrome.PROFILES / "network" / "Default/Preferences"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"net": {"network_prediction_options": 3, "other": "keep"}}))
    chrome.start("network", headless=True)
    assert json.loads(path.read_text())["net"] == {"network_prediction_options": 2, "other": "keep"}


def test_existing_dictionary_download_preferences_are_replaced_and_unrelated_settings_survive(launch):
    path = chrome.PROFILES / "network" / "Default/Preferences"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"browser": {"enable_spellchecking": True, "show_bookmarks": True},
                                "spellcheck": {"dictionary": "en-US", "dictionaries": ["en-US"],
                                               "use_spelling_service": True, "other": "keep"}}))
    chrome.start("network", headless=True)
    preferences = json.loads(path.read_text())
    assert preferences["browser"] == {"enable_spellchecking": False, "show_bookmarks": True}
    assert preferences["spellcheck"] == {"dictionary": "", "dictionaries": [],
                                        "use_spelling_service": False, "other": "keep"}


@pytest.mark.parametrize("key", ["browser", "spellcheck", "net"])
@pytest.mark.parametrize("value", [None, [], "invalid"])
def test_invalid_network_preferences_refuse_without_spawn_or_overwrite(launch, key, value):
    path = chrome.PROFILES / "network" / "Default/Preferences"
    path.parent.mkdir(parents=True)
    original = json.dumps({key: value})
    path.write_text(original)
    with pytest.raises(Exception) as error:
        chrome.start("network", headless=True)
    assert isinstance(error.value, chrome.ChromeError)
    assert path.read_text() == original
    assert launch["calls"] == []


@pytest.mark.parametrize("marker", [None, False, 1, "true"])
def test_running_browser_without_background_policy_is_not_reused(launch, monkeypatch, marker):
    monkeypatch.setattr(chrome, "status", lambda _profile: [{
        "alive": True, "headless": True, "password_services_disabled": True,
        "background_services_disabled": marker,
    }])
    with pytest.raises(chrome.ChromeError, match="Restart.*background"):
        chrome.start("network", headless=True)
    assert launch["calls"] == []


def test_unrelated_existing_preferences_survive_the_privacy_update(launch):
    path = chrome.PROFILES / "existing" / "Default" / "Preferences"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"credentials_enable_service": True,
                                "profile": {"password_manager_leak_detection": True, "name": "Fixture profile"},
                                "other": {"theme": "dark", "labels": ["保持", chr(0xD800)]}}))
    chrome.start("existing", headless=True)
    assert json.loads(path.read_text()) == {
        "credentials_enable_service": False,
        "credentials_enable_autosignin": False,
        "profile": {"password_manager_leak_detection": False, "name": "Fixture profile"},
        "browser": {"enable_spellchecking": False},
        "spellcheck": {"dictionary": "", "dictionaries": [], "use_spelling_service": False},
        "net": {"network_prediction_options": 2},
        "other": {"theme": "dark", "labels": ["保持", chr(0xD800)]},
    }


@pytest.mark.parametrize("raw", ['{"private-value":', '[]', '{"profile": null}'])
def test_invalid_preferences_refuse_launch_without_overwriting_them(launch, raw):
    path = chrome.PROFILES / "invalid" / "Default" / "Preferences"
    path.parent.mkdir(parents=True)
    path.write_text(raw)
    with pytest.raises(Exception) as error:
        chrome.start("invalid", headless=True)
    assert isinstance(error.value, chrome.ChromeError)
    assert "private-value" not in str(error.value)
    assert path.read_text() == raw
    assert launch["calls"] == []


@pytest.mark.parametrize("linked", ["profile", "default", "preferences"])
def test_linked_preferences_are_refused_before_any_outside_file_is_changed(launch, tmp_path, linked):
    outside = tmp_path / "outside"
    (outside / "Default").mkdir(parents=True)
    protected = outside / "Default" / "Preferences"
    protected.write_text('{"keep": "outside profile"}')
    profile = chrome.PROFILES / "linked"
    profile.parent.mkdir(parents=True)
    if linked == "profile":
        profile.symlink_to(outside, target_is_directory=True)
    else:
        profile.mkdir()
        if linked == "default":
            (profile / "Default").symlink_to(outside / "Default", target_is_directory=True)
        else:
            (profile / "Default").mkdir()
            (profile / "Default" / "Preferences").symlink_to(protected)
    with pytest.raises(chrome.ChromeError):
        chrome.start("linked", headless=True)
    assert protected.read_text() == '{"keep": "outside profile"}'
    assert launch["calls"] == []


def test_failed_atomic_replace_preserves_preferences_and_removes_private_tempfile(launch, monkeypatch):
    path = chrome.PROFILES / "atomic" / "Default" / "Preferences"
    path.parent.mkdir(parents=True)
    original = '{"keep": "original profile"}'
    path.write_text(original)

    def refuse(*_args):
        raise PermissionError("fixture: replacement refused")

    monkeypatch.setattr(chrome.os, "replace", refuse)
    with pytest.raises(Exception) as error:
        chrome.start("atomic", headless=True)
    assert isinstance(error.value, chrome.ChromeError)
    assert path.read_text() == original
    assert list(path.parent.iterdir()) == [path]
    assert launch["calls"] == []


@pytest.mark.parametrize("marker", [None, False])
def test_running_legacy_profile_is_refused_without_restart_or_preference_writes(launch, monkeypatch, marker):
    record = {"alive": True, "headless": True, "password_services_disabled": marker}
    monkeypatch.setattr(chrome, "status", lambda _profile: [record])
    with pytest.raises(chrome.ChromeError, match="Restart"):
        chrome.start("legacy", headless=True)
    assert launch["calls"] == []
    assert not chrome.PROFILES.exists()


def test_running_private_profile_is_reused_without_rewriting_active_preferences(launch, monkeypatch):
    record = {"alive": True, "headless": True, "password_services_disabled": True,
              "background_services_disabled": True}
    monkeypatch.setattr(chrome, "status", lambda _profile: [record])
    assert chrome.start("private", headless=True) is record
    assert launch["calls"] == []
    assert not chrome.PROFILES.exists()


def test_preference_read_errors_are_safe_and_prevent_launch(launch, monkeypatch):
    path = chrome.PROFILES / "unreadable" / "Default" / "Preferences"
    path.parent.mkdir(parents=True)
    path.write_text('{"keep": "original"}')
    original_read = Path.read_text

    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError("fixture private details")
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(Exception) as error:
        chrome.start("unreadable", headless=True)
    assert isinstance(error.value, chrome.ChromeError)
    assert "fixture private details" not in str(error.value)
    assert launch["calls"] == []


def test_temp_cleanup_error_keeps_the_safe_launch_error_and_private_file(launch, monkeypatch):
    original_unlink = Path.unlink

    def denied_replace(*_args):
        raise PermissionError("fixture private replacement details")

    def denied_cleanup(self, *args, **kwargs):
        if self.name.startswith(".qajev-preferences-"):
            raise PermissionError("fixture private cleanup details")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(chrome.os, "replace", denied_replace)
    monkeypatch.setattr(Path, "unlink", denied_cleanup)
    with pytest.raises(Exception) as error:
        chrome.start("cleanup", headless=True)
    assert isinstance(error.value, chrome.ChromeError)
    assert "fixture private" not in str(error.value)
    leftovers = list((chrome.PROFILES / "cleanup/Default").glob(".qajev-preferences-*"))
    assert len(leftovers) == 1 and leftovers[0].stat().st_mode & 0o777 == 0o600
    assert launch["calls"] == []
