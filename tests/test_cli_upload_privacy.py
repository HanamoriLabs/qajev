"""Capture the submitting CLI's real HTTP uploads; never contact a cloud service."""

import hashlib
import io
import json
import tarfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from qajev import cli, cloud, config
from qajev_upload_privacy import restore_paths, sanitize_upload

PRIVATE_PATHS = (
    "/Users/upload-fixture/project/input.json",
    "/home/upload-fixture/project/input.json",
    r"C:\Users\upload-fixture\project\input.json",
)
RUN = "r_uploadprivacy"


class CaptureDashboard:
    def __init__(self):
        self.calls, self.uploads = [], {}
        self.after_create = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, data):
                raw = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def read_body(self):
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def do_POST(self):
                raw = self.read_body()
                outer.calls.append((self.command, self.path, raw))
                if self.path != "/api/v1/runs":
                    return self.reply(404, {"error": "unexpected POST"})
                if outer.after_create:
                    outer.after_create()
                self.reply(201, {"run": {"id": RUN}})

            def do_PUT(self):
                raw = self.read_body()
                outer.calls.append((self.command, self.path, raw))
                name = urllib.parse.unquote(self.path.split("/files/", 1)[1])
                outer.uploads[name] = (raw, {key.lower(): value for key, value in self.headers.items()})
                self.reply(200, {"ok": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = cloud.Client(f"http://127.0.0.1:{self.server.server_address[1]}", "synthetic-upload-key")

    def created(self):
        return json.loads(next(raw for method, path, raw in self.calls if method == "POST" and path == "/api/v1/runs"))

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def dashboard(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "HOME", tmp_path / "qajev-home")
    monkeypatch.setattr(config, "env_files", lambda *args: [])
    monkeypatch.chdir(tmp_path)
    dash = CaptureDashboard()
    yield dash
    dash.close()


def assert_public(data):
    for path in PRIVATE_PATHS:
        assert path.encode() not in data and json.dumps(path)[1:-1].encode() not in data


def assert_declarations(dashboard):
    declared = {item["path"]: item for item in dashboard.created()["files"]}
    assert set(declared) == set(dashboard.uploads)
    for name, (data, headers) in dashboard.uploads.items():
        digest = hashlib.sha256(data).hexdigest()
        assert declared[name]["sha256"] == headers["x-content-sha256"] == digest
        assert declared[name]["size"] == int(headers["x-content-size"]) == len(data)


def make_plan(tmp_path, *, serve=True):
    site = tmp_path / "dist"
    site.mkdir()
    payloads = {
        "index.html": (
            '<a href="/home/login">Login</a><script src="app.js"></script>\n'
            + "\n".join(f"<!-- source: {path} -->" for path in PRIVATE_PATHS)
        ).encode(),
        "app.js": ("fetch('/api/profile');\n" + "\n".join(f"// source {path}" for path in PRIVATE_PATHS)).encode(),
        "data.json": json.dumps({"url": "/home/login", "source_paths": list(PRIVATE_PATHS)}).encode(),
        "safe.html": b'<a href="/home/login">Login</a><img src="./logo.png"><script src="app.js"></script>',
        "logo.png": b"\x89PNG\r\n\x1a\n\xff\x00safe-image",
    }
    for name, raw in payloads.items():
        (site / name).write_bytes(raw)
    suite = tmp_path / "upload.yaml"
    suite.write_text(
        "name: upload fixture\nbase_url: http://127.0.0.1:8877\nscenarios:\n"
        "  - name: home\n    url: /home/login\n    about: the page loads\n"
        "    expect: {text: [Login]}\n" + "\n".join(f"# source: {path}" for path in PRIVATE_PATHS)
    )
    argv = ["run", str(suite), "--runner", "cloud", *(["--serve", str(site)] if serve else [])]
    args = cli.build_parser().parse_args(argv)
    args.argv, args.runner_named = argv, True
    plan = cloud.plan(args)
    return plan, site, suite, payloads


@pytest.mark.parametrize("path", PRIVATE_PATHS)
def test_direct_put_sanitizes_before_http_hash_size_and_payload(dashboard, path):
    original = json.dumps({"source": path, "url": "/home/login", "relative": "assets/logo.png"}).encode()
    dashboard.client.put_file(RUN, "in/source.json", original)
    data, headers = dashboard.uploads["in/source.json"]
    assert data == sanitize_upload(original) and data != original
    assert_public(data)
    assert headers["x-content-sha256"] == hashlib.sha256(data).hexdigest()
    assert int(headers["x-content-size"]) == len(data)
    assert json.loads(data)["url"] == "/home/login"


def test_direct_post_sanitizes_nested_metadata_before_http(dashboard):
    original = {
        "name": PRIVATE_PATHS[0],
        "args": [PRIVATE_PATHS[1]],
        "tunnel": {"source": {"file": PRIVATE_PATHS[2]}},
        "url": "/home/login",
    }
    dashboard.client.post("/api/v1/runs", original)
    assert dashboard.created() == json.loads(sanitize_upload(json.dumps(original).encode()))
    assert_public(dashboard.calls[0][2])
    assert original["name"] == PRIVATE_PATHS[0]


def test_safe_relative_web_assets_remain_byte_exact(dashboard):
    raw = b'<a href="/home/login">Login</a><img src="./logo.png"><script src="app.js"></script>'
    dashboard.client.put_file(RUN, "site/safe.html", raw)
    assert dashboard.uploads["site/safe.html"][0] == raw


def test_submit_serve_prepares_public_files_and_matching_metadata_without_changing_originals(tmp_path, dashboard):
    plan, site, suite, originals = make_plan(tmp_path)
    source = suite.read_bytes()
    plan.name = " / ".join(PRIVATE_PATHS)
    plan.inline.append(("in/extra.json", json.dumps({"source": PRIVATE_PATHS[0]}).encode()))
    assert cloud.submit(dashboard.client, plan) == RUN
    assert_declarations(dashboard)
    assert_public(json.dumps(dashboard.created()).encode())
    for name, raw in originals.items():
        assert (site / name).read_bytes() == raw
        assert dashboard.uploads[f"site/{name}"][0] == sanitize_upload(raw)
    assert suite.read_bytes() == source
    assert dashboard.uploads["in/upload.yaml"][0] == sanitize_upload(source)
    assert dashboard.uploads["in/extra.json"][0] == sanitize_upload(plan.inline[0][1])
    assert dashboard.uploads["site/safe.html"][0] == originals["safe.html"]
    assert dashboard.uploads["site/logo.png"][0] == originals["logo.png"]


def test_submit_uses_preflight_copies_even_if_original_changes_after_create(tmp_path, dashboard):
    plan, site, suite, originals = make_plan(tmp_path)
    source = suite.read_bytes()

    def change_originals():
        suite.write_text("changed after create " + PRIVATE_PATHS[0])
        (site / "app.js").write_text("changed after create " + PRIVATE_PATHS[1])

    dashboard.after_create = change_originals
    cloud.submit(dashboard.client, plan)
    assert_declarations(dashboard)
    assert dashboard.uploads["site/app.js"][0] == sanitize_upload(originals["app.js"])
    assert dashboard.uploads["in/upload.yaml"][0] == sanitize_upload(source)


def test_packed_submit_sanitizes_members_before_splitting_and_clears_tar_owners(tmp_path, dashboard, monkeypatch):
    plan, site, suite, originals = make_plan(tmp_path)
    # Root-relative web URLs cross small part boundaries: sanitizing chunks after packing would corrupt the tar.
    originals["boundary.html"] = b"x" * 1000 + b'<a href="/home/login">ok</a>'
    (site / "boundary.html").write_bytes(originals["boundary.html"])
    monkeypatch.setattr(cloud, "PACK_OVER_FILES", 0)
    monkeypatch.setattr(cloud, "PART_BYTES", 257)
    cloud.submit(dashboard.client, plan)
    assert_declarations(dashboard)
    parts = sorted(name for name in dashboard.uploads if name.startswith("site/qcloud-archive."))
    assert len(parts) > 1
    archive = b"".join(dashboard.uploads[name][0] for name in parts)
    assert_public(archive)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        assert set(tar.getnames()) == set(originals)
        for member in tar.getmembers():
            assert member.uid == member.gid == 0 and member.uname == member.gname == ""
            assert not member.name.startswith("/") and ".." not in member.name.split("/") and "\\" not in member.name
            stream = tar.extractfile(member)
            assert stream is not None and stream.read() == sanitize_upload(originals[member.name])
    for name, raw in originals.items():
        assert (site / name).read_bytes() == raw


@pytest.mark.parametrize("packed", [False, True])
def test_binary_path_metadata_is_refused_before_any_run_is_created(tmp_path, dashboard, monkeypatch, packed):
    plan, site, suite, originals = make_plan(tmp_path)
    unsafe = b"\x89PNG\xff\x00" + PRIVATE_PATHS[0].encode()
    (site / "unsafe.png").write_bytes(unsafe)
    if packed:
        monkeypatch.setattr(cloud, "PACK_OVER_FILES", 0)
    with pytest.raises(cloud.CloudError, match="binary artifact contains a local filesystem path"):
        cloud.submit(dashboard.client, plan)
    assert dashboard.calls == [] and (site / "unsafe.png").read_bytes() == unsafe


def test_direct_binary_refusal_cannot_bypass_the_submit_preflight(dashboard):
    with pytest.raises(cloud.CloudError, match="binary artifact contains a local filesystem path"):
        dashboard.client.put_file(RUN, "shots/unsafe.png", b"\xff\x00" + PRIVATE_PATHS[1].encode())
    assert dashboard.calls == []


def test_actual_seeded_plan_keeps_public_suite_reference_and_fixture_mapping(tmp_path, dashboard):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "qa").mkdir()
    (repo / "fixtures").mkdir()
    seed = repo / "fixtures/users.json"
    original_seed = b'{"test_account":true,"allowed_hosts":["localhost"],"password_a":"test-value-123"}'
    seed.write_bytes(original_seed)
    suite = repo / "qa/seed.yaml"
    original_suite = (
        "name: seeded fixture\nbase_url: http://localhost:8877\naccount:\n"
        "  email: qa@example.test\n  password: seed:../fixtures/users.json#password_a\n"
        "  login: {url: /login}\ntunnel:\n  data: local\n"
        "  ports: {8877: {command: 'node fixture.js'}}\nscenarios:\n"
        "  - name: home\n    url: /\n    about: the page loads\n    expect: {text: [Hi]}\n"
    )
    suite.write_text(original_suite)
    argv = ["run", str(suite), "--runner", "cloud"]
    args = cli.build_parser().parse_args(argv)
    args.argv, args.runner_named = argv, True
    plan = cloud.plan(args)
    assert plan.tunnel is not None
    plan.tunnel["ports"]["8877"]["command"] = "node " + str(repo / "fixture.js")
    cloud.submit(dashboard.client, plan)
    public_ref = json.loads(sanitize_upload(json.dumps(str(seed.resolve())).encode()))
    assert public_ref.startswith("local/")
    expected = f"in/seed/{hashlib.sha256(public_ref.encode()).hexdigest()[:16]}.json"
    assert cloud.seed_remote(str(seed.resolve())) == cloud.seed_remote(public_ref) == expected
    assert expected in dashboard.uploads
    assert f"seed:{public_ref}#password_a" in dashboard.uploads["in/seed.yaml"][0].decode()
    assert json.loads(dashboard.uploads[expected][0])["password_a"] == "test-value-123"
    assert str(seed.resolve()).encode() not in b"".join(raw for method, path, raw in dashboard.calls)
    assert seed.read_bytes() == original_seed and suite.read_text() == original_suite
    assert restore_paths(dashboard.created()["tunnel"], directory=config.HOME) == plan.tunnel
    assert str(repo.resolve()).encode() not in b"".join(raw for method, path, raw in dashboard.calls)
    registry = [path for path in config.HOME.rglob("*") if path.is_file()]
    assert registry and all(path.stat().st_mode & 0o077 == 0 for path in registry)
    assert all(not name.endswith(path.name) for path in registry for name in dashboard.uploads)
    assert_declarations(dashboard)


def test_private_tunnel_registry_failure_refuses_before_creating_a_run(tmp_path, dashboard, monkeypatch):
    def no_registry(*args, **kwargs):
        raise OSError("synthetic local storage failure")

    monkeypatch.setattr(cloud, "remember_paths", no_registry, raising=False)
    plan = cloud.Plan(
        "check",
        ["check", "http://localhost:8877"],
        "tunnel fixture",
        tunnel={
            "root": str(tmp_path / "repo"),
            "source": {"file": str(tmp_path / "repo/suite.yaml")},
            "ports": {"8877": {"command": "node fixture.js"}},
        },
    )
    with pytest.raises(cloud.CloudError, match="private local tunnel references"):
        cloud.submit(dashboard.client, plan)
    assert dashboard.calls == []
