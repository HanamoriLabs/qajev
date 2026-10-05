"""A game's kept TEST profile (SideGame1, 6 Oct: a save from one plan still there in the next). Only a QAJev-owned
folder or a git-ignored folder inside the game's repo; never a real or Steam-synced save (the 30 Sep games rule)."""

import subprocess

import pytest

from qajev import game_profile as gp


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake home: ~/Library and ~/.qajev live in tmp, so no test can touch the real ones."""
    fake = tmp_path / "home"
    (fake / "Library" / "Application Support" / "ImHim").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setattr(gp, "ROOT", fake / ".qajev" / "game-profiles")
    return fake


@pytest.fixture
def game(tmp_path):
    """A game repo with a git-ignored qa-work folder, as the sidescroller has."""
    repo = tmp_path / "game"
    (repo / "desktop").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("qa-work/\n")
    return repo


def test_a_named_profile_lives_under_qajevs_own_folder(home):
    folder = gp.named("plan-71")
    assert folder == (home / ".qajev" / "game-profiles" / "plan-71").resolve() and folder.is_dir()
    for bad in ("../x", "A", "a/b", "", "-x", "x" * 41, "a.b"):
        with pytest.raises(gp.ProfileError, match="name"):
            gp.named(bad)


def test_a_folder_in_the_games_repo_counts_only_when_git_ignores_it(home, game):
    kept = gp.check(game / "qa-work" / "profiles" / "plan-71", game / "desktop")
    assert kept.is_dir() and kept == (game / "qa-work" / "profiles" / "plan-71").resolve()
    with pytest.raises(gp.ProfileError, match="git-ignored"):
        gp.check(game / "saves" / "plan-71", game / "desktop")  # committed saves: refused
    assert not (game / "saves").exists()


def test_a_real_or_steam_synced_save_is_refused_and_never_created(home, game):
    real = home / "Library" / "Application Support" / "ImHim"
    for folder in (real, real / "qa", home / "Documents" / "saves"):
        with pytest.raises(gp.ProfileError):
            gp.check(folder, game / "desktop")
    assert list(real.iterdir()) == [] and not (home / "Documents").exists()


def test_a_symlink_cannot_point_the_profile_somewhere_else(home, game):
    (game / "qa-work").mkdir()
    (game / "qa-work" / "profiles").symlink_to(home / "Library" / "Application Support" / "ImHim")
    with pytest.raises(gp.ProfileError, match="symlink"):
        gp.check(game / "qa-work" / "profiles" / "plan-71", game / "desktop")
    assert list((home / "Library" / "Application Support" / "ImHim").iterdir()) == []


def test_a_dead_runs_kept_profile_outlives_the_reaper_and_its_throwaway_does_not(tmp_path, monkeypatch):
    import json

    from qajev import native

    monkeypatch.setattr(native, "STATE", tmp_path / "native")
    native.STATE.mkdir()
    for name, kept in (("kept", True), ("throwaway", False)):
        dead = subprocess.Popen(["true"])  # a game and its run, both finished: the reaper forgets it, kills nothing
        dead.wait()
        folder = tmp_path / name
        folder.mkdir()
        (folder / "save.json").write_text("{}")
        (native.STATE / f"{dead.pid}.json").write_text(json.dumps(
            {"pid": dead.pid, "owner_pid": dead.pid, "project": str(tmp_path),
             "user_dir": str(folder), "profile": {"folder": str(folder), "kept": kept}}))
    native.reap()
    assert (tmp_path / "kept" / "save.json").is_file() and not (tmp_path / "throwaway").exists()


def test_reset_empties_only_an_allowed_folder(home, game):
    folder = gp.named("plan-71")
    (folder / "save.json").write_text("{}")
    gp.reset(folder, game / "desktop")
    assert folder.is_dir() and list(folder.iterdir()) == []
    with pytest.raises(gp.ProfileError):
        gp.reset(home / "Library" / "Application Support" / "ImHim", game / "desktop")
