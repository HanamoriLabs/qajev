"""Drag-and-drop hints are not destructive (Foley, 13 Oct: each project button says "Right-click for options; drag to
move. Drop a task here to move it into this project.", and the word "Drop" hid every project from Jev and the checks).
Only drag-and-drop phrasing is set aside: "Drop table", "Drop all" and the like stay hidden (the Orchestrator)."""

import json
import shutil
import subprocess

import pytest

from qajev import guard

# The page's own test, as qajev/js/guard.js runs it: harmless phrases are blanked first, unless a grave word is there.
DESTROYS = """
const cfg = %s;
const re = (list) => (list && list.length ? new RegExp(list.join('|'), 'i') : null);
const destructive = re(cfg.destructive), grave = re(cfg.grave);
const harmless = cfg.harmless && cfg.harmless.length ? new RegExp(cfg.harmless.join('|'), 'gi') : null;
const destroys = (text) => {
  const rest = harmless && !(grave && grave.test(text)) ? text.replace(harmless, ' ') : text;
  return destructive.test(rest);
};
console.log(JSON.stringify(%s.map(destroys)));
"""


def destroys(*labels):
    cfg = guard.build_config(mode="mutate", hosts=["localhost:5284"])
    script = DESTROYS % (json.dumps(cfg), json.dumps(list(labels)))
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30).stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="needs node to run the page-side JavaScript")
def test_drag_and_drop_hints_are_not_destructive():
    assert destroys(
        "Shared test 1 Right-click for options; drag to move. Drop a task here to move it into this project.",
        "Drag and drop your files",
        "Drag & drop to reorder",
        "Drop files here to upload",
        "Drag to reorder",
    ) == [False] * 5


@pytest.mark.skipif(not shutil.which("node"), reason="needs node to run the page-side JavaScript")
def test_a_drop_that_destroys_stays_hidden():
    assert destroys(
        "Drop table",
        "Drop all",
        "Drop database",
        "Drop the users table",
        "Drag to move. Delete project",  # another destructive word on the same control still counts
        "Drop your account data here",  # a grave word keeps it hidden, drag-and-drop phrasing or not
    ) == [True] * 6
