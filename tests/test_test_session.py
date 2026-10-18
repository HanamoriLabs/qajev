"""Offline contract: no browser, Keychain or HTTP endpoint is contacted."""

import json
import socket
import unittest
from unittest import mock

from qajev import test_session as ts

POLICY = {
    "app": "qajev-dashboard",
    "origin": "https://cloud.qajev.com",
    "uid": "qajev-dashboard",
    "login_path": "/test-signin",
    "fragment": "qajev_test_code",
    "clean_path": "/",
}


class TestSessionContract(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000.0
        self.policy = ts.Policy.parse(POLICY)

    def reply(self):
        return {
            "login_url": self.policy.origin + "/test-signin#qajev_test_code=" + "c" * 64,
            "test_account": True,
            "uid": self.policy.uid,
            "publishable_key": "pk_test_fixture",
            "expires_at": self.now + 60,
        }

    def test_policy_rejects_user_selectors_redirects_and_wrong_origins(self):
        for change in (
            {"origin": "http://cloud.qajev.com"},
            {"origin": "https://user@cloud.qajev.com"},
            {"origin": "https://cloud.qajev.com/"},
            {"user": "owner"},
            {"login_path": "//elsewhere"},
            {"clean_path": "/signin?next=https://elsewhere"},
        ):
            with self.subTest(change=change), self.assertRaises(ts.SessionError):
                ts.Policy.parse({**POLICY, **change})

    def test_mint_reply_rejects_wrong_identity_live_keys_expiry_and_redirect_urls(self):
        for change in (
            {"uid": "owner"},
            {"test_account": False},
            {"publishable_key": "pk_live_fault"},
            {"publishable_key": ""},
            {"expires_at": self.now},
            {"expires_at": self.now + 61},
            {"login_url": "https://elsewhere/test-signin#qajev_test_code=" + "c" * 64},
            {"login_url": self.policy.origin + "/test-signin?next=/settings#qajev_test_code=" + "c" * 64},
        ):
            with self.subTest(change=change), self.assertRaises(ts.SessionError):
                self.policy.validate_link({**self.reply(), **change}, self.now)

    def test_encrypted_delivery_is_bound_to_run_machine_app_and_ready_nonce(self):
        secret = bytes(range(32))
        binding = ts.binding("r_fixture", "machine-a", self.policy.app, "n" * 32)
        sealed = ts.seal(secret, binding, self.reply())
        self.assertNotIn("qajev_test_code", json.dumps(sealed))
        self.assertEqual(ts.unseal(secret, binding, sealed), self.reply())
        for b in (
            ts.binding("r_other", "machine-a", self.policy.app, "n" * 32),
            ts.binding("r_fixture", "machine-b", self.policy.app, "n" * 32),
            ts.binding("r_fixture", "machine-a", "other-app", "n" * 32),
            ts.binding("r_fixture", "machine-a", self.policy.app, "o" * 32),
        ):
            with self.assertRaises(ts.SessionError):
                ts.unseal(secret, b, sealed)

    def test_same_browser_identity_and_deadline_are_required_before_actions(self):
        valid = {
            "test_account": True,
            "uid": self.policy.uid,
            "publishable_key": "pk_test_fixture",
            "expires_at": self.now + 900,
        }
        self.assertEqual(self.policy.verify(valid, self.now), self.now + 900)
        for change in (
            {"uid": "owner"},
            {"test_account": False},
            {"publishable_key": "pk_live_fault"},
            {"expires_at": self.now},
            {"expires_at": self.now + 901},
        ):
            with self.subTest(change=change), self.assertRaises(ts.SessionError):
                self.policy.verify({**valid, **change}, self.now)
        lease = ts.Lease(self.policy, self.now + 900, lambda: self.now)
        lease.check()
        self.now += 900
        with self.assertRaisesRegex(ts.SessionError, "expired"):
            lease.check()

    def test_private_wire_has_bounded_frames_and_hides_transport_exceptions(self):
        a, b = socket.socketpair()
        try:
            ts.send(a, {"kind": "ready"})
            self.assertEqual(ts.receive(b), {"kind": "ready"})
            a.sendall((9000).to_bytes(4, "big"))
            with self.assertRaises(ts.SessionError):
                ts.receive(b)
        finally:
            a.close()
            b.close()

    def test_bootstrap_uses_no_daemon_or_recording_and_disposes_context_on_failure(self):
        events = []

        class FakeBrowser:
            def ready(self):
                events.append("browser-ready")

            def navigate(self, url):
                events.append("navigate")

            def identity(self, policy):
                events.append("browser-identity")
                return {"test_account": True, "uid": "owner", "publishable_key": "pk_test_fixture", "expires_at": 0}

            def close(self):
                events.append("context-disposed")

        def deliver(policy):
            events.append("mint-after-ready")
            return self.reply()

        with self.assertRaises(ts.SessionError):
            ts.bootstrap(self.policy, FakeBrowser(), deliver, now=lambda: self.now)
        self.assertEqual(
            events, ["browser-ready", "mint-after-ready", "navigate", "browser-identity", "context-disposed"]
        )

    def test_bootstrap_accepts_only_clean_verified_page_and_never_replays(self):
        events = []

        class FakeBrowser:
            def ready(self):
                events.append("ready")

            def navigate(self, url):
                events.append("navigate")

            def identity(inner, policy):
                return {
                    "test_account": True,
                    "uid": policy.uid,
                    "publishable_key": "pk_test_fixture",
                    "expires_at": self.now + 900,
                }

            def close(self):
                events.append("closed")

        lease = ts.bootstrap(self.policy, FakeBrowser(), lambda p: self.reply(), now=lambda: self.now)
        self.assertEqual(lease.expires_at, self.now + 900)
        self.assertEqual(events, ["ready", "navigate"])

    def test_missing_provider_and_wrong_origin_never_start_a_browser(self):
        with mock.patch.object(ts, "PrivateBrowser") as browser:
            with self.assertRaises(ts.SessionError):
                ts.prepare("qajev-dashboard", "unused")
            with ts.provider(lambda app: (self.policy, lambda p: self.reply())):
                with self.assertRaises(ts.SessionError):
                    ts.prepare("qajev-dashboard", "unused", urls=["https://other.invalid/"])
            browser.assert_not_called()

    def test_bootstrap_driver_error_never_exposes_the_code_or_secret(self):
        browser = mock.Mock()
        browser.navigate.side_effect = ValueError("CANARY_AUTH https://example.invalid/#CANARY_CODE")
        with self.assertRaises(ts.SessionError) as result:
            ts.bootstrap(self.policy, browser, lambda p: self.reply(), now=lambda: self.now)
        self.assertNotIn("CANARY", str(result.exception))
        browser.close.assert_called_once()

    def test_seeded_run_refuses_profiles_parallel_jobs_models_and_attached_browsers_before_startup(self):
        from qajev import runner, suite

        base = {
            "name": "seeded",
            "base_url": self.policy.origin,
            "scenarios": [
                {"name": "overview", "about": "Read the seeded overview.", "url": "/", "expect": {"text": ["Overview"]}}
            ],
        }
        cases = [(base, {"profile": "manual-profile"}), (base, {"jobs": 2}), (base, {"cdp_url": "http://127.0.0.1:1"})]
        for extra in (
            {"goal": "Find the overview. Stop when it opens."},
            {"vision": True},
            {"expect": {"looks": ["The page is readable."]}},
        ):
            cases.append(({**base, "scenarios": [{**base["scenarios"][0], **extra}]}, {}))
        with mock.patch.object(runner.chrome, "start") as start, ts.provider(lambda app: None):
            for data, options in cases:
                with (
                    self.subTest(options=options, data=data),
                    self.assertRaisesRegex(runner.ConfigError, "test sessions require one fresh cloud browser"),
                ):
                    runner.run(suite.parse(data), runner.Options(test_session="qajev-dashboard", **options))
            start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
