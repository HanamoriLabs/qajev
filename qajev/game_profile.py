"""A game's kept TEST profile: its save folder, kept between QAJev runs on purpose (SideGame1, 6 Oct: a save made in
one plan is still there when the next starts; long plans run in chunks).

The default stays a throwaway, deleted at close. A kept profile is allowed in two places only, never anywhere else
(the 30 Sep games rule: never a real or Steam-synced save):
  ~/.qajev/game-profiles/<name>                <name>: ^[a-z0-9][a-z0-9-]{0,39}$
  a git-ignored folder inside the game's repo  (git check-ignore says so: no save is ever committed)
Checked before anything is created: no "..", nothing under ~/Library or an Application Support folder, no symlink
anywhere on the path, and a folder that already holds files only when QAJev marked it (MARKER, written on first use):
a git-ignored node_modules/left-pad is never used or emptied. Then created, and its real path checked again.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import HOME

ROOT = HOME / "game-profiles"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
MARKER = ".qajev-game-profile"  # in every folder QAJev keeps as a profile: only those are used and reset


class ProfileError(ValueError):
    """A kept profile QAJev refuses, with why."""


def named(name):
    """~/.qajev/game-profiles/<name>, checked and created. -> its real path."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ProfileError(f"a game profile name is lowercase letters, digits and -, up to 40 (got {name!r})")
    return check(ROOT / name, None)


def _repo(project):
    """The game's repo (its git top level), or None."""
    if project is None:
        return None
    here = Path(project).expanduser()
    here = here if here.is_dir() else here.parent
    try:
        out = subprocess.run(["git", "-C", str(here), "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                             timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def _under(path, root):
    return path == root or root in path.parents


def _no_symlinks(path):
    for part in [*reversed(path.parents), path]:
        if part.is_symlink():
            raise ProfileError(f"refused: {part} is a symlink (a kept profile's path has none)")


def _marked(path):
    mark = path / MARKER
    return mark.is_file() and not mark.is_symlink()


def check(folder, project):
    """A kept profile's folder, allowed only under ROOT or as a git-ignored folder in the game's repo (`project` is
    the game), and only empty or marked as QAJev's. -> its real path, created and marked. Raises ProfileError, before
    creating anything, when it is not allowed."""
    raw = Path(os.path.expanduser(str(folder)))
    if not raw.is_absolute() or ".." in raw.parts or "\0" in str(raw):
        raise ProfileError(f"refused: a kept profile is an absolute path without '..' (got {folder})")
    path = Path(os.path.normpath(raw))
    home = Path(os.path.normpath(Path.home()))
    if _under(path, home / "Library") or any(p.lower() == "application support" for p in path.parts):
        raise ProfileError(f"refused: {path} is where real saves live (Steam syncs them): never a test profile")
    root, repo = Path(os.path.normpath(ROOT)), _repo(project)
    if _under(path, root) and path != root:
        allowed = root
    elif repo is not None and _under(path, repo) and path != repo:
        allowed = repo
        rel = path.relative_to(repo)
        ignored = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--", str(rel)], timeout=10)
        if ignored.returncode != 0:
            raise ProfileError(f"refused: {rel} is not git-ignored in {repo} (a test save must never be committed); "
                               "ignore it in .gitignore, or use --game-profile NAME")
    else:
        raise ProfileError(f"refused: {path}: a kept profile lives under {ROOT}/<name> or in a git-ignored folder "
                           "of the game's own repo")
    _no_symlinks(path)
    if path.is_dir() and any(path.iterdir()) and not _marked(path):
        raise ProfileError(f"refused: {path} already holds files and is not QAJev's (no {MARKER}): QAJev never "
                           "uses or empties a folder it did not make; name an empty or new folder")
    path.mkdir(parents=True, exist_ok=True)
    real = Path(os.path.realpath(path))
    if not _under(real, Path(os.path.realpath(allowed))) or real == Path(os.path.realpath(allowed)):
        raise ProfileError(f"refused: {path} resolves to {real}, outside {allowed}")
    _no_symlinks(real)
    if not _marked(real):
        (real / MARKER).write_text("A game test profile QAJev keeps between runs. QAJev may empty this folder.\n")
    return real


def reset(folder, project):
    """Empty a kept profile, after the same checks: only ever a marked folder inside the allowed places, and it stays
    marked. -> its real path."""
    real = check(folder, project)
    for child in real.iterdir():
        if child.name == MARKER:
            continue
        if child.is_symlink() or not child.is_dir():
            child.unlink()
        else:
            shutil.rmtree(child)
    return real
