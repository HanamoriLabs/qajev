"""Request starts, including DNS and sockets, are audited independently of response success."""

import runpy
from pathlib import Path

import pytest

AUDIT = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/chrome-netlog-audit.py"))
STARTS = ("URL_REQUEST_START_JOB", "HTTP_STREAM_JOB_CONTROLLER", "HTTP_STREAM_REQUEST_STARTED_JOB",
          "SOCKET_POOL_CONNECT_JOB_CREATED", "TCP_CLIENT_SOCKET_POOL_REQUESTED_SOCKET",
          "TCP_CLIENT_SOCKET_POOL_REQUESTED_SOCKETS", "WEBSOCKET_SEND_REQUEST_HEADERS",
          "HOST_RESOLVER_MANAGER_REQUEST")


@pytest.mark.parametrize("kind", STARTS)
@pytest.mark.parametrize("key", ["url", "group_id", "group_name", "host"])
def test_each_request_start_kind_and_host_field_rejects_a_nonlocal_host(kind, key):
    log = {"constants": {"logEventTypes": {kind: 1}},
           "events": [{"type": 1, "params": {key: "https://outside.example.test/fixture"}}]}
    assert AUDIT["bad_hosts"](log) == {"outside.example.test": 1}


def test_a_resolver_marker_is_not_exempted_and_headers_are_not_request_hosts():
    log = {"constants": {"logEventTypes": {"HOST_RESOLVER_MANAGER_REQUEST": 1}}, "events": [
        {"type": 1, "params": {"host": "~notfound"}},
        {"type": 1, "params": {"host": "127.0.0.1:12345", "headers": "Origin: https://outside.example.test"}},
    ]}
    assert AUDIT["bad_hosts"](log) == {"~notfound": 1}


def test_repeated_nonlocal_starts_keep_their_count():
    log = {"constants": {"logEventTypes": {"URL_REQUEST_START_JOB": 1}}, "events": [
        {"type": 1, "params": {"url": "https://outside.example.test/first"}},
        {"type": 1, "params": {"url": "https://outside.example.test/second"}},
    ]}
    assert AUDIT["bad_hosts"](log) == {"outside.example.test": 2}


@pytest.mark.parametrize("value", ["http://127.0.0.1:12345/fixture", "http://localhost/fixture", "http://[::1]/",
                                  "chrome://newtab", "devtools://devtools", ""])
def test_only_local_hosts_and_browser_internal_schemes_are_allowed(value):
    log = {"constants": {"logEventTypes": {"URL_REQUEST_START_JOB": 1}},
           "events": [{"type": 1, "params": {"url": value}}]}
    assert AUDIT["bad_hosts"](log) == {}
