"""Strict request-start audit for isolated local Chrome fixtures."""

import json
import re
from urllib.parse import urlsplit

LOCAL = {"127.0.0.1", "localhost", "::1"}
SKIP = {"data", "blob", "about", "chrome", "devtools", "chrome-extension", "file", "chrome-untrusted"}
STARTS = {"URL_REQUEST_START_JOB", "HTTP_STREAM_JOB_CONTROLLER", "HTTP_STREAM_REQUEST_STARTED_JOB",
          "SOCKET_POOL_CONNECT_JOB_CREATED", "TCP_CLIENT_SOCKET_POOL_REQUESTED_SOCKET",
          "TCP_CLIENT_SOCKET_POOL_REQUESTED_SOCKETS", "WEBSOCKET_SEND_REQUEST_HEADERS",
          "HOST_RESOLVER_MANAGER_REQUEST"}


def load(path):
    raw = path.read_text(encoding="utf-8", errors="replace").rstrip()
    for tail in ("", "}", "]}", "}]}"):
        try:
            return json.loads(raw.rstrip(",") + tail if tail else raw)
        except ValueError:
            continue
    raise ValueError("Chrome network log is incomplete")


def host_of(value):
    value = value.strip().split(" ")[0]
    if "://" in value:
        parts = urlsplit(value)
        return parts.scheme, parts.hostname or ""
    return "", re.sub(r":\d+$", "", value).strip("[]")


def bad_hosts(log):
    names = {value: name for name, value in log["constants"]["logEventTypes"].items()}
    bad = {}
    for event in log.get("events", []):
        if names.get(event.get("type")) not in STARTS:
            continue
        params = event.get("params") or {}
        for key in ("url", "group_id", "group_name", "host"):
            if isinstance(params.get(key), str):
                scheme, host = host_of(params[key])
                if scheme in SKIP or not host or host in LOCAL:
                    continue
                bad[host] = bad.get(host, 0) + 1
    return bad
