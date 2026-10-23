"""Report evidence names and publication order, without browser or model dependencies."""

import os
import tempfile
import threading
from collections.abc import Iterable
from pathlib import Path

PUBLISH_LOCK = threading.RLock()
REPORTS = {"report.json", "report.md", "report.html"}


def relative(path: object) -> bool:
    return (
        isinstance(path, str)
        and bool(path)
        and not path.startswith("/")
        and "\\" not in path
        and ".." not in path.split("/")
        and not any(ord(c) < 32 for c in path)
        and "://" not in path
    )


def _objects(value):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, dict) for item in value):
        raise ValueError("invalid report scenario or screenshot metadata")
    return value


def ux_details(scenario: dict) -> dict | None:
    """New deterministic findings, apart from legacy UX note lists and an explicitly disabled scan."""
    value = scenario.get("ux")
    if not isinstance(value, dict) or value.get("mode") == "off":
        return None
    _objects(value.get("findings"))
    return value


def ux_crop(finding: dict) -> str | None:
    """UX crops belong to the run's UX folder, never an absolute path or an external URL."""
    path = finding.get("screenshot")
    return path if relative(path) and path.startswith("ux/") else None


def image_paths(data: dict) -> list[str]:
    """Every safe run-relative frame named by the full report, once, in stable order."""
    named = set()
    for scenario in _objects(data.get("scenarios")):
        effect = scenario.get("effect") or {}
        if not isinstance(effect, dict):
            raise ValueError("invalid report effect metadata")
        paths = [
            scenario.get("shot"),
            *(item.get("shot") for item in _objects(scenario.get("clients"))),
            *(item.get("path") for item in _objects(scenario.get("react"))),
            *(effect.get(key) for key in ("off", "on", "difference")),
        ]
        for check in _objects(scenario.get("checks")):
            evidence = check.get("evidence")
            if evidence is not None and not isinstance(evidence, dict):
                raise ValueError("invalid report claim evidence metadata")
            if evidence and check.get("evidence_type") != "js":
                paths.extend(evidence.get(key) for key in ("crop", "reference", "panel", "difference"))
        ux = ux_details(scenario)
        if ux is not None:
            paths.extend(ux_crop(finding) for finding in _objects(ux.get("findings")))
        named.update(path for path in paths if relative(path))
    return sorted(named)


def missing_images(folder: Path, data: dict) -> list[str]:
    root = Path(folder).resolve()
    missing = []
    for name in image_paths(data):
        path = root / name
        try:
            if root not in path.resolve().parents or path.is_symlink() or not path.is_file() or not path.stat().st_size:
                missing.append(name)
        except OSError:
            missing.append(name)
    return missing


def publish(folder: Path, files: Iterable[tuple[str, bytes]]) -> None:
    """Stage every byte before committing images, human reports, then the final JSON marker.

    Unique private stage names prevent writer collisions. A producer/download exception leaves the previous
    report intact. Each replacement is atomic; final JSON signals that every referenced artifact is ready.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    root = folder.resolve()
    staged, names = [], set()
    with tempfile.TemporaryDirectory(prefix=".qajev-publish-", dir=folder) as temporary:
        for name, data in files:
            if not relative(name) or name in names:
                raise ValueError("unsafe or duplicate report artifact path")
            target = folder / name
            if root not in target.resolve().parents:
                raise ValueError("report artifact leaves its folder")
            names.add(name)
            stage = Path(temporary) / f"{len(staged)}.stage"
            with stage.open("xb") as output:
                os.chmod(stage, 0o600)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            staged.append((name, stage, target))
        staged.sort(key=lambda item: 2 if item[0] == "report.json" else 1 if item[0] in REPORTS else 0)
        with PUBLISH_LOCK:
            for _, stage, target in staged:
                if root not in target.resolve().parents:
                    raise ValueError("report artifact leaves its folder")
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(stage, target)
