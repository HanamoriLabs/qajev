"""Link checking over plain HTTP: cross-origin redirects are fine, HEAD stays HEAD, no file bodies are fetched."""

import http.server
import threading

import pytest

from qajev import smoke

SEEN = []
BASE = []


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _answer(self, method):
        SEEN.append((method, self.path) + ((self.headers.get("range"),) if method == "GET" else ()))
        port = self.server.server_address[1]
        if self.path == "/elsewhere":  # a different origin: 127.0.0.1 -> localhost
            self.send_response(302)
            self.send_header("location", f"http://localhost:{port}/ok")
        elif self.path == "/ok":
            self.send_response(200)
            self.send_header("content-type", "text/html")
        elif self.path == "/no-head" and method == "HEAD":
            self.send_response(405)
        elif self.path == "/no-head":
            self.send_response(200)
            self.send_header("content-type", "text/html")
        elif self.path.startswith("/download"):  # like a site's /download?platform=mac: redirects to the file
            self.send_response(302)
            self.send_header("location", "/big.dmg")
        elif self.path == "/no-head.dmg" and method == "HEAD":
            self.send_response(405)
        elif self.path in ("/big.dmg", "/no-head.dmg"):
            self.send_response(200)
            self.send_header("content-type", "application/x-apple-diskimage")
            self.send_header("content-length", "104857600")
        else:
            self.send_response(404)
        self.end_headers()
        if method == "GET" and self.path in ("/big.dmg", "/no-head.dmg"):
            self.wfile.write(b"x" * 1024)

    def do_HEAD(self):
        self._answer("HEAD")

    def do_GET(self):
        self._answer("GET")


@pytest.fixture(scope="module")
def base():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    BASE[:] = [f"http://127.0.0.1:{server.server_address[1]}"]
    yield BASE[0]
    server.shutdown()


def test_a_cross_origin_redirect_is_a_working_link(base):
    SEEN.clear()
    status, final, _ = smoke.http_status(base + "/elsewhere")
    assert status == 200 and final.startswith("http://localhost:")
    assert SEEN == [("HEAD", "/elsewhere"), ("HEAD", "/ok")]  # HEAD all the way


def test_head_refused_falls_back_to_get(base):
    assert smoke.http_status(base + "/no-head")[0] == 200


def test_a_file_link_is_checked_without_downloading_it(base):
    SEEN.clear()
    status, _, kind = smoke.http_status(base + "/big.dmg")
    assert status == 200 and kind == "application/x-apple-diskimage"
    assert SEEN == [("HEAD", "/big.dmg")]


def test_link_list_reports_only_real_breakage(base):
    result = smoke.check_link_list([base + "/elsewhere", base + "/ok", base + "/gone"], name="links")
    assert result["outcome"] == "fail"
    assert [f["detail"] for f in result["findings"]] == [f"{base}/gone -> 404"]
    assert "1 redirect(s) followed" in result["reason"]


def test_urls_have_one_spelling():
    assert smoke.canon("https://shop.example") == smoke.canon("https://shop.example/") == "https://shop.example/"
    assert smoke.canon("https://x.test/a#top") == "https://x.test/a"
    assert smoke.page_name("https://x.test/download?platform=mac") == "/download?platform=mac"


def test_a_file_whose_server_refuses_head_is_asked_for_one_byte(base):
    # A full GET of a 40 MB installer, dropped after the headers, reads as a client hang-up on the server.
    SEEN.clear()
    status, _, kind = smoke.http_status(BASE[0] + "/no-head.dmg")
    assert status == 200 and kind == "application/x-apple-diskimage"
    assert SEEN == [("HEAD", "/no-head.dmg"), ("GET", "/no-head.dmg", "bytes=0-0")]


def test_a_download_link_is_checked_by_head_and_never_opened_in_the_browser(base):
    assert smoke.looks_like_download(base + "/download?platform=mac")
    assert smoke.looks_like_download("https://x.test/releases/App_1.0.66_x64-setup.exe")
    assert not smoke.looks_like_download("https://x.test/pricing")
    SEEN.clear()
    served = smoke.served_file(base + "/download?platform=mac")
    assert served and served[0] == 200 and served[1].endswith("/big.dmg")
    assert SEEN == [("HEAD", "/download?platform=mac"), ("HEAD", "/big.dmg")]  # no GET of the file
    assert smoke.served_file(base + "/ok") is None  # a page: the browser opens it as usual
