"""A to-do row's own title is not an action (Foley, 13 Oct: the task "Buy paint" drew blank because the guard hid its
title button for the danger word "buy"). Only the one control whose every name equals the row's checkbox label is
shown, unless it reads as a purchase; off a local dev host a destructive title stays hidden; every other control in the
row keeps the usual rules (the Orchestrator; redteam on #123)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from qajev import guard

GUARD_JS = (Path(guard.__file__).parent / "js" / "guard.js").read_text()


def row_title(cases):
    """The page's own rowTitle, cut from guard.js and run in node with the real patterns. A case is
    [names (text, aria-label, title), toggles, is_local, has aria-labelledby, its children's alt/aria-label/title]."""
    squash = re.search(r"^  const squash = .*$", GUARD_JS, re.M).group(0)
    func = re.search(r"^  const rowTitle = [\s\S]*?^  };$", GUARD_JS, re.M).group(0)
    cfg = guard.build_config(mode="mutate", hosts=["localhost:5284"])
    purchase = json.dumps("|".join(cfg["purchase"]))
    destructive = json.dumps("|".join(cfg["destructive"]))
    deny = json.dumps("|".join(cfg["deny"]))
    script = (f"{squash}\n{func}\nconst purchase = new RegExp({purchase}, 'i');\n"
              f"const destructive = new RegExp({destructive}, 'i');\nconst destroys = (t) => destructive.test(t);\n"
              f"const denyRe = new RegExp({deny}, 'i');\n"
              f"const cases = {json.dumps(cases)};\n"
              "const shown = cases.map(([n, toggles, local, labelledby = false, inner = '']) => rowTitle(n, toggles,\n"
              "  { purchase, destroys, deny: (s) => denyRe.test(s), local, labelledby, inner }));\n"
              "console.log(JSON.stringify(shown));")
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30).stdout)


def hidden_by_usual_rules(label):
    cfg = guard.build_config(mode="mutate", hosts=["localhost:5284"])
    return any(re.search(p, label, re.I) for p in cfg["deny"] + cfg["destructive"])


NODE = pytest.mark.skipif(not shutil.which("node"), reason="needs node to run the page-side JavaScript")


@NODE
def test_a_rows_own_title_is_shown_and_its_other_controls_keep_the_usual_rules():
    assert row_title([
        [["Buy paint", None, None], ["Toggle Buy paint"], True],                 # the row's title: shown
        [["Delete", None, None], ["Toggle Buy paint"], True],                    # the same row's Delete: usual rules
        [["Delete old files", None, None], ["Toggle Delete old files"], True],   # a destructive title, local: shown
        [["Delete", None, None], ["Toggle Delete old files"], True],             # that row's real delete: usual rules
    ]) == [True, False, True, False]
    assert hidden_by_usual_rules("Delete")


@NODE
def test_a_page_cannot_dress_a_dangerous_control_as_a_row_title():
    # redteam, #123: <li><input type=checkbox aria-label="Toggle Buy paint"><button aria-label="Delete account">
    # Buy paint</button></li> showed Jev a button named "Delete account".
    assert row_title([
        [["Buy paint", "Delete account", None], ["Toggle Buy paint"], True],
        [["Buy paint", None, "Delete account"], ["Toggle Buy paint"], True],
        [["Buy paint", "Buy paint", "Buy paint"], ["Toggle Buy paint"], True],  # every name agrees: shown
        # redteam, second look: the browser names <button aria-labelledby="x">Buy paint</button> from #x, and
        # <button><img alt="Delete account">Buy paint</button> "Delete account Buy paint".
        [["Buy paint", None, None], ["Toggle Buy paint"], True, True, ""],
        [["Buy paint", None, None], ["Toggle Buy paint"], True, False, "Delete account "],
        [["Buy paint", None, None], ["Toggle Buy paint"], True, False, "Sign out"],
        [["Buy paint", None, None], ["Toggle Buy paint"], True, False, "Normal priority"],  # Foley's priority dot
    ]) == [False, False, True, False, False, False, True]


@NODE
def test_off_a_local_dev_host_a_destructive_title_stays_hidden():
    assert row_title([
        [["Delete old files", None, None], ["Toggle Delete old files"], False],
        [["Buy paint", None, None], ["Toggle Buy paint"], False],  # not destructive: shown on production too
    ]) == [False, True]


@NODE
def test_a_purchase_stays_hidden_in_a_row_and_out_of_one():
    assert row_title([[[t, None, None], [] if i < 2 else [f"Toggle {t}"], True] for i, t in enumerate([
        "Buy credits", "Buy now", "Buy 500 credits $10", "Checkout", "Add to cart", "Subscribe", "Upgrade to Pro",
        "Top up 100", "Donate", "Start your free trial", "Place order", "Pay 10 EUR", "49,99 €", "R$ 20",
        "CHF 15", "₹ 499",
    ])]) == [False] * 16
    assert all(hidden_by_usual_rules(x) for x in ("Buy credits", "Buy now", "Buy 500 credits $10"))
