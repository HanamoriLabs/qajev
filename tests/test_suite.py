import pytest

from qajev import suite as S
from qajev.cli import SUITE_TEMPLATE


def parse(scenarios, **top):
    return S.parse({"name": "t", "scenarios": scenarios, **top})


def test_relative_urls_resolve_against_base_url():
    s = parse([{"url": "/pricing", "expect": {"text": "Pro"}}], base_url="http://localhost:3000/app/")
    assert s.scenarios[0].url == "http://localhost:3000/pricing"
    assert s.hosts == ["localhost:3000"]


def test_first_scenario_defaults_to_base_url():
    s = parse([{"expect": {"text": ["x"]}}], base_url="http://127.0.0.1:8080")
    assert s.scenarios[0].url == "http://127.0.0.1:8080"


def test_a_step_without_url_continues_and_depends_on_its_predecessor():
    s = parse([
        {"name": "a", "url": "http://127.0.0.1:1/", "goal": "open it"},
        {"name": "b", "goal": "then this", "expect": {"text": "done"}},
    ])
    assert s.scenarios[1].url is None
    assert s.scenarios[1].depends_on == ["a"]


def test_expect_strings_become_lists_and_fetch_gets_a_default_status():
    s = parse([{"url": "http://h/", "expect": {"text": "Hi", "absent": "Oops", "fetch": "/api/health"}}])
    e = s.scenarios[0].expect
    assert e["text"] == ["Hi"] and e["absent"] == ["Oops"]
    assert e["fetch"] == [{"url": "/api/health", "status": 200}]


def test_an_expected_status_is_a_page_check_and_must_be_a_number():
    s = parse([{"url": "http://h/old", "expect": {"status": 404, "text": "File not found"}}])
    assert s.scenarios[0].expect["status"] == 404 and S.page_checks(s.scenarios[0].expect)
    assert S.page_checks(parse([{"url": "http://h/old", "expect": {"status": 404}}]).scenarios[0].expect)
    with pytest.raises(S.SuiteError, match="status"):
        parse([{"url": "http://h/", "expect": {"status": "gone"}}])


def test_persona_is_prepended_to_the_goal():
    s = parse([{"url": "http://h/", "goal": "Find pricing."}], persona="You are new here.")
    assert s.scenarios[0].task == "You are new here.\n\nWhat you want now: Find pricing."


@pytest.mark.parametrize("device, width, mobile", [
    ("phone", 390, True), ("tall", 1280, False), ("800x600", 800, False),
])
def test_devices(device, width, mobile):
    s = parse([{"url": "http://h/", "expect": {"text": "x"}, "device": device}])
    assert s.scenarios[0].device["width"] == width
    assert s.scenarios[0].device["mobile"] is mobile


@pytest.mark.parametrize("scenarios, top, message", [
    ([{"url": "http://h/"}], {}, "give a goal, an expect block, or both"),
    ([{"url": "ftp://h/", "expect": {"text": "x"}}], {}, r"must be http\(s\)"),
    ([{"goal": "x"}], {}, "first scenario needs a url"),
    ([{"url": "http://h/", "goal": "x", "surprise": 1}], {}, "unknown key"),
    ([{"url": "http://h/", "goal": "x", "budget": {"actions": 61}}], {}, "capped at 60"),
    ([{"url": "http://h/", "goal": "x", "depends_on": "later"}], {}, "must name an earlier scenario"),
    ([{"name": "a", "url": "http://h/", "goal": "x"}, {"name": "a", "url": "http://h/", "goal": "y"}], {}, "duplicate"),
    ([{"url": "http://h/", "goal": "x", "device": "watch"}], {}, "device must be"),
    ([{"url": "http://h/", "expect": {"url_regex": "("}}], {}, "not a valid regex"),
])
def test_malformed_suites_are_refused_with_a_reason(scenarios, top, message):
    with pytest.raises(S.SuiteError, match=message):
        parse(scenarios, **top)


def test_mutate_is_refused_on_any_non_loopback_host():
    with pytest.raises(S.SuiteError, match="loopback"):
        parse([{"url": "https://console.example.com/", "goal": "x", "mode": "mutate"}])
    with pytest.raises(S.SuiteError, match="loopback"):
        parse([{"url": "http://localhost:3000/", "goal": "x", "mode": "mutate"}], hosts=["api.example.com"])


