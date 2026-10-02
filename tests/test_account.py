import pytest

from qajev import account, vault
from qajev import project as P

PROJECT = """name = "shop"
default_env = "local"

[env.local]
base_url = "http://127.0.0.1:8765"
hosts = ["cdn.shop.test"]

[[objective]]
name = "orders"
url = "/orders"
expect = { text = ["Your orders"] }
"""


def test_an_account_added_to_a_project_holds_references_and_signs_its_runs_in(tmp_path):
    path = tmp_path / "shop.toml"
    path.write_text(PROJECT)
    proj = account.add_to_project(path, "shop-tester", "qa+shop@example.com", "keychain:qajev/shop-tester",
                                  "/login", default=True)
    assert proj.account == "shop-tester"  # the top-level key went above the first table, where TOML needs it
    acct, hosts = account.project_account(proj, "shop-tester")
    assert acct["login"]["url"] == "http://127.0.0.1:8765/login" and {"127.0.0.1:8765", "cdn.shop.test"} <= hosts
    # Every run of the project signs in with it, by reference.
    data = P.suite_data(P.load(str(path)))
    assert data["account"] == {"name": "shop-tester", "email": "qa+shop@example.com",
                               "password": "keychain:qajev/shop-tester", "login": {"url": "/login"}}


def test_a_project_account_never_holds_a_password_value_and_names_stay_unique(tmp_path):
    path = tmp_path / "shop.toml"
    path.write_text(PROJECT)
    with pytest.raises(P.ProjectError, match="never values"):
        account.add_to_project(path, "shop-tester", "qa+shop@example.com", "hunter2-plain", "/login")
    assert path.read_text() == PROJECT  # restored: a rejected account leaves no trace
    account.add_to_project(path, "shop-tester", "qa+shop@example.com", "env:SHOP_PASS", "/login")
    with pytest.raises(account.AccountError, match="already exists"):
        account.add_to_project(path, "shop-tester", "other@example.com", "env:OTHER", "/login")


def test_older_email_env_and_password_env_become_references():
    proj = P.Project(name="shop", config_path=None, repo=None, in_repo=False, default_env="local",  # type: ignore[arg-type]
                     envs={"local": {"base_url": "https://shop.test"}}, budget={}, guard={}, persona=None,
                     device=None, accounts={"old": {"email_env": "SHOP_USER", "password_env": "SHOP_PASS",
                                                    "login": {"url": "/login"}}})
    assert P.sign_in_account(proj, "old") == {"name": "old", "email": "env:SHOP_USER", "password": "env:SHOP_PASS",
                                              "login": {"url": "/login"}}


def test_the_keychain_prompt_needs_the_persons_own_terminal(monkeypatch):
    def unreadable(ref):
        raise vault.VaultError("no such item")

    monkeypatch.setattr(vault, "resolve", unreadable)
    with pytest.raises(account.AccountError, match="your own terminal"):
        account.save_password("keychain:qajev/shop-tester", interactive=False)
    monkeypatch.setattr(vault, "resolve", lambda ref: "x" * 12)
    assert account.save_password("keychain:qajev/shop-tester", interactive=False) == "kept"  # never re-asked
    assert account.save_password("env:SHOP_PASS") == "found"


def test_a_suite_block_signs_in_over_https_only():
    assert account.block("t", "qa@example.com", "env:P", "https://shop.test/login")["login"] == {
        "url": "https://shop.test/login"}
    with pytest.raises(account.AccountError, match="https"):
        account.block("t", "qa@example.com", "env:P", "http://shop.test/login")
    with pytest.raises(account.AccountError, match="password"):
        account.block("t", "qa@example.com", "plain-value", "https://shop.test/login")
