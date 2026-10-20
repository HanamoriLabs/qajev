"""Cloud transport recovery without credentials, external requests or model calls."""

import hashlib
import io
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from qajev import cli, cloud, config

KEY = "synthetic-credential"
RUN = "r_transport1"


class Reply:
    def __init__(self, data=b'{"ok": true}', *, error: Exception | None = None):
        self.data, self.error = data, error
        self.status = 200
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        if self.error:
            raise self.error
        return self.data


def client_and_waits(monkeypatch):
    waits = []
    monkeypatch.setattr(cloud.time, "sleep", waits.append)
    return cloud.Client("http://127.0.0.1:8878", KEY), waits


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("read operation timed out"),
        urllib.error.URLError("connection reset"),
        ConnectionResetError("reset"),
    ],
)
def test_file_put_retries_transient_errors_with_identical_bytes_and_headers(monkeypatch, failure):
    client, waits = client_and_waits(monkeypatch)
    calls = []
    data = b"\xff\xd8" + bytes(40575)

    def urlopen(request, *, timeout):
        calls.append((request, timeout))
        if len(calls) < 3:
            raise failure
        return Reply()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert client.put_file(RUN, "site/f/frame.jpg", data) == {"ok": True}
    assert waits == [2, 5] and len(calls) == 3
    for request, timeout in calls:
        assert request.get_method() == "PUT" and request.data == data
        assert request.full_url == f"{client.base}/api/v1/runs/{RUN}/files/site/f/frame.jpg"
        assert request.get_header("Authorization") == f"Bearer {KEY}"
        assert request.get_header("X-content-sha256") == hashlib.sha256(data).hexdigest()
        assert request.get_header("X-content-size") == str(len(data)) and timeout == 30


def test_a_reply_body_timeout_retries_the_file_put(monkeypatch):
    client, waits = client_and_waits(monkeypatch)
    calls = []

    def urlopen(request, *, timeout):
        calls.append(request)
        return Reply(error=TimeoutError("reply body timed out")) if len(calls) == 1 else Reply()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert client.put_file(RUN, "site/frame.jpg", b"frame") == {"ok": True}
    assert len(calls) == 2 and waits == [2]


def test_real_http_put_recovers_when_the_first_ack_is_lost(monkeypatch):
    requests, waits = [], []
    data = b"\xff\xd8" + bytes(40575)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_PUT(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append((self.path, body, self.headers["x-content-sha256"]))
            if len(requests) == 1:
                threading.Event().wait(0.6)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
            except ConnectionError:
                pass  # The timed-out client has already closed its first connection.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = cloud.Client(f"http://127.0.0.1:{server.server_address[1]}", KEY)
        monkeypatch.setattr(client, "timeout_for", lambda size: 0.15)
        monkeypatch.setattr(cloud.time, "sleep", waits.append)
        assert client.put_file(RUN, "site/frame.jpg", data) == {"ok": True}
        expected = (f"/api/v1/runs/{RUN}/files/site/frame.jpg", data, hashlib.sha256(data).hexdigest())
        assert requests == [expected, expected] and waits == [2]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_upload_recovery_creates_one_run_and_never_stops_it(monkeypatch, tmp_path):
    client, waits = client_and_waits(monkeypatch)
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"\xff\xd8" + bytes(40575))
    calls = []

    def urlopen(request, *, timeout):
        method, path = request.get_method(), urllib.parse.urlsplit(request.full_url).path
        calls.append((method, path))
        if method == "POST":
            assert path == "/api/v1/runs"
            return Reply(json.dumps({"run": {"id": RUN}}).encode())
        if calls.count((method, path)) == 1:
            raise TimeoutError("read operation timed out")
        return Reply()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    plan = cloud.Plan("run", ["run", "in/suite.yaml"], "upload recovery", files=[("site/f/frame.jpg", image)])
    assert cloud.submit(client, plan) == RUN
    assert [path for method, path in calls if method == "POST"] == ["/api/v1/runs"]
    assert waits == [2]


