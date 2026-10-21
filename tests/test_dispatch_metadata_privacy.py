"""QAJev dispatch metadata is cleaned while declared inputs and opaque values stay exact."""
import json
import unittest
import urllib.parse
from unittest import mock

from qajev import cloud
from qajev_upload_privacy import public_reference, sanitize_upload

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
                           json.loads(bytes(request.data))))
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

    def test_nested_quoted_spaced_path_has_no_tail_in_artifacts_or_machine_reports(self):
        for path in ("/Users/privacy-fixture/Work Files/qa.yaml",
                     r"C:\Users\privacy-fixture\Work Files\qa.yaml",
                     "file:///Users/privacy-fixture/Work Files/qa.yaml"):
            for outer, inner in (("`", '"'), ("`", "'"), ('"', "'"),
                                 ("'", '"'), ('"', "`"), ("'", "`")):
                with self.subTest(path=path, outer=outer, inner=inner):
                    message = f"source {outer}{inner}{path}{inner}{outer} is not a git root"
                    expected = (
                        f"source {outer}{inner}{public_reference(path)}{inner}{outer} is not a git root"
                    )
                    paths = {}
                    artifact = sanitize_upload(message.encode(), paths=paths).decode()
                    self.assertEqual(artifact, expected)
                    self.assertEqual(set(paths.values()), set() if path.startswith("file://") else {path})
                    self.assertEqual(sanitize_upload(artifact.encode()).decode(), expected)
                    self.client.post("/api/v1/dispatch/r_fixture/machine", {"error": message})
                    self.assertEqual(self.calls[-1][2]["error"], expected)
                    self.assertNotIn("privacy-fixture", json.dumps(self.calls[-1][2]))
                    self.assertNotIn("Work Files", json.dumps(self.calls[-1][2]))

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

    def test_run_name_project_and_tunnel_are_cleaned_while_declared_inputs_stay_exact(self):
        inputs = {
            "args": ["--expect-text", "Saved to /tmp/out", "--expect-js",
                     "location.pathname === '/done'", SOURCE],
            "files": [{"path": "in/suite.yaml", "sha256": "a" * 64, "size": 17}],
            "serve": {"path": "site", "port": 8877},
            "seed_files": [{"file": SOURCE + "/fixture.json"}],
        }
        metadata = {
            "name": f"Checks for {SOURCE}/qa.yaml could not start",
            "project": {"name": SOURCE + "/project", "repo": SOURCE, "note": REASON},
            "tunnel": {"root": SOURCE, "source": {"file": SOURCE + "/project.toml"},
                       "ports": {"8877": {"cwd": SOURCE, "command": [SOURCE + "/bin/serve"]}}},
        }
        body = {**inputs, **metadata}
        original = json.loads(json.dumps(body))
        for buffer in (None, bytes, bytearray, memoryview):
            with self.subTest(buffer=buffer):
                if buffer is None:
                    self.client.post("/api/v1/runs", body)
                else:
                    self.client._call("POST", "/api/v1/runs", raw=buffer(json.dumps(body).encode()),
                                      headers={"Content-Type": "application/json"})
                sent = self.calls[-1][2]
                for field in ("name", "project", "tunnel"):
                    with self.subTest(field=field):
                        self.assertNotIn(PERSON, json.dumps(sent[field]))
                        self.assertNotEqual(sent[field], metadata[field])
                self.assertIn("Checks for", sent["name"])
                self.assertIn("could not start", sent["name"])
                self.assertIn("did not match its declaration", sent["project"]["note"])
                for field, value in inputs.items():
                    self.assertEqual(sent[field], value)
                self.assertEqual(body, original, "Cleaning must leave the caller's metadata and inputs intact")

    def test_new_metadata_routes_are_cleaned_by_default(self):
        original = {"source": SOURCE, "args": ["--expect-url", "/dashboard", "/[a-z]+/g"]}
        for route in ("/api/v1/dispatch/future-status", "/api/v1/future/status",
                      "/api/v1/dispatch/r_fixture/log", "/api/v1/dispatch/r_fixture/progress",
                      "/api/v1/jobs/r_fixture/progress", "/api/v1/jobs/r_fixture/log",
                      "/api/v1/hosts/h_fixture/heartbeat", "/api/v1/runs/r_fixture/notes",
                      "/api/v1/dispatch/r_fixture/machine/"):
            with self.subTest(route=route):
                self.client.post(route, original)
                self.assertNotIn(PERSON, json.dumps(self.calls[-1][2]))
                self.assertNotEqual(self.calls[-1][2]["source"], SOURCE)
                self.assertEqual(original["source"], SOURCE)

    def test_raw_json_metadata_cannot_bypass_the_shared_cleaner(self):
        for buffer in (bytes, bytearray, memoryview):
            with self.subTest(buffer=buffer):
                self.client._call("POST", "/api/v1/dispatch/future-status",
                                  raw=buffer(json.dumps({"error": REASON}).encode()),
                                  headers={"Content-Type": "application/json"})
                self.assert_clean_reason(self.calls[-1][2])

    def test_tuple_metadata_is_cleaned_before_json_serialization(self):
        self.client.post("/api/v1/dispatch/future-status", {"locations": (SOURCE, (SOURCE,))})
        self.assertNotIn(PERSON, json.dumps(self.calls[-1][2]))

    def test_new_run_metadata_fields_are_cleaned_while_arguments_remain_exact(self):
        args = ["--expect-url", "/dashboard", "--expect-js", "location.pathname === '/done'"]
        body = {"args": args, "future_diagnostics": {"source": SOURCE}}
        self.client.post("/api/v1/runs", body)
        self.assertNotIn(PERSON, json.dumps(self.calls[-1][2]))
        self.assertEqual(self.calls[-1][2]["args"], args)
        self.assertEqual(body["future_diagnostics"]["source"], SOURCE)

    def test_run_input_declarations_stay_exact_while_new_metadata_is_cleaned(self):
        inputs = {
            "args": ["--expect-url", "/dashboard", "--expect-js", "location.pathname === '/done'"],
            "files": [{"path": "in/suite.yaml", "sha256": "a" * 64, "size": 17}],
            "serve": {"path": "site", "port": 8877},
            "seed_files": [{"file": SOURCE + "/first.json"}, {"file": SOURCE + "/second.json"}],
        }
        body = {**inputs, "future_diagnostics": {"source": SOURCE}}
        self.client.post("/api/v1/runs", body)
        sent = self.calls[-1][2]
        for field, value in inputs.items():
            self.assertEqual(sent[field], value)
        self.assertNotIn(PERSON, json.dumps(sent["future_diagnostics"]))
        self.assertEqual(body["future_diagnostics"]["source"], SOURCE)

    def test_device_codes_keep_their_exact_opaque_values(self):
        original = {"device_code": "/opaque/code/value"}
        self.client.post("/api/v1/device/token", original)
        self.assertEqual(self.calls[-1][2], original)

    def test_exact_input_fields_apply_only_to_their_own_route(self):
        cases = (
            ("/api/v1/runs", {"args": [SOURCE, "/dashboard"], "files": [{"path": "in/qa.yaml"}],
                              "device_code": SOURCE + "/opaque"}, ("args", "files"), ("device_code",)),
            ("/api/v1/device/token", {"device_code": "/opaque/code/value", "args": [SOURCE],
                                      "files": [{"path": SOURCE}], "serve": {"path": SOURCE},
                                      "seed_files": [{"file": SOURCE}]},
             ("device_code",), ("args", "files", "serve", "seed_files")),
        )
        for route, body, exact, metadata in cases:
            original = json.loads(json.dumps(body))
            for buffer in (None, bytes, bytearray, memoryview):
                with self.subTest(route=route, buffer=buffer):
                    if buffer is None:
                        self.client.post(route, body)
                    else:
                        self.client._call("POST", route, raw=buffer(json.dumps(body).encode()),
                                          headers={"Content-Type": "application/json"})
                    sent = self.calls[-1][2]
                    for field in exact:
                        self.assertEqual(sent[field], original[field])
                    for field in metadata:
                        self.assertNotIn(PERSON, json.dumps(sent[field]))
                        self.assertNotEqual(sent[field], original[field])
                    self.assertEqual(body, original)

    def test_query_and_fragment_do_not_change_exact_input_field_scopes(self):
        cases = (
            ("/api/v1/runs", {"args": ["--expect-url", "/dashboard", "--expect-js",
                                       "location.pathname === '/done'", SOURCE],
                               "files": [{"path": "in/qa.yaml"}], "source": SOURCE}, ("args", "files")),
            ("/api/v1/device/token", {"device_code": SOURCE + "/opaque", "source": SOURCE}, ("device_code",)),
        )
        for route, body, exact in cases:
            original = json.loads(json.dumps(body))
            for suffix in ("?x=1", "#section", "?x=1#section"):
                for buffer in (None, bytes, bytearray, memoryview):
                    with self.subTest(route=route, suffix=suffix, buffer=buffer):
                        if buffer is None:
                            self.client.post(route + suffix, body)
                        else:
                            self.client._call("POST", route + suffix, raw=buffer(json.dumps(body).encode()),
                                              headers={"Content-Type": "application/json"})
                        sent = self.calls[-1][2]
                        for field in exact:
                            self.assertEqual(sent[field], original[field])
                        self.assertNotIn(PERSON, sent["source"])
                        self.assertEqual(body, original)

    def test_only_device_code_is_exact_while_token_request_metadata_is_cleaned(self):
        original = {"device_code": "/opaque/code/value", "error": REASON, "source": SOURCE}
        for buffer in (None, bytes, bytearray, memoryview):
            with self.subTest(buffer=buffer):
                if buffer is None:
                    self.client.post("/api/v1/device/token", original)
                else:
                    self.client._call("POST", "/api/v1/device/token",
                                      raw=buffer(json.dumps(original).encode()),
                                      headers={"Content-Type": "application/json"})
                sent = self.calls[-1][2]
                self.assert_clean_reason(sent)
                self.assertEqual(sent["device_code"], original["device_code"])
                self.assertNotEqual(sent["source"], SOURCE)
                self.assertEqual(original["error"], REASON)
