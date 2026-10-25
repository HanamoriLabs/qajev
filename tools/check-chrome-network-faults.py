"""Restored, assertion-based faults for managed Chrome's background-service policy."""

import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "network_fault_helpers", Path(__file__).with_name("check-evidence-faults.py"))
assert spec is not None and spec.loader is not None
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

C = "qajev/chrome.py"
U = "tests/test_chrome_privacy.py::"
A = "tools/chrome-netlog-audit.py"
T = "tests/test_chrome_network_audit.py::"

switches = ("--disable-background-networking", "--disable-component-update", "--disable-sync", "--disable-default-apps",
            "--disable-domain-reliability", "--disable-client-side-phishing-detection", "--no-pings",
            "--metrics-recording-only", "--safebrowsing-disable-auto-update", "--disable-extensions",
            "--disable-component-extensions-with-background-pages")
features = ("AutofillServerCommunication", "PasswordLeakDetection", "OptimizationGuideModelDownloading",
            "NetworkTimeServiceQuerying", "AimServerEligibilityEnabled", "AimServerRequestOnStartupEnabled",
            "OptimizationGuideManifestBroker", "OptimizationGuideOnDeviceModel")
base.FAULTS = [
    (f"S{index:02d}", C, f'    "{switch}",\n', "",
     U + f"test_background_network_switches_reach_the_managed_browser[{switch}]")
    for index, switch in enumerate(switches, 1)
] + [
    (f"F{index:02d}", C, feature, "",
     U + f"test_extra_disabled_features_keep_the_product_network_policy[{feature}]")
    for index, feature in enumerate(features, 1)
] + [
    ("M01-last-feature-switch", C, "    return result\n", "    return combined\n",
     U + "test_extra_disabled_features_keep_the_product_network_policy"),
    ("M02-duplicates", C, "dict.fromkeys(disabled)", "disabled",
     U + "test_extra_disabled_features_keep_the_product_network_policy"),
    ("M03-bare-switch", C, 'if value == "--disable-features":', "if False:",
     U + "test_bare_disabled_feature_switch_refuses_before_spawn"),
    ("P01-spellcheck-enabled", C, 'data.setdefault("browser", {})["enable_spellchecking"] = False', "pass",
     U + "test_dictionary_downloads_are_disabled_before_spawn"),
    ("P02-legacy-dictionary", C, 'dictionary=""', 'dictionary="en-US"',
     U + "test_dictionary_downloads_are_disabled_before_spawn"),
    ("P03-dictionary-list", C, "dictionaries=[]", 'dictionaries=["en-US"]',
     U + "test_dictionary_downloads_are_disabled_before_spawn"),
    ("P04-online-spelling", C, "use_spelling_service=False", "use_spelling_service=True",
     U + "test_dictionary_downloads_are_disabled_before_spawn"),
    ("P05-preference-validation", C, 'for key in ("profile", "browser", "spellcheck", "net")',
     'for key in ("profile",)',
     U + "test_invalid_network_preferences_refuse_without_spawn_or_overwrite"),
    ("P06-policy-before-spawn", C, "    _disable_password_services(profile_dir)\n", "    pass\n",
     U + "test_dictionary_downloads_are_disabled_before_spawn"),
    ("P07-speculative-preconnections", C, 'data.setdefault("net", {})["network_prediction_options"] = 2',
     'data.setdefault("net", {})["network_prediction_options"] = 0',
     U + "test_speculative_preconnections_are_disabled_before_spawn"),
    ("R01-reuse-old-process", C, 'if running[0].get("background_services_disabled") is not True:', "if False:",
     U + "test_running_browser_without_background_policy_is_not_reused"),
    ("R02-missing-marker", C, '        "background_services_disabled": True,\n', "",
     U + "test_dictionary_downloads_are_disabled_before_spawn"),
    ("R03-truthy-marker", C, 'if running[0].get("background_services_disabled") is not True:',
     'if running[0].get("background_services_disabled") != True:',
     U + "test_running_browser_without_background_policy_is_not_reused"),
    ("A01-resolver-start", A, '"HOST_RESOLVER_MANAGER_REQUEST"', '"IGNORED_RESOLVER_REQUEST"',
     T + "test_each_request_start_kind_and_host_field_rejects_a_nonlocal_host"),
    ("A02-host-field", A, '"group_name", "host"', '"group_name"',
     T + "test_each_request_start_kind_and_host_field_rejects_a_nonlocal_host"),
    ("A03-marker-exemption", A, "or host in LOCAL:", 'or host in LOCAL or host.startswith("~"):',
     T + "test_a_resolver_marker_is_not_exempted_and_headers_are_not_request_hosts"),
    ("A04-count", A, "bad.get(host, 0) + 1", "1",
     T + "test_repeated_nonlocal_starts_keep_their_count"),
    ("A05-headers", A, '"group_name", "host"', '"group_name", "host", "headers"',
     T + "test_a_resolver_marker_is_not_exempted_and_headers_are_not_request_hosts"),
    ("G04-extra-endpoint-override", C, 'if any(value.split("=", 1)[0] in {"--gcm-checkin-url",',
     'if False and any(value.split("=", 1)[0] in {"--gcm-checkin-url",',
     U + "test_extra_flags_cannot_restore_cloud_messaging_endpoints"),
    ("M04-extra-flag-order", C,
     'result.insert(feature_position, "--disable-features=" + ",".join(dict.fromkeys(disabled)))',
     'result.append("--disable-features=" + ",".join(dict.fromkeys(disabled)))',
     "tests/test_linux.py::test_extra_chrome_flags_come_from_the_environment"),
] + [
    (f"G{index:02d}-network-endpoint", C, f'    "--{endpoint}={url}",\n', "",
     U + f"test_cloud_messaging_endpoints_are_nonnetwork_urls_before_spawn[{endpoint}]")
    for index, (endpoint, url) in enumerate((("gcm-checkin-url", "chrome://qajev-disabled"),
        ("gcm-registration-url", "chrome://qajev-disabled"), ("gcm-mcs-endpoint", "https://127.0.0.1:0")), 1)
]

if __name__ == "__main__":
    sys.argv += ["--endpoint", "http://127.0.0.1:9"]
    raise SystemExit(base.main())
