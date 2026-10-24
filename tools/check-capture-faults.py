"""Restored assertion-based faults for Linux viewport-capture preparation."""

import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "capture_fault_helpers", Path(__file__).with_name("check-evidence-faults.py"))
assert spec is not None and spec.loader is not None
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

SOURCE = "qajev/session.py"
UNIT = "tests/test_capture_latency.py::"
CONDITION = 'if method == "Page.captureScreenshot" and sys.platform == "linux":'
ACTIVATE = '            self.browser.call("Page.bringToFront")\n'
FORWARD = '        return self.browser.call(method, **params)\n'

base.FAULTS = [
    ("C01-no-preparation", SOURCE, ACTIVATE, "            pass\n",
     UNIT + "test_linux_capture_activates_its_own_target_before_sending_the_unchanged_request"),
    ("C02-headless-only", SOURCE, CONDITION, CONDITION[:-1] + " and self.headless:",
     UNIT + "test_linux_capture_activates_its_own_target_before_sending_the_unchanged_request"),
    ("C03-other-platform-focus", SOURCE, CONDITION, 'if method == "Page.captureScreenshot":',
     UNIT + "test_non_linux_capture_keeps_the_existing_window_focus_policy"),
    ("C04-every-command-focus", SOURCE, CONDITION, 'if sys.platform == "linux":',
     UNIT + "test_other_page_commands_do_not_activate_a_window"),
    ("C05-preparation-after-capture", SOURCE,
     '        ' + CONDITION + "\n" + ACTIVATE + FORWARD,
     '        result = self.browser.call(method, **params)\n        ' + CONDITION + "\n" + ACTIVATE +
     "        return result\n",
     UNIT + "test_linux_capture_activates_its_own_target_before_sending_the_unchanged_request"),
    ("C06-default-daemon-target", SOURCE, ACTIVATE, '            self.jev.cdp("Page.bringToFront")\n',
     UNIT + "test_linux_capture_activates_its_own_target_before_sending_the_unchanged_request"),
    ("C07-ignored-preparation-error", SOURCE, ACTIVATE,
     '            try:\n                self.browser.call("Page.bringToFront")\n'
     "            except RuntimeError:\n                pass\n",
     UNIT + "test_failed_activation_refuses_capture_instead_of_waiting_on_a_hidden_surface"),
    ("C08-dropped-capture-arguments", SOURCE, FORWARD, '        return self.browser.call(method)\n',
     UNIT + "test_linux_capture_activates_its_own_target_before_sending_the_unchanged_request"),
    ("C09-only-first-capture", SOURCE, CONDITION + "\n" + ACTIVATE,
     CONDITION[:-1] + ' and not getattr(self, "_capture_prepared", False):\n' + ACTIVATE +
     '            self._capture_prepared = True\n',
     UNIT + "test_saved_and_data_url_screenshots_use_the_same_prepared_capture_path"),
    ("C10-hidden-preparation-time", SOURCE, 'round(time.monotonic() - start - at_s, 3)', "0.0",
     UNIT + "test_react_capture_keeps_each_frame_and_measures_preparation_and_readback"),
]

if __name__ == "__main__":
    sys.argv += ["--endpoint", "http://127.0.0.1:9"]
    raise SystemExit(base.main())
