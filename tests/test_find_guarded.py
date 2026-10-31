"""A hook's selector whose first match is a control the guard hid says so plainly (dash-wholesale-links, 13 Oct: a Save
click matched "Sign out" in the sidebar first, and "the guard hid this control (danger: Sign out)" read as if the test
had tried to sign out). QAJev never clicks a later match on its own: the suite must say which control it means."""

import json
import shutil
import subprocess

import pytest

from qajev.session import FIND_JS


def find(elements, selector="form button"):
    """FIND_JS run in node over a page whose querySelectorAll(selector) gives `elements` (dicts: guard, id)."""
    page = json.dumps(elements)
    script = (f"const els = {page}.map((e) => ({{ dataset: e.guard ? {{ qajevGuard: e.guard }} : {{}}, id: e.id, "
              "tagName: 'BUTTON', classList: [], matches: () => false, scrollIntoView() {}, "
              "getBoundingClientRect: () => ({ x: 0, y: 0, width: 10, height: 10 }), contains: () => false }));"
              "globalThis.Element = class Element { scrollIntoView() {} };"
              "globalThis.document = { querySelector: () => els[0] || null, querySelectorAll: () => els, "
              "elementFromPoint: () => els[0] };"
              f"console.log(JSON.stringify({FIND_JS.substitute(selector=json.dumps(selector))}));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    return json.loads(out.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="needs node to run the page-side JavaScript")
def test_a_selector_that_matches_a_guarded_control_first_says_how_many_it_matches():
    got = find([{"guard": "danger: Sign out", "id": "out"}, {"id": "save"}])
    assert got["error"].startswith("refused: the guard hid this control (danger: Sign out)")
    assert "your selector matches 2 elements and this one comes first" in got["error"]
    assert "make it match only the control you mean" in got["error"]


@pytest.mark.skipif(not shutil.which("node"), reason="needs node to run the page-side JavaScript")
def test_a_selector_that_matches_only_the_guarded_control_says_only_that():
    assert find([{"guard": "danger: Sign out", "id": "out"}]) == {
        "error": "refused: the guard hid this control (danger: Sign out)"}
    assert "error" not in find([{"id": "save"}])  # an ordinary control is found as before
