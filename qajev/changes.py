"""What changed since the previous run of the same project, environment and kind (objectives or smoke).

Only scenarios present in both runs are compared, so running a subset (--name) does not read as "gone".
Findings are matched by scenario, kind and page (not by detail: timings and counts vary from run to run).
"""

import json
from pathlib import Path
from urllib.parse import urlsplit

BAD = {"fail", "stuck"}


def kind_of(report):
    return "smoke" if report.get("smoke") else "objectives"


def _row_kind(row):
    return row.get("kind") or ("smoke" if "-smoke-" in Path(row["report"]).parent.name else "objectives")


def _where(url):
    parts = urlsplit(url or "")
    return f"{parts.netloc}{parts.path}"


def _key(f):
    return f.get("scenario"), f.get("kind"), _where(f.get("url"))


def previous(rows, *, project, env, kind, exclude=None):
    """The last finished (not interrupted) report of this project/env/kind, from index rows newest first."""
    for row in rows:
        if row.get("project") != project or row.get("env") != env or _row_kind(row) != kind:
            continue
        folder = Path(row["report"]).parent
        if exclude and folder == Path(exclude):
            continue
        try:
            data = json.loads((folder / "report.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("interrupted") or data.get("partial"):
            continue
        return data
    return None


def diff(prev, now):
    was = {s["name"]: s for s in prev.get("scenarios") or []}
    cur = {s["name"]: s for s in now.get("scenarios") or []}
    common = [n for n in cur if n in was]
    newly_failing, fixed, changed = [], [], []
    for name in common:
        a, b = was[name]["outcome"], cur[name]["outcome"]
        if a == b:
            continue
        item = {"name": name, "was": a, "now": b, "reason": cur[name].get("reason")}
        if b in BAD and a not in BAD:
            newly_failing.append(item)
        elif b == "pass":
            fixed.append(item)
        else:
            changed.append(item)
    names = set(common)
    before = {_key(f): f for f in prev.get("findings") or [] if f.get("scenario") in names}
    after = {_key(f): f for f in now.get("findings") or [] if f.get("scenario") in names}
    new_findings = [after[k] for k in after if k not in before]
    gone_findings = [before[k] for k in before if k not in after]
    parts = [f"{len(x)} {label}" for x, label in ((newly_failing, "newly failing"), (fixed, "fixed"),
                                                  (changed, "other outcome change(s)"),
                                                  (new_findings, "new finding(s)"),
                                                  (gone_findings, "finding(s) gone")) if x]
    out = {
        "since": {"run_dir": prev.get("run_dir"), "started_at": prev.get("started_at"), "gate": prev.get("gate")},
        "changed": bool(parts) or prev.get("gate") != now.get("gate"),
        "summary": ", ".join(parts) or "no changes",
        "newly_failing": newly_failing, "fixed": fixed, "other": changed,
        "new_findings": new_findings, "gone_findings": gone_findings,
    }
    if prev.get("gate") != now.get("gate"):
        out["gate"] = {"was": prev.get("gate"), "now": now.get("gate")}
        if not parts:
            out["summary"] = f"gate {prev.get('gate')} -> {now.get('gate')}"
    return out


def compare(project, env, report, rows):
    """-> the changes block for `report`, or None on a first run (nothing to compare with) or an interrupted one."""
    if report.get("interrupted"):
        return None
    prev = previous(rows, project=project, env=env, kind=kind_of(report), exclude=report.get("run_dir"))
    return diff(prev, report) if prev else None
