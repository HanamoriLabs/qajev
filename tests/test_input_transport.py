"""Declared inputs stay byte-exact; only QAJev's own results and metadata are cleaned."""
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qajev import cloud

ASSETS = {
    "app.js": b'import "/assets/lib.js"; const API = "/api/items"; const re = /[a-z]+/g;',
    "style.css": b"body { background: url(/img/bg.png) }",
    "index.html": b'<main data-route="/shop"><script src="/app.js"></script></main>',
    "assets/lib.js": b'export const home = "/home";',
    "source.json": b'{"source":"/home/input-owner/source.js","route":"/done"}',
    "raw.bin": b"\xff\x00opaque-binary\x00",
}
SUITE = (b"name: exact inputs\nscenarios:\n  - name: route\n    url: /done\n    expect:\n"
         b"      js: [\"location.pathname === '/done'\"]\n      text: ['Saved to /tmp/out']\n")
ARGS = ["run", "in/qa.yaml", "--expect-url", "/dashboard", "--expect-js", "location.pathname === '/done'"]


class Reply:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, value):
        self.value = value

    def read(self):
        return json.dumps(self.value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class InputTransportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls, self.uploads, self.declarations = [], {}, {}
        self.client = cloud.Client("http://127.0.0.1:9", "synthetic-key")
        patch = mock.patch("urllib.request.urlopen", side_effect=self.request)
        patch.start()
        self.addCleanup(patch.stop)

    def request(self, req, timeout=None):
        path = req.full_url.split(":9", 1)[1]
        method = req.get_method()
        self.calls.append((method, path, req.data))
        if "/files/" in path:
            remote = path.split("/files/", 1)[1]
            self.uploads[remote] = req.data
            self.assertEqual(req.get_header("X-content-sha256"), hashlib.sha256(req.data).hexdigest())
            self.assertEqual(int(req.get_header("X-content-size")), len(req.data))
            return Reply({"ok": True})
        if path == "/api/v1/runs":
            body = json.loads(req.data)
            self.declarations = {f["path"]: f for f in body.get("files", [])}
            return Reply({"run": {"id": "r_exactinputs"}})
        return Reply({"ok": True})

    def posted(self):
        return json.loads(next(raw for method, path, raw in self.calls
                               if method == "POST" and path == "/api/v1/runs"))

    def submit(self, packed=False):
        site = self.root / "site"
        for name, raw in ASSETS.items():
            target = site / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        suite = self.root / "qa.yaml"
        suite.write_bytes(SUITE)
        plan = cloud.Plan("run", list(ARGS), "exact inputs", files=[("in/qa.yaml", suite)],
                          serve_root=site, port=8877)
        with mock.patch.object(cloud, "PACK_OVER_FILES", 0 if packed else 150), \
             mock.patch.object(cloud, "PART_BYTES", 257 if packed else 45 * 1024 * 1024):
            cloud.submit(self.client, plan)
        self.assertEqual(set(self.declarations), set(self.uploads))
        for name, raw in self.uploads.items():
            self.assertEqual(self.declarations[name]["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(self.declarations[name]["size"], len(raw))
        self.assertEqual(suite.read_bytes(), SUITE)
        for name, raw in ASSETS.items():
            self.assertEqual((site / name).read_bytes(), raw)

    def test_direct_declared_site_files_keep_imports_routes_regex_css_and_binary_bytes(self):
        for name, raw in ASSETS.items():
            with self.subTest(name=name):
                self.client.put_file("r_exactinputs", "site/" + name, raw)
                self.assertEqual(self.uploads["site/" + name], raw)

    def test_declared_suite_checks_and_expected_text_keep_exact_bytes(self):
        self.client.put_file("r_exactinputs", "in/qa.yaml", SUITE)
        self.assertEqual(self.uploads["in/qa.yaml"], SUITE)
        raw = b'{"source":"/home/input-owner/raw.json"}'
        for name in ("in/report.json", "payload.json", "report.json"):
            self.client.put_file("r_exactinputs", name, raw)
            self.assertEqual(self.uploads[name], raw)

    def test_creation_args_preserve_root_relative_expectations_and_embedded_js(self):
        body = {"kind": "run", "args": ARGS, "name": "unchanged checks", "files": []}
        self.client.post("/api/v1/runs", body)
        self.assertEqual(self.posted()["args"], ARGS)
        self.assertEqual(body["args"], ARGS)

    def test_actual_submission_preserves_all_declared_input_bytes_and_args(self):
        self.submit()
        self.assertEqual(self.posted()["args"], ARGS)
        self.assertEqual(self.uploads["in/qa.yaml"], SUITE)
        for name, raw in ASSETS.items():
            self.assertEqual(self.uploads["site/" + name], raw)

    def test_actual_packed_submission_is_readable_and_members_and_checks_are_exact(self):
        self.submit(packed=True)
        parts = sorted(name for name in self.uploads if name.startswith("site/qcloud-archive."))
        self.assertGreater(len(parts), 1)
        packed = b"".join(self.uploads[name] for name in parts)
        with tarfile.open(fileobj=io.BytesIO(packed)) as archive:
            self.assertEqual(set(archive.getnames()), set(ASSETS))
            for member in archive.getmembers():
                self.assertEqual(member.uid, 0)
                self.assertEqual(member.gid, 0)
                self.assertEqual(member.uname, "")
                self.assertEqual(member.gname, "")
                stream = archive.extractfile(member)
                assert stream is not None
                self.assertEqual(stream.read(), ASSETS[member.name])
        self.assertEqual(self.posted()["args"], ARGS)
        self.assertEqual(self.uploads["in/qa.yaml"], SUITE)

    def test_report_and_log_artifacts_still_hide_local_paths(self):
        from qajev_upload_privacy import upload_artifact

        raw = b"source: /home/result-owner/work/qa.yaml failed because the expected item is absent"
        for name in ("report.md", "reports/extra.log", "stdout.log"):
            with self.subTest(name=name):
                upload_artifact(self.client, "r_exactinputs", name, raw)
                data = self.uploads[name]
                self.assertNotIn(b"result-owner", data)
                self.assertIn(b"failed because the expected item is absent", data)

    def test_finish_and_event_metadata_still_hide_local_paths(self):
        body = {"report": {"run_dir": "/home/result-owner/runs/one",
                           "source": r"C:\Users\result-owner\qa.yaml"}}
        self.client.post("/api/v1/jobs/j_fixture/finish", body)
        self.client.post("/api/v1/jobs/j_fixture/events",
                         {"events": [{"doing": "opening /Users/result-owner/qa.yaml"}]})
        self.assertNotIn(b"result-owner", b"".join(raw or b"" for _, _, raw in self.calls))
