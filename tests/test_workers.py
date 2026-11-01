"""Web Workers run in QAJev's Chrome (twinbox1, 12 Oct 2026).

A stream page (Moonlight Web) stayed on "Selecting pipeline automatically": it asks a module worker what it supports
and waits for the answer. From 0.4.12, QAJev turned on Target.setAutoAttach with waitForDebuggerOnStart to guard
frames on other sites. Its filter named only iframes, but Chrome still held every dedicated worker at its start,
waiting for a debugger that never came, so no worker ever answered. 0.4.11 ran them.

Now QAJev attaches to workers too and lets each run at once, with the block list in it first, so a worker's request
to a blocked address never leaves either.

The live half runs in QAJev's own throwaway Chrome, in a visible window (never headless). Nothing leaves the machine.
"""

import http.server
import os
import threading
import time
from functools import partial

import pytest

live = pytest.mark.skipif(os.environ.get("QAJEV_LIVE") != "1", reason="set QAJEV_LIVE=1 (starts a local Chrome)")

CROSS_SITE = "tool.test"
HITS = []


class Server(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path.startswith("/beacon"):
            HITS.append(self.path)
            self.send_response(204)
            self.end_headers()
            return
        super().do_GET()


PROBES = """<script type="module">
window.answers = {};
function probe(id, make) {
  const w = make();
  w.onmessage = (e) => { window.answers[id] = e.data; };
  w.onerror = (e) => { window.answers[id] = 'error ' + (e.message || e); };
  w.postMessage('ping');
}
probe('classic', () => new Worker('classic.js'));
probe('module', () => new Worker(new URL('./module.js', import.meta.url), { type: 'module' }));
probe('blob', () => new Worker(URL.createObjectURL(new Blob(["onmessage = () => postMessage('pong-blob')"]))));
</script>"""


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("workers")
    (root / "classic.js").write_text(
        "onmessage = () => { fetch('/beacon?from=worker').catch(() => {}); postMessage('pong-classic'); };")
    (root / "module.js").write_text("self.onmessage = () => self.postMessage('pong-module');")
    (root / "page.html").write_text("<!doctype html><title>workers</title>" + PROBES)
    (root / "frame.html").write_text("<!doctype html><title>frame</title>" + PROBES.replace(
        "w.onmessage = (e) => { window.answers[id] = e.data; };",
        "w.onmessage = (e) => { window.answers[id] = e.data; parent.postMessage([id, e.data], '*'); };"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Server, directory=str(root)))
    port = server.server_address[1]
    (root / "cross.html").write_text(
        "<!doctype html><title>outer</title><script>window.answers = {};"
        "addEventListener('message', (e) => { window.answers[e.data[0]] = e.data[1]; });</script>"
        f"<iframe src='http://{CROSS_SITE}:{port}/frame.html'></iframe>")
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield port
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    from qajev import chrome

    if why := chrome.display_missing():  # a visible Chrome: with no display the tests skip and say why
        pytest.skip(why)

    old = os.environ.get("QAJEV_CHROME_FLAGS")
    os.environ["QAJEV_CHROME_FLAGS"] = f"{old or ''} '--host-resolver-rules=MAP {CROSS_SITE} 127.0.0.1'".strip()
    try:
        record = chrome.start(f"workers-{os.getpid()}", headless=False, ephemeral=True)  # visible, never headless
    finally:
        if old is None:
            os.environ.pop("QAJEV_CHROME_FLAGS", None)
        else:
            os.environ["QAJEV_CHROME_FLAGS"] = old
    yield record
    chrome.stop(record["state_key"])


def answers_on(site, browser, page_name, mode, block_urls=()):
    from qajev import session as session_mod
    from qajev.ledger import Ledger

    port = site
    session_mod.configure_env(browser["cdp_url"])
    s = session_mod.Session(Ledger(0.0), headless=False, hosts={f"127.0.0.1:{port}"},
                            guard_opts={"block_urls": list(block_urls)})
    try:
        s.set_device({"width": 1280, "height": 900, "mobile": False, "scale": 1})
        s.arm(mode)
        assert s.navigate(f"http://127.0.0.1:{port}/{page_name}") is None
        deadline = time.monotonic() + 8
        while len(s.evaluate("window.answers || {}") or {}) < 3 and time.monotonic() < deadline:
            time.sleep(0.1)
        return s.evaluate("window.answers || {}")
    finally:
        s.close()
        session_mod.stop_daemon()


EVERY = {"classic": "pong-classic", "module": "pong-module", "blob": "pong-blob"}


@live
@pytest.mark.parametrize("mode", ["readonly", "mutate"])
def test_a_pages_workers_start_and_answer(site, browser, mode):
    assert answers_on(site, browser, "page.html", mode) == EVERY


@live
def test_the_workers_of_a_frame_on_another_site_start_and_answer(site, browser):
    assert answers_on(site, browser, "cross.html", "readonly") == EVERY


@live
def test_a_workers_request_to_a_blocked_address_never_leaves(site, browser):
    HITS.clear()
    assert answers_on(site, browser, "page.html", "readonly", block_urls=["*/beacon*"]) == EVERY
    time.sleep(0.5)
    assert HITS == []
    assert answers_on(site, browser, "page.html", "readonly") == EVERY
    deadline = time.monotonic() + 3
    while not HITS and time.monotonic() < deadline:  # planted: without the block list it does arrive
        time.sleep(0.1)
    assert HITS and HITS[0].startswith("/beacon?from=worker")
