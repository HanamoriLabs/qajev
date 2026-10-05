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


def base_account(**kw):
    return {"email": "qa-test@example.test", "password": "env:QA_PASS", "login": {"url": "http://localhost:4000/login"},
            **kw}


def test_a_code_or_a_cookie_is_only_for_a_seeded_test_user_on_a_local_dev_host():
    assert _account(base_account(totp="env:QA_TOTP"), None)["totp"] == "env:QA_TOTP"
    with pytest.raises(SuiteError, match="local dev host"):
        _account(base_account(totp="env:QA_TOTP", login={"url": "https://dash.first4figures.com/login"}), None)
    with pytest.raises(SuiteError, match="reserved test domain"):
        _account(base_account(totp="env:QA_TOTP", email="someone@gmail.com"), None)
    with pytest.raises(SuiteError, match="never the value"):
        _account(base_account(totp="JBSWY3DPEHPK3PXP"), None)  # a secret written in the suite is refused
    cookie = _account({"email": "qa-test@example.test", "login": {"url": "http://127.0.0.1:3100/"},
                       "cookie": {"name": "figgyz-auth-token", "value": "env:QA_TOKEN"}}, None)
    assert cookie["cookie"]["value"] == "env:QA_TOKEN"  # no password needed with a cookie
    with pytest.raises(SuiteError, match="account.cookie must be"):
        _account(base_account(cookie={"name": "x"}), None)


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
