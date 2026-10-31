"""The imhim adapter brings a dev menu item or a settings option into view with the browser's own scrollIntoView,
never the element's own (React Native Web, 31 Oct: a page can give one element its own scroll functions). The adapter
runs in Node with a stand-in page, as it runs in the game's page."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ADAPTER = Path(__file__).resolve().parent.parent / "qajev" / "bridges" / "web" / "adapters" / "imhim.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="needs node to run the adapter")

PAGE = """
const calls = [];
global.window = global;
global.Element = class Element {};
Element.prototype.scrollIntoView = function () { calls.push('browser scrollIntoView'); };
function control(text, extra) {
  const el = Object.create(Element.prototype);
  return Object.assign(el, { textContent: text, innerText: text, checkVisibility: () => true, closest: () => null,
    getAttribute: () => null, click: () => calls.push('click ' + text),
    scrollIntoView: () => calls.push('own scrollIntoView') }, extra || {});
}
const radio = control('Off');
const button = control('Skip level');
const section = { querySelector: () => ({ textContent: 'Levels' }),
                  querySelectorAll: (q) => (q === 'button' ? [button] : []) };
global.document = { querySelector: () => null, getElementById: () => null, body: null,
  documentElement: { classList: { contains: () => false } },
  querySelectorAll: (q) => (q.includes('[role="radio"]') ? [radio] : q === '.devmenu section' ? [section] : []) };
global.localStorage = { getItem: () => null };
global.location = { href: 'http://localhost/' };
require(%s);
"""


def _calls(action):
    assert NODE
    code = PAGE % json.dumps(str(ADAPTER)) + (
        f"const out = window.__qajevAdapter.act({json.dumps(action)});\n"
        "console.log(JSON.stringify({out, calls}));")
    run = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.parametrize("action,clicked", [({"op": "set_option", "match": "Off"}, "click Off"),
                                            ({"op": "dev_item", "match": "Levels: Skip level"}, "click Skip level")],
                         ids=["settings option", "dev menu button"])
def test_the_adapter_scrolls_with_the_browsers_scrollintoview(action, clicked):
    got = _calls(action)
    assert got == {"out": {"ok": True}, "calls": ["browser scrollIntoView", clicked]}, got
