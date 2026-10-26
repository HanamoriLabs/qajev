"""A Chrome-shaped child: loopback discovery and environment metadata only, never environment values."""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

args = dict(arg[2:].split("=", 1) for arg in sys.argv[1:] if arg.startswith("--") and "=" in arg)
profile = Path(args["user-data-dir"])
snapshot = {"names": sorted(os.environ), "pid": os.getpid(),
            "temp": {name: os.environ.get(name) for name in ("TMPDIR", "TMP", "TEMP")}}
(profile / "environment.json").write_text(json.dumps(snapshot))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = json.dumps({"Browser": "Fixture Chrome"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


with HTTPServer(("127.0.0.1", int(args["remote-debugging-port"])), Handler) as server:
    (profile / "DevToolsActivePort").write_text(f"{server.server_port}\n/devtools/browser/fixture\n")
    server.serve_forever()
