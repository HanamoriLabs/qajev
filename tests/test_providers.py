import os

import pytest

from qajev import providers as P


@pytest.mark.parametrize("env, jev, key_name", [
    ({"TYPESAFE_API_KEY": "ts-secret"}, "typesafe", "TYPESAFE_API_KEY"),
    ({"OPENROUTER_API_KEY": "or"}, "openrouter", "OPENROUTER_API_KEY"),
    ({"TYPESAFE_API_KEY": "ts", "OPENROUTER_API_KEY": "or"}, "typesafe", "TYPESAFE_API_KEY"),
    # José's existing jev-ultrafast .env: the text helper's OpenRouter key is enough for Jev too
    ({"TEXT_MODEL_API_KEY": "or", "TEXT_MODEL_BASE_URL": "https://openrouter.ai/api/v1"}, "openrouter",
     "TEXT_MODEL_API_KEY"),
    ({"TEXT_MODEL_API_KEY": "ds", "TEXT_MODEL_BASE_URL": "https://api.deepseek.com/v1"}, None, None),
    ({"TYPESAFE_API_KEY": "ts", "OPENROUTER_API_KEY": "or", "QAJEV_JEV_PROVIDER": "openrouter"}, "openrouter",
     "OPENROUTER_API_KEY"),
    ({}, None, None),
])
def test_the_route_follows_the_keys(env, jev, key_name):
    r = P.resolve(env)
    assert (r["jev"], r["jev_key_name"]) == (jev, key_name)
    described = str(P.describe(r))
    assert all(v not in described for k, v in env.items() if k.endswith("_KEY"))  # names keys, never values


def test_forcing_a_provider_without_its_key_is_an_error():
    with pytest.raises(P.ProviderError, match="OPENROUTER_API_KEY"):
        P.resolve({"TYPESAFE_API_KEY": "ts", "QAJEV_JEV_PROVIDER": "openrouter"})
    with pytest.raises(P.ProviderError, match="one of"):
        P.resolve({"QAJEV_JEV_PROVIDER": "bing"})


def test_openrouter_route_rewrites_url_key_and_model_but_leaves_text_calls_alone():
    env = {"OPENROUTER_API_KEY": "or-key"}
    resolved = P.apply(env)
    assert env["TYPESAFE_API_KEY"] == P.PLACEHOLDER
    assert env["TEXT_MODEL_API_KEY"] == "or-key" and env["TEXT_MODEL_BASE_URL"] == P.OPENROUTER_BASE
    assert P.resolve(env)["jev"] == "openrouter", "the placeholder must not look like a TypeSafe key"
    sent = []
    post = P.route(lambda url, key, body: sent.append((url, key, body)) or {}, resolved, env)
    post(P.TYPESAFE_URL, env["TYPESAFE_API_KEY"], {"model": "jev-latest", "questions": {}})
    post("https://openrouter.ai/api/v1/chat/completions", "or-key", {"model": "inception/mercury-2.5"})
    assert sent[0] == (P.OPENROUTER_SYSTEMONE, "or-key", {"model": "~typesafe/jev-latest", "questions": {}})
    assert sent[1][0].endswith("/chat/completions") and sent[1][2]["model"] == "inception/mercury-2.5"


def test_typesafe_route_is_untouched():
    env = {"TYPESAFE_API_KEY": "ts"}
    raw = object()
    assert P.route(raw, P.apply(env), env) is raw


def test_the_routing_placeholder_never_masks_a_real_key_from_an_env_file(tmp_path, monkeypatch):
    # A child process inherits the placeholder from a parent that routed to OpenRouter; its env file must still win.
    from qajev.config import load_env

    monkeypatch.setenv("TYPESAFE_API_KEY", P.PLACEHOLDER)
    monkeypatch.delenv("QAJEV_JEV_PROVIDER", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("TYPESAFE_API_KEY=fake-value-for-test\n")
    load_env(str(env_file))
    assert os.environ["TYPESAFE_API_KEY"] == "fake-value-for-test"
    assert P.resolve()["jev"] == "typesafe"
