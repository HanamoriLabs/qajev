"""QAJev dispatch metadata is cleaned while declared inputs and opaque values stay exact."""
import json
import unittest
import urllib.parse
from unittest import mock

from qajev import cloud

PERSON = "dispatch-fixture"
SOURCE = "/Users/dispatch-fixture/work/app"
REASON = (
    "the tunnel was refused: the project folder /Users/dispatch-fixture/work/app "
    "is not inside /home/dispatch-fixture/Development; "
    r"source C:\Users\dispatch-fixture\qa.yaml did not match its declaration"
)


class Reply:
    def __init__(self):
        self.status = 200
        self.headers = {"Content-Type": "application/json"}

    def read(self):
        return b'{"ok":true}'

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class DispatchMetadataPrivacyTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.client = cloud.Client("http://127.0.0.1:9", "synthetic-dispatch-key")
        patch = mock.patch("urllib.request.urlopen", side_effect=self.capture)
        patch.start()
        self.addCleanup(patch.stop)

    def capture(self, request, timeout=None):
        self.calls.append((request.get_method(), urllib.parse.urlsplit(request.full_url).path,
                           json.loads(request.data)))
        return Reply()

    def assert_clean_reason(self, body):
        text = json.dumps(body)
        self.assertNotIn(PERSON, text)
        for words in ("the tunnel was refused", "the project folder", "is not inside",
                      "source", "did not match its declaration"):
            self.assertIn(words, text)
        self.assertEqual(body["error"].count("local/"), 3)

    def test_machine_status_hides_restored_paths_without_losing_the_refusal_reason(self):
        original = {"state": "stopped", "error": REASON}
        self.client.post("/api/v1/dispatch/r_fixture/machine", original)
        self.assert_clean_reason(self.calls[-1][2])
        self.assertEqual(self.calls[-1][2]["state"], "stopped")
        self.assertEqual(original["error"], REASON)

    def test_host_heartbeat_and_claim_metadata_use_the_same_cleaner(self):
        for path in ("/api/v1/dispatch/hosts", "/api/v1/dispatch/claim"):
            with self.subTest(path=path):
                original = {"hosts": [{"source": SOURCE}], "error": REASON}
                self.client.post(path, original)
                self.assert_clean_reason(self.calls[-1][2])
                self.assertIn(PERSON, json.dumps(original))

    def test_run_creation_expectation_args_remain_exact(self):
        args = ["check", "https://example.test", "--expect-url", "/dashboard", "--expect-js",
                "location.pathname === '/done'", "--expect-text", "Saved to /tmp/out", SOURCE]
        self.client.post("/api/v1/runs", {"kind": "check", "name": "checks", "args": args})
        self.assertEqual(self.calls[-1][2]["args"], args)

    def test_unknown_dispatch_request_is_not_reclassified_as_owned_metadata(self):
        original = {"source": SOURCE, "args": ["--expect-url", "/dashboard", "/[a-z]+/g"]}
        self.client.post("/api/v1/dispatch/input", original)
        self.assertEqual(self.calls[-1][2], original)

    def test_device_codes_keep_their_exact_opaque_values(self):
        original = {"device_code": "/opaque/code/value"}
        self.client.post("/api/v1/device/token", original)
        self.assertEqual(self.calls[-1][2], original)
