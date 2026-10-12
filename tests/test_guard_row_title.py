"""A to-do row's own title is not an action (Foley, 13 Oct: the task "Buy paint" drew blank because the guard hid its
title button for the danger word "buy"). Only the one control whose visible text equals the row's checkbox label is
shown, unless it reads as a purchase; every other control in the row keeps the usual rules (the Orchestrator)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from qajev import guard

GUARD_JS = (Path(guard.__file__).parent / "js" / "guard.js").read_text()


def row_title(cases):
    """The page's own rowTitle, cut from guard.js and run in node, with the real purchase patterns."""
    squash = re.search(r"^  const squash = .*$", GUARD_JS, re.M).group(0)
    func = re.search(r"^  const rowTitle = [\s\S]*?^  };$", GUARD_JS, re.M).group(0)
    cfg = guard.build_config(mode="mutate", hosts=["localhost:5284"])
    purchase = json.dumps("|".join(cfg["purchase"]))
    script = (f"{squash}\n{func}\nconst purchase = new RegExp({purchase}, 'i');\n"
              f"console.log(JSON.stringify({json.dumps(cases)}.map(([t, toggles]) => rowTitle(t, toggles, purchase))));")
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30).stdout)


def hidden_by_usual_rules(label):
    cfg = guard.build_config(mode="mutate", hosts=["localhost:5284"])
    return any(re.search(p, label, re.I) for p in cfg["deny"] + cfg["destructive"])


@pytest.mark.skipif(not shutil.which("node"), reason="needs node to run the page-side JavaScript")
def test_a_rows_own_title_is_shown_and_its_other_controls_keep_the_usual_rules():
    assert row_title([
        ["Buy paint", ["Toggle Buy paint"]],              # the row's title: shown
        ["Delete", ["Toggle Buy paint"]],                 # the same row's Delete button: usual rules
        ["Delete old files", ["Toggle Delete old files"]],  # a title that holds a destructive word: shown
        ["Delete", ["Toggle Delete old files"]],          # that row's real delete control: usual rules
    ]) == [True, False, True, False]
    assert hidden_by_usual_rules("Delete")  # and the usual rules hide it


@pytest.mark.skipif(not shutil.which("node"), reason="needs node to run the page-side JavaScript")
def test_a_purchase_stays_hidden_in_a_row_and_out_of_one():
    assert row_title([
        ["Buy credits", []],                                       # a button, in no to-do row
        ["Buy now", []],                                           # in a list item without a checkbox
        ["Buy 500 credits $10", ["Toggle Buy 500 credits $10"]],   # a purchase, even as a row's title
    ]) == [False, False, False]
    assert all(hidden_by_usual_rules(x) for x in ("Buy credits", "Buy now", "Buy 500 credits $10"))