def test_exhausted_upload_retries_stop_once_and_explain_the_attempts(monkeypatch, tmp_path):
    client, waits = client_and_waits(monkeypatch)
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"frame")
    calls = []

    def urlopen(request, *, timeout):
        method, path = request.get_method(), urllib.parse.urlsplit(request.full_url).path
        calls.append((method, path))
        if method == "POST":
            return Reply(json.dumps({"run": {"id": RUN}}).encode())
        raise TimeoutError(f"read operation timed out {KEY}")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    plan = cloud.Plan("run", ["run", "in/suite.yaml"], "upload exhaustion", files=[("site/frame.jpg", image)])
    with pytest.raises(cloud.CloudError) as caught:
        cloud.submit(client, plan)
    message = str(caught.value)
    assert "the upload of site/frame.jpg failed, so the run was stopped" in message
    assert "3 attempts" in message and "PUT" in message and "30 s" in message
    assert "read operation timed out" in message and KEY not in message
    assert caught.value.code == 4 and caught.value.run_id == RUN
    assert calls.count(("PUT", f"/api/v1/runs/{RUN}/files/site/frame.jpg")) == 3
    assert [path for method, path in calls if method == "POST"] == ["/api/v1/runs", f"/api/v1/runs/{RUN}/stop"]
    assert waits == [2, 5]


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_mutating_calls_never_retry_an_uncertain_reply(monkeypatch, method):
    client, waits = client_and_waits(monkeypatch)
    calls = []

    def urlopen(request, *, timeout):
        calls.append(request)
        raise TimeoutError("reply was lost")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(cloud.CloudError):
        client._call(method, "/api/v1/runs", body={})
    assert len(calls) == 1 and waits == []


@pytest.mark.parametrize("status", [400, 401, 403, 413, 500])
def test_an_http_answer_is_not_retried_or_turned_into_a_timeout(monkeypatch, status):
    client, waits = client_and_waits(monkeypatch)
    calls = []

    def urlopen(request, *, timeout):
        calls.append(request)
        raise urllib.error.HTTPError(
            request.full_url, status, "refused", Message(), io.BytesIO(json.dumps({"error": f"refused {KEY}"}).encode())
        )

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(cloud.ApiError) as caught:
        client.put_file(RUN, "site/frame.jpg", b"frame")
    assert caught.value.status == status and KEY not in str(caught.value)
    assert len(calls) == 1 and waits == []


def test_invalid_json_is_not_retried_as_a_transport_error(monkeypatch):
    client, waits = client_and_waits(monkeypatch)
    calls = []

    def urlopen(request, *, timeout):
        calls.append(request)
        return Reply(b"not json")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(cloud.CloudError):
        client.get("/api/v1/runs/invalid")
    assert len(calls) == 1 and waits == []


def test_an_http_error_with_an_unreadable_body_is_not_retried(monkeypatch):
    client, waits = client_and_waits(monkeypatch)
    calls = []

    class Unreadable(io.BytesIO):
        def read(self, *args):
            raise TimeoutError("error body timed out")

    def urlopen(request, *, timeout):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 400, "refused", Message(), Unreadable())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(cloud.ApiError) as caught:
        client.put_file(RUN, "site/frame.jpg", b"frame")
    assert caught.value.status == 400 and "refused" in str(caught.value)
    assert len(calls) == 1 and waits == []


def test_file_retry_keeps_the_larger_upload_timeout(monkeypatch):
    client, waits = client_and_waits(monkeypatch)
    timeouts = []

    def urlopen(request, *, timeout):
        timeouts.append(timeout)
        if len(timeouts) == 1:
            raise TimeoutError("read timed out")
        return Reply()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(client, "timeout_for", lambda size: 210)
    assert client.put_file(RUN, "site/part", b"part") == {"ok": True}
    assert timeouts == [210, 210] and waits == [2]


@pytest.mark.parametrize("recover", [True, False])
def test_report_json_get_recovers_or_returns_nonzero_with_its_reason(monkeypatch, tmp_path, capsys, recover):
    client, waits = client_and_waits(monkeypatch)
    monkeypatch.setattr(cloud, "connect", lambda *a, **k: client)
    monkeypatch.setattr(config, "HOME", tmp_path)
    report = {"scenarios": [{"name": "scene", "checks": [], "screenshots": []}], "gate": "PASS"}
    calls = []

    def urlopen(request, *, timeout):
        path = urllib.parse.urlsplit(request.full_url).path
        calls.append(path)
        if path.endswith("report.json"):
            if not recover or calls.count(path) == 1:
                raise TimeoutError("report read operation timed out")
            return Reply(json.dumps(report, indent=2).encode())
        return Reply(b"<p>report</p>")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    code = cli.main(["report", RUN, "--json"])
    captured = capsys.readouterr()
    if recover:
        assert code == 0 and json.loads(captured.out) == report
        assert "kept in" in captured.err and waits == [2]
    else:
        assert code == 4 and captured.out == ""
        assert "report read operation timed out" in captured.err and "3 attempts" in captured.err
        assert waits == [2, 5]
