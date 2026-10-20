"""A late upload reply cannot cause cancellation of an already delivered input."""

import hashlib
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message

import pytest

from qajev import cloud

KEY = "synthetic-credential"
RUN = "r_receipt1"


class Reply:
    def __init__(self, data=b'{"ok": true}'):
        self.data = data
        self.status = 200
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.data


def dashboard(monkeypatch, data, *, reply=None, get_error=None, refuse_first=False, exhaust=False):
    calls, waits = [], []
    stored = False
    client = cloud.Client("http://127.0.0.1:8878", KEY)
    monkeypatch.setattr(cloud.time, "sleep", waits.append)

    def urlopen(request, *, timeout):
        nonlocal stored
        method, path = request.get_method(), urllib.parse.urlsplit(request.full_url).path
        calls.append((method, path, request, timeout))
        if method == "POST":
            return Reply(json.dumps({"run": {"id": RUN}}).encode())
        if method == "GET":
            if get_error:
                raise get_error
            return Reply(data if reply is None else reply)
        assert method == "PUT"
        assert request.data == data
        if not stored and not refuse_first:
            stored = True  # All declared input bytes arrived; dispatch advances the run to running.
            raise TimeoutError("the stored upload reply was lost")
        if exhaust:
            raise TimeoutError("the reply is still lost")
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "queued inputs only",
            Message(),
            io.BytesIO(b'{"error": "An agent key may upload only a queued run input."}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return client, calls, waits


def submit(client, tmp_path, data, remote="site/f/frame.jpg"):
    file = tmp_path / "input"
    file.write_bytes(data)
    plan = cloud.Plan("run", ["run", "in/suite.yaml"], "lost upload reply", files=[(remote, file)])
    return cloud.submit(client, plan)


@pytest.mark.parametrize("data", [b"\xff\xd8" + bytes(40575), b'{"value": "private fixture"}\n'])
def test_stored_last_input_is_verified_without_rewriting_a_running_run(monkeypatch, tmp_path, data):
    client, calls, waits = dashboard(monkeypatch, data)
    assert submit(client, tmp_path, data) == RUN
    assert [path for method, path, _, _ in calls if method == "POST"] == ["/api/v1/runs"]
    puts = [request for method, _, request, _ in calls if method == "PUT"]
    reads = [(path, request, timeout) for method, path, request, timeout in calls if method == "GET"]
    assert len(puts) == 2 and len(reads) == 1 and waits == [2]
    assert reads[0][0] == f"/api/v1/runs/{RUN}/files/site/f/frame.jpg"
    assert reads[0][1].get_header("Authorization") == f"Bearer {KEY}" and reads[0][2] == 30
    assert all(req.get_header("X-content-sha256") == hashlib.sha256(data).hexdigest() for req in puts)


def test_lost_final_reply_recovers_with_one_confirmation_read_after_the_put_budget(monkeypatch, tmp_path):
    data = b"delivered input"
    client, calls, waits = dashboard(monkeypatch, data, exhaust=True)
    assert submit(client, tmp_path, data) == RUN
    assert [method for method, _, _, _ in calls] == ["POST", "PUT", "PUT", "PUT", "GET"]
    assert waits == [2, 5]


@pytest.mark.parametrize("reply", [b"wrong input bytes", b"expected input!", b""])
def test_missing_or_different_input_never_counts_as_an_upload_receipt(monkeypatch, tmp_path, reply):
    data = b"expected input?"
    client, calls, _ = dashboard(monkeypatch, data, reply=reply)
    with pytest.raises(cloud.CloudError) as caught:
        submit(client, tmp_path, data)
    assert caught.value.code == 4
    assert [path for method, path, _, _ in calls if method == "POST"] == ["/api/v1/runs", f"/api/v1/runs/{RUN}/stop"]
    assert len([method for method, _, _, _ in calls if method == "GET"]) == 1


@pytest.mark.parametrize("status", [403, 404])
def test_unreadable_or_protected_input_does_not_bypass_worker_permissions(monkeypatch, tmp_path, status):
    data = b"private input"
    error = urllib.error.HTTPError(
        "http://127.0.0.1/file", status, "not readable", Message(), io.BytesIO(b'{"error": "not readable"}')
    )
    client, calls, _ = dashboard(monkeypatch, data, get_error=error)
    with pytest.raises(cloud.CloudError):
        submit(client, tmp_path, data, remote="in/seed/fixture.json")
    assert [path for method, path, _, _ in calls if method == "POST"][-1] == f"/api/v1/runs/{RUN}/stop"
    assert len([method for method, _, _, _ in calls if method == "GET"]) == 1


def test_confirmation_timeout_is_bounded_and_keeps_the_original_upload_failure(monkeypatch, tmp_path):
    client, calls, waits = dashboard(
        monkeypatch, b"input", get_error=TimeoutError("confirmation timed out"), exhaust=True
    )
    with pytest.raises(cloud.CloudError) as caught:
        submit(client, tmp_path, b"input")
    assert "3 attempts" in str(caught.value) and "the reply is still lost" in str(caught.value)
    assert "confirmation timed out" not in str(caught.value)
    assert [method for method, _, _, _ in calls] == ["POST", "PUT", "PUT", "PUT", "GET", "POST"]
    assert waits == [2, 5]


def test_an_initial_http_refusal_is_never_accepted_via_a_read(monkeypatch, tmp_path):
    client, calls, waits = dashboard(monkeypatch, b"input", refuse_first=True)
    with pytest.raises(cloud.CloudError):
        submit(client, tmp_path, b"input")
    assert [method for method, _, _, _ in calls] == ["POST", "PUT", "POST"]
    assert waits == []


def test_no_empty_response_can_confirm_a_zero_byte_upload(monkeypatch, tmp_path):
    client, calls, _ = dashboard(monkeypatch, b"", reply=b"")
    with pytest.raises(cloud.CloudError):
        submit(client, tmp_path, b"")
    assert len([method for method, _, _, _ in calls if method == "GET"]) == 1


def test_confirmation_uses_the_same_run_encoded_file_path_and_large_timeout(monkeypatch):
    data = b"large part"
    client, calls, _ = dashboard(monkeypatch, data)
    monkeypatch.setattr(client, "timeout_for", lambda size: 210)
    receipt = client.put_file(RUN, "site/a b.part", data)
    assert isinstance(receipt, dict)
    assert receipt["sha256"] == hashlib.sha256(data).hexdigest() and receipt["size"] == len(data)
    assert [(path, timeout) for method, path, _, timeout in calls if method == "GET"] == [
        (f"/api/v1/runs/{RUN}/files/site/a%20b.part", 210)
    ]
