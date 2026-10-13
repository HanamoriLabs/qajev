"""Where QAJev's settings come from (Foley1, 6 Oct): a project's own .env held a Cloudflare token for its deploys, QAJev
read it before ~/.qajev/.env, and Clef answered 401. QAJev reads its own files only, its own keys come from them, and
`qajev doctor` names where each came from, never a value."""

import os

import pytest

from qajev import config


@pytest.fixture
def places(tmp_path, monkeypatch):
    home, repo = tmp_path / "home", tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.chdir(repo)
    for key in [*config.OWN_KEYS, "QAJEV_ENV_FILE", "PROJECT_ONLY", "QAJEV_RUNNER", "QAJEV_CLOUD_URL",
                *(f"QAJEV_{k}" for k in config.OWN_KEYS)]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "_FROM_FILES", {})
    return home, repo


def test_a_projects_own_env_file_is_never_read(places):
    home, repo = places
    (repo / ".env").write_text("CLOUDFLARE_API_TOKEN=project-deploy\nPROJECT_ONLY=1\n")
    (home / ".env").write_text("CLOUDFLARE_API_TOKEN=for-clef\n")
    loaded = config.load_env()
    assert os.environ["CLOUDFLARE_API_TOKEN"] == "for-clef"
    assert "PROJECT_ONLY" not in os.environ and str(repo / ".env") not in loaded


def test_qajevs_own_keys_come_from_its_file_over_the_shell_unless_pinned(places, monkeypatch):
    home, _ = places
    (home / ".env").write_text("OPENROUTER_API_KEY=from-qajev-file\nCLOUDFLARE_ACCOUNT_ID=acct-file\n")
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-shell")
    monkeypatch.setenv("QAJEV_CLOUDFLARE_ACCOUNT_ID", "acct-pinned")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ci-only")  # no file sets it: the shell's value stays (CI)
    config.load_env()
    assert os.environ["OPENROUTER_API_KEY"] == "from-qajev-file"
    assert os.environ["CLOUDFLARE_ACCOUNT_ID"] == "acct-pinned"
    assert os.environ["TYPESAFE_API_KEY"] == "ci-only"
    said = "\n".join(config.key_sources())
    assert "OPENROUTER_API_KEY: " + str(home / ".env") in said
    assert "the shell's OPENROUTER_API_KEY is not used" in said
    assert "CLOUDFLARE_ACCOUNT_ID: QAJEV_CLOUDFLARE_ACCOUNT_ID" in said
    assert "TYPESAFE_API_KEY: the shell" in said
    assert not any(v in said for v in ("from-qajev-file", "from-shell", "acct-pinned", "ci-only"))


def test_a_refused_model_key_points_at_where_it_came_from():
    from qajev import verdict

    _, reason = verdict.classify("model_error", [], has_checks=False,
                                 stop_detail="Model provider returned HTTP 401; no action executed.")
    assert "HTTP 401" in reason and "qajev doctor" in reason
    _, other = verdict.classify("model_error", [], has_checks=False, stop_detail="timed out after 30 s")
    assert "qajev doctor" not in other


def test_an_env_file_named_on_purpose_is_still_read(places, tmp_path):
    named = tmp_path / "ci.env"
    named.write_text("TYPESAFE_API_KEY=from-named\n")
    assert str(named) in config.load_env(str(named))
    assert os.environ["TYPESAFE_API_KEY"] == "from-named"


def test_a_long_lived_process_follows_an_edited_env_file(places):
    home, _ = places
    env = home / ".env"
    env.write_text("QAJEV_RUNNER=cloud\nOPENROUTER_API_KEY=key-one\n")
    config.load_env()
    assert os.environ["QAJEV_RUNNER"] == "cloud"
    env.write_text("QAJEV_RUNNER=local\nQAJEV_CLOUD_URL=http://cloud-off.invalid\nOPENROUTER_API_KEY=key-two\n")
    config.reload_env()
    assert os.environ["QAJEV_RUNNER"] == "local"  # `qajev mode local` reaches an MCP server started before it
    assert os.environ["QAJEV_CLOUD_URL"] == "http://cloud-off.invalid"
    assert os.environ["OPENROUTER_API_KEY"] == "key-two"
    env.write_text("OPENROUTER_API_KEY=key-two\n")
    config.reload_env()
    assert "QAJEV_RUNNER" not in os.environ and "QAJEV_CLOUD_URL" not in os.environ  # a removed line is gone


def test_a_value_the_shell_or_the_process_set_is_never_taken_back(places, monkeypatch):
    home, _ = places
    env = home / ".env"
    env.write_text("QAJEV_RUNNER=cloud\nQAJEV_CLOUD_URL=https://cloud.qajev.com\n")
    monkeypatch.setenv("QAJEV_RUNNER", "auto")  # the shell's: it wins over the file, now and after a reload
    config.load_env()
    os.environ["QAJEV_CLOUD_URL"] = "https://staging.example"  # the process changed it after loading
    env.write_text("QAJEV_RUNNER=local\nQAJEV_CLOUD_URL=http://cloud-off.invalid\n")
    config.reload_env()
    assert os.environ["QAJEV_RUNNER"] == "auto" and os.environ["QAJEV_CLOUD_URL"] == "https://staging.example"


@pytest.mark.parametrize("call", ["dispatch", "run_report"])
def test_every_mcp_run_reads_the_env_file_again_before_it_decides_anything(monkeypatch, call):
    import asyncio

    from qajev import cloud, jobs, mcp_server

    seen = []

    class Stop(Exception):
        pass

    def decided(*a, **k):  # where the run goes (dispatch) or the job starting (run_report): the first decision
        seen.append("decided")
        raise Stop

    monkeypatch.setattr(config, "reload_env", lambda: seen.append("reload"))
    monkeypatch.setattr(cloud, "runner_for", decided)
    monkeypatch.setattr(jobs, "start", decided)
    run = (mcp_server._dispatch(["check", "https://example.com/"], [], None, True) if call == "dispatch"
           else mcp_server._run_report(["check", "https://example.com/"], None, background=True))
    with pytest.raises(Stop):
        asyncio.run(run)
    assert seen == ["reload", "decided"]
