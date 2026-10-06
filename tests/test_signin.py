import base64
import json

import pytest

from qajev import signin, vault
from qajev.suite import SuiteError, _account

# RFC 6238 appendix B: the SHA1 test key is the ASCII "12345678901234567890"; authenticators take it in base32.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


def test_the_one_time_code_matches_rfc_6238():
    # The RFC's 8-digit values, cut to the 6 digits sign-in pages ask for.
    assert signin.totp(RFC_SECRET, 59) == "287082"
    assert signin.totp(RFC_SECRET, 1111111109) == "081804"
    assert signin.totp(RFC_SECRET, 1234567890) == "005924"
    assert signin.totp("gezd gnbv gy3t qojq gezd gnbv gy3t qojq", 59) == "287082"  # as authenticator apps print it
    with pytest.raises(ValueError):  # not base32: sign-in turns this into SignInFailed (harness), not a crash
        signin.totp("not base32!", 59)


def base_account(**kw):
    return {"email": "qa-test@example.test", "password": "env:QA_PASS", "login": {"url": "http://localhost:4000/login"},
            **kw}


SEED = "seed:/qa/qa_test_user.json#"


def test_a_code_or_a_cookie_is_only_for_a_seeded_test_user_on_a_local_dev_host():
    assert _account(base_account(totp=SEED + "totp"), None)["totp"] == SEED + "totp"
    with pytest.raises(SuiteError, match="local dev host"):
        _account(base_account(totp=SEED + "totp", login={"url": "https://dash.first4figures.com/login"}), None)
    with pytest.raises(SuiteError, match="reserved test domain"):
        _account(base_account(totp=SEED + "totp", email="someone@gmail.com"), None)
    with pytest.raises(SuiteError, match="never the value"):
        _account(base_account(totp="JBSWY3DPEHPK3PXP"), None)  # a secret written in the suite is refused
    # The review of #47: only the app's seed fixture, never a person's keychain or environment, signs in by itself
    for ref in ("env:QA_TOTP", "keychain:qajev/qa-test", "op://QA/qa-test/totp"):
        with pytest.raises(SuiteError, match="only from the app's seed fixture"):
            _account(base_account(totp=ref), None)
    cookie = _account({"email": "qa-test@example.test", "login": {"url": "http://127.0.0.1:3100/"},
                       "cookie": {"name": "figgyz-auth-token", "value": SEED + "session"}}, None)
    assert cookie["cookie"]["value"] == SEED + "session"  # no password needed with a cookie
    with pytest.raises(SuiteError, match="only from the app's seed fixture"):
        _account({"email": "qa-test@example.test", "login": {"url": "http://127.0.0.1:3100/"},
                  "cookie": {"name": "figgyz-auth-token", "value": "env:QA_TOKEN"}}, None)
    with pytest.raises(SuiteError, match="account.cookie must be"):
        _account(base_account(cookie={"name": "x"}), None)


def test_a_fixture_must_name_the_host_it_signs_in_on(tmp_path):
    # The review of #47: allowed_hosts is required for a code or a cookie; an empty or missing list refuses.
    for data, why in (({"test_account": True}, "must list its allowed_hosts"),
                      ({"test_account": True, "allowed_hosts": []}, "must list its allowed_hosts"),
                      ({"test_account": True, "allowed_hosts": ["dash.test"]},
                       "allows \\['dash.test'\\], not localhost")):
        fixture = tmp_path / "qa.json"
        fixture.write_text(json.dumps({**data, "totp": "JBSW"}))
        account = _account(base_account(totp=f"seed:{fixture}#totp"), None)
        with pytest.raises(signin.SignInFailed, match=why):
            signin._test_only(account, "http://localhost:4000/login", "qa-test@example.test", "a one-time code")
    fixture.write_text(json.dumps({"test_account": True, "allowed_hosts": ["localhost"], "totp": "JBSW"}))
    assert signin._test_only(account, "http://localhost:4000/login", "qa-test@example.test", "a code") == "localhost"
    with pytest.raises(signin.SignInFailed, match="local dev host"):  # where the browser really is, read as it reads
        signin._test_only(account, "http://evil.com\\@localhost/login", "qa-test@example.test", "a code")


@pytest.mark.parametrize("url", [
    "http://evil.com\\@localhost/login",       # Chrome reads \ as /: host evil.com; urlsplit says localhost
    "http://evil.com\\localhost/login",
    "http://user@localhost/login",            # userinfo: never in a test sign-in address
    "http://localhost\t.evil.com/login",
    "http://local\nhost/login",
    "http://localhost%2f@evil.com/login",
])
def test_a_host_a_browser_reads_otherwise_is_never_local(url):
    # The Orchestrator's review of #47: a cookie preset could go to evil.com through a URL Python and Chrome parse
    # differently. Such an address is never loopback or a local dev host, so neither a cookie nor mutate mode go there.
    from qajev import config

    assert not config.is_local_dev(url) and not config.is_loopback(url)
    with pytest.raises(SuiteError):
        _account(base_account(totp=SEED + "totp", login={"url": url}), None)


def test_a_seed_reference_reads_only_a_fixture_that_says_it_is_a_test_account(tmp_path):
    good = tmp_path / "qa_test_user.json"
    good.write_text(json.dumps({"test_account": True, "email": "qa-test@example.test", "totp_secret_base32": "JBSW"}))
    account = _account(base_account(totp="seed:qa_test_user.json#totp_secret_base32"), None, tmp_path)
    assert account["totp"] == f"seed:{good}#totp_secret_base32"  # relative to the suite's folder
    assert vault.resolve(account["totp"]) == "JBSW"
    real = tmp_path / "users.json"
    real.write_text(json.dumps({"email": "someone@example.com", "password": "not-a-fixture"}))
    with pytest.raises(vault.VaultError, match="not a test-user fixture") as e:
        vault.resolve(f"seed:{real}#password")
    assert "not-a-fixture" not in str(e.value)
    with pytest.raises(vault.VaultError, match="no text value"):
        vault.resolve(f"seed:{good}#missing")
