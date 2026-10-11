"""Clicks into a same-origin iframe right after the page loads, in fresh sessions: prints how many of 12 landed.

Run in a process of its own (test_click_at.py does): inside pytest's process the same steps landed on QAJev 0.4.10
too, but on their own they landed 0 of 12 (12 Oct 2026). QAJev 0.4.11 waits before a click on a frame (FRAME_INPUT_S),
and 12 of 12 land. Its own visible, throwaway Chrome; a local fixture; nothing leaves the machine.
"""

import http.server
import os
import tempfile
import threading
import time
from functools import partial
from pathlib import Path

from qajev import chrome
from qajev import session as session_mod
from qajev.ledger import Ledger

PAGE = ("<!doctype html><title>w</title><style>body{margin:0}iframe{position:absolute;left:40px;top:30px;"
        "width:300px;height:200px;border:0}</style><iframe id=view src='frame.html'></iframe>")
FRAME = ("<!doctype html><style>body{margin:0}canvas{display:block}</style><canvas width=300 height=200></canvas>"
         "<script>window.clicks=[];document.querySelector('canvas').addEventListener('pointerdown',e=>clicks.push(1));"
         "</script>")


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def main():
    root = Path(tempfile.mkdtemp())
    (root / "page.html").write_text(PAGE)
    (root / "frame.html").write_text(FRAME)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{server.server_address[1]}"
    record = chrome.start(f"frame-click-{os.getpid()}", headless=False, ephemeral=True)
    landed = 0
    try:
        session_mod.configure_env(record["cdp_url"])
        for size in ((1024, 700), (1280, 900)):
            for navigations in (1, 2):
                for _ in range(3):
                    s = session_mod.Session(Ledger(0.0), headless=False, hosts={host})
                    try:
                        s.set_device({"width": size[0], "height": size[1], "mobile": False, "scale": 1})
                        s.arm("readonly")
                        for _ in range(navigations):
                            s.navigate(f"http://{host}/page.html")
                        s.trusted_click(160, 110)  # 40 + 120, 30 + 80: the frame's 120, 80
                        time.sleep(0.4)
                        landed += s.evaluate("document.getElementById('view').contentWindow.clicks.length")
                    finally:
                        s.close()
        session_mod.stop_daemon()
    finally:
        chrome.stop(record["state_key"])
        server.shutdown()
    print(f"landed {landed} of 12")


if __name__ == "__main__":
    main()