@pytest.mark.parametrize("host", ["localhost:3101", "127.0.0.1:5000", "app.localhost:80", "[::1]:8080"])
def test_mutate_is_allowed_on_loopback(host):
    s = parse([{"url": f"http://{host}/", "goal": "x", "mode": "mutate"}])
    assert s.mutates


def test_secret_fields_can_only_be_unlocked_on_loopback():
    with pytest.raises(S.SuiteError, match="allow_secret_fields"):
        parse([{"url": "https://example.com/", "goal": "x"}], guard={"allow_secret_fields": True})
    parse([{"url": "http://localhost:1/", "goal": "x"}], guard={"allow_secret_fields": True})


def test_command_hooks_are_detected():
    s = parse([{"url": "http://h/", "goal": "x", "after": [{"command": "true"}]}])
    assert s.scenarios[0].uses_commands
    with pytest.raises(S.SuiteError, match="must be one of"):
        parse([{"url": "http://h/", "goal": "x", "before": [{"shell": "true"}]}])


def test_the_starter_template_is_a_valid_suite(tmp_path):
    path = tmp_path / "qajev.yaml"
    path.write_text(SUITE_TEMPLATE.format(name=path))
    s = S.load(path)
    assert [x.name for x in s.scenarios] == ["home loads", "find pricing", "compare plans"]
    assert s.scenarios[2].depends_on == ["find pricing"]


def test_motion_is_reduced_by_default_and_validated():
    base = {"base_url": "http://127.0.0.1:1", "scenarios": [{"expect": {"text": "x"}}]}
    assert S.parse(base).motion == "reduce"
    assert S.parse({**base, "motion": "full"}).motion == "full"
    with pytest.raises(S.SuiteError, match="motion"):
        S.parse({**base, "motion": "slow"})


def test_an_account_names_where_its_password_lives_and_signs_in_over_https():
    account = {"email": "qa+shop@example.com", "password": "keychain:qajev/shop-tester",
               "login": {"url": "/login", "signed_in": {"url_not": "/login"}}}
    s = parse([{"url": "/account", "expect": {"text": ["Orders"]}}], base_url="https://shop.example",
              account=account)
    assert s.account == {"name": "qa+shop@example.com", "email": "qa+shop@example.com",
                         "password": "keychain:qajev/shop-tester",
                         "login": {"url": "https://shop.example/login", "signed_in": {"url_not": "/login"}}}
    sso = parse([{"url": "/", "expect": {"text": ["x"]}}], base_url="https://shop.example",
                account={**account, "login": {"url": "https://auth.example/sign-in"}})
    assert "auth.example" in sso.hosts  # the sign-in host is one QAJev may visit
    with pytest.raises(S.SuiteError, match="never the value"):
        parse([{"url": "/", "expect": {"text": ["x"]}}], base_url="https://shop.example",
              account={**account, "password": "hunter2-plain"})
    with pytest.raises(S.SuiteError, match="https"):
        parse([{"url": "/", "expect": {"text": ["x"]}}], base_url="http://shop.example",
              account=account)  # a password never travels unencrypted; http is for localhost only
    assert parse([{"url": "/", "expect": {"text": ["x"]}}], base_url="http://127.0.0.1:8765",
                 account=account).account["login"]["url"] == "http://127.0.0.1:8765/login"
    with pytest.raises(S.SuiteError, match="login.url"):
        parse([{"url": "/", "expect": {"text": ["x"]}}], base_url="https://shop.example",
              account={**account, "login": {}})


def test_about_says_what_a_test_proves_on_the_suite_and_each_scenario():
    s = parse([{"name": "buy", "url": "http://h/", "goal": "Buy it.", "about": "a visitor can pay for a plan"},
               {"name": "home", "url": "http://h/", "expect": {"text": "Hi"}}],
              about="the shop takes money", devices=["desktop", "phone"])
    assert s.about == "the shop takes money"
    assert [(x.name, x.about) for x in s.scenarios] == [
        ("buy", "a visitor can pay for a plan"), ("home", None),
        ("buy (phone)", "a visitor can pay for a plan"), ("home (phone)", None)]
    with pytest.raises(S.SuiteError, match="about"):
        parse([{"url": "http://h/", "goal": "x", "about": ["not", "text"]}])
