"""The approved test plan (José, 8 Oct: no QAJev run without a test plan the Orchestrator has approved).

A plan is a Markdown file in the project, usually tools/qa/plans/<date>-<name>.md. The approver adds a line

    Approved by the Orchestrator 8 Oct 2026 10:34 sha256:<the sha256 of the plan text above this line>

(`qajev plan-hash FILE` prints the sha256). The LAST approval line counts: it covers everything above it, earlier
approvals and additions too. A plan edited after its approval, or with text below that line, is not approved.

A suite names its plan (`plan:`, from the suite's folder or any folder above it, such as the project root); a check or
a play takes `--plan`. Without an approved plan a run warns; with QAJEV_REQUIRE_PLAN=1 it is refused before Chrome.
"""

import hashlib
import os
import re
from pathlib import Path

APPROVER = os.environ.get("QAJEV_PLAN_APPROVER") or "the Orchestrator"
NO_PLAN = "no test plan named (plan: in the suite, or --plan)"


def required():
    """QAJEV_REQUIRE_PLAN=1: a run without an approved plan is refused (exit 3) instead of warned about."""
    return os.environ.get("QAJEV_REQUIRE_PLAN", "").strip().lower() in {"1", "true", "yes", "on"}


def digest(text):
    """The sha256 of a plan's text: line ends as \\n, trailing blank lines and spaces dropped, one final newline."""
    return hashlib.sha256((text.replace("\r\n", "\n").rstrip() + "\n").encode()).hexdigest()


def project_root(base):
    """The project the suite belongs to: the nearest folder at or above `base` with a .qajev/ config or a git checkout
    (.git, a folder or a worktree's file). -> Path, or None outside any project"""
    for folder in (base, *base.parents):
        if (folder / ".qajev").is_dir() or (folder / ".git").exists():
            return folder
    return None


def resolve(value, base):
    """A plan path from a suite: absolute, or relative to the suite's folder or a folder above it up to the project
    root, never above it (a plan of another project must not count). -> Path (it may not exist)."""
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path
    base = Path(base).resolve()
    root = project_root(base)
    folders = [base, *base.parents]
    for folder in folders[:folders.index(root) + 1] if root else [base]:
        if (folder / path).is_file():
            return folder / path
    return base / path


def read(path):
    """A plan's text. -> (text, None), or (None, problem) for a file that cannot be read or is not UTF-8."""
    try:
        return Path(path).read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError) as e:
        return None, f"cannot read the plan: {e}"


def check(path):
    """-> {path, approved, approval (the line), sha256 (of the text above it), problem (None when approved)}"""
    out = {"path": str(path) if path else None, "approved": False, "approval": None, "approved_at": None,
           "sha256": None, "problem": None}
    if not path:
        return {**out, "problem": NO_PLAN}
    path = Path(path)
    if not path.is_file():
        return {**out, "problem": "no such file"}
    text, problem = read(path)
    if text is None:
        return {**out, "problem": problem}
    lines = text.replace("\r\n", "\n").split("\n")
    starts = f"Approved by {APPROVER}"
    marks = [i for i, line in enumerate(lines) if line.strip().startswith(starts)]
    if not marks:
        return {**out, "problem": f"has no line that starts “{starts}”"}
    last = marks[-1]
    line = lines[last].strip()
    above = "\n".join(lines[:last])
    when = re.match(rf"{re.escape(starts)}\s+(\d{{1,2}} \w{{3}} \d{{4}} \d{{1,2}}:\d{{2}})", line)
    out.update(approval=line, approved_at=when[1] if when else None, sha256=digest(above))
    stated = re.search(r"sha256:([0-9a-f]{64})\b", line)
    if not stated:
        return {**out, "problem": "its approval line has no sha256 of the plan (qajev plan-hash FILE prints it)"}
    if stated[1] != out["sha256"]:
        return {**out, "problem": "the plan changed after its approval (the sha256 of the text above the approval "
                                  "line is not the one approved)"}
    if any(rest.strip() for rest in lines[last + 1:]):
        return {**out, "problem": "text after its last approval line is not approved"}
    return {**out, "approved": True}


def words(status):
    """One line for a person: "approved: <line>" or "NOT APPROVED (<problem>)"."""
    if status.get("approved"):
        return f"approved: {status['approval']}"
    return f"NOT APPROVED ({status.get('problem')})"
