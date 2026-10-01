"""Local fixture server for QAJev experiments: static pages plus a feedback API that records submissions.

    python tests/fixtures/serve.py [PORT]      # default 8765, 127.0.0.1 only
GET /api/feedback lists what was received, so a suite can check the side effect.
"""

import http.server
import json
import sys
from functools import partial
from pathlib import Path

RECEIVED = []


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _download_redirect(self):
        if self.path.startswith("/download?platform="):  # like a real "Download for Mac": a redirect to a file
            self.send_response(302)
            self.send_header("location", "/files/app.dmg")
            self.end_headers()
            return True
        return False

    def do_HEAD(self):
        if not self._download_redirect():
            super().do_HEAD()

    def do_GET(self):
        if self._download_redirect():
            return None
        if self.path == "/api/feedback":
            return self._json(200, {"count": len(RECEIVED), "items": RECEIVED})
        return super().do_GET()

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length).decode(errors="replace")
        if self.path == "/api/feedback":
            try:
                RECEIVED.append(json.loads(raw))
            except json.JSONDecodeError:
                return self._json(400, {"error": "bad json"})
            return self._json(200, {"ok": True})
        return self._json(200, {"ok": True})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    site = Path(__file__).parent / "site"
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(site)))
    print(f"fixture site on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()
