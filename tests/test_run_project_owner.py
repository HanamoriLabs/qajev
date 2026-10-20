"""Ownership is decided on the submitting computer, before paths are shortened for sync."""

import json
import sys

import pytest

from qajev import jobs, project


@pytest.fixture
def repos(tmp_path, monkeypatch):
    monkeypatch.setattr(project, "listing", lambda: [])
    for name in ("outer", "cwd"):
        root = tmp_path / name
        (root / ".qajev").mkdir(parents=True)
        (root / ".qajev/project.toml").write_text(
            f'name = "{name}"\n[env.dev]\nbase_url = "https://{name}.example"\n')
    nested = tmp_path / "outer/nested"
    (nested / ".qajev").mkdir(parents=True)
    (nested / ".qajev/project.toml").write_text('name = "nearest"\n[env.dev]\nbase_url = "https://site.example"\n')
    suite = nested / "qa/test.yaml"
    suite.parent.mkdir()
    suite.write_text('base_url: https://site.example\nscenarios: []\n')
    monkeypatch.chdir(tmp_path / "cwd")
    return tmp_path, suite


def test_foreground_job_keeps_nearest_suite_project_over_working_folder(repos):
    _, suite = repos
    folder = jobs.register(["run", "--jobs", "1", str(suite)])
    assert json.loads((folder / "job.json").read_text())["project"] == "nearest"


def test_background_job_uses_its_explicit_cwd_not_the_callers(repos):
    root, _ = repos
    argv = ["run", "--jobs", "1", "nested/qa/test.yaml"]
    job = jobs.start(argv, cwd=str(root / "outer"), command=[sys.executable, "-c", "pass"])
    meta = json.loads((jobs.JOBS / job["id"] / "job.json").read_text())
    assert meta["project"] == "nearest"
    if child := jobs._children.get(job["id"]):
        child.wait(timeout=10)  # status() may already have reaped this very short fixture child


def test_foreground_check_prefers_cwd_project_to_unrelated_host(repos):
    folder = jobs.register(["check", "https://site.example", "--expect-text", "ready"])
    assert json.loads((folder / "job.json").read_text())["project"] == "cwd"


def test_project_flag_is_recorded_as_name_instead_of_config_path(repos):
    root, _ = repos
    folder = jobs.register(["run", "--project", str(root / "outer")])
    assert json.loads((folder / "job.json").read_text())["project"] == "outer"


def test_local_host_fallback_is_only_used_without_a_repo_owner(repos, monkeypatch):
    root, _ = repos
    outside = root / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    folder = jobs.register(["check", "https://www.site.example:443/path?ticket=private#fragment"])
    meta = json.loads((folder / "job.json").read_text())
    assert meta["project"] == "site.example"
    assert "ticket" not in meta["project"]


def test_local_suite_without_a_base_url_uses_its_page_host(repos, monkeypatch):
    root, _ = repos
    outside = root / "outside"
    outside.mkdir()
    path = outside / "standalone.yaml"
    path.write_text('hosts: [cdn.other.example]\nscenarios:\n  - url: https://page.other.example/cart\n')
    monkeypatch.chdir(outside)
    folder = jobs.register(["run", str(path)])
    assert json.loads((folder / "job.json").read_text())["project"] == "page.other.example"
