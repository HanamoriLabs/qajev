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


CF = {"CLOUDFLARE_ACCOUNT_ID": "acct123", "CLOUDFLARE_API_TOKEN": "cf-token-value"}


def test_clef_on_cloudflare_is_a_choice_and_auto_still_prefers_jev():
    # Cloudflare credentials are often set for wrangler and other tools: auto never sends pages to Workers AI.
    assert P.resolve(CF)["jev"] is None and P.resolve(CF)["decider"] is None
    CF_CHOSEN = {**CF, "QAJEV_JEV_PROVIDER": "cloudflare"}
    r = P.resolve(CF_CHOSEN)
    assert (r["jev"], r["jev_model"], r["decider"]) == ("cloudflare", "clef-flash", "Clef-flash")
    assert r["jev_url"] == "https://api.cloudflare.com/client/v4/accounts/acct123/ai/run/@cf/cloudflare/clef-flash"
    assert P.resolve({**CF_CHOSEN, "QAJEV_CLEF_MODEL": "clef"})["decider"] == "Clef"
    assert P.resolve({**CF, "TYPESAFE_API_KEY": "ts"})["decider"] == "Jev"  # an existing setup does not change
    assert P.resolve({**CF, "TYPESAFE_API_KEY": "ts", "QAJEV_JEV_PROVIDER": "cloudflare"})["jev"] == "cloudflare"
    described = P.describe(r)
    assert described["decider"] == "Clef-flash" and "cf-token-value" not in str(described)
    with pytest.raises(P.ProviderError, match="CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN"):
        P.resolve({"QAJEV_JEV_PROVIDER": "cloudflare", "CLOUDFLARE_API_TOKEN": "t"})
    with pytest.raises(P.ProviderError, match="QAJEV_CLEF_MODEL"):
        P.resolve({**CF_CHOSEN, "QAJEV_CLEF_MODEL": "clef-mega"})


def test_clef_route_bridges_one_option_questions_the_envelope_and_the_cost():
    # Clef rejects a choice with a single option ("Dictionary should have at least 2 items"); Jev accepts it.
    env = {**CF, "QAJEV_JEV_PROVIDER": "cloudflare"}
    resolved = P.apply(env)
    sent = []

    def workers_ai(url, key, body):
        sent.append((url, key, body))
        return {"success": True, "result": {"model": "clef-flash", "usage": {"input_tokens": 1_000_000},
                                            "answers": {"operation": {"type": "choice", "choice": "CLICK",
                                                                      "probabilities": {"CLICK": 0.9, "DONE": 0.1},
                                                                      "confidence": 0.8}}}}

    post = P.route(workers_ai, resolved, env)
    out = post(P.TYPESAFE_URL, env["TYPESAFE_API_KEY"], {"model": "jev-latest", "state": {}, "questions": {
        "operation": {"type": "choice", "criteria": {"CLICK": "click", "DONE": "done"}},
        "type_text_target": {"type": "choice", "criteria": {"1": {"element": "[1] Email"}}}}})
    (url, key, body), = sent
    assert url == resolved["jev_url"] and key == "cf-token-value" and body["model"] == "clef-flash"
    assert list(body["questions"]) == ["operation"]  # the one-option question never reaches Clef
    assert out["answers"]["type_text_target"] == {"type": "choice", "choice": "1", "probabilities": {"1": 1.0},
                                                  "confidence": 1.0}
    assert out["answers"]["operation"]["choice"] == "CLICK"
    assert out["usage"]["cost_usd"] == 0.09  # a million input tokens at Clef-flash's $0.09
    post("https://openrouter.ai/api/v1/chat/completions", "or", {"model": "text"})
    assert sent[-1][0].endswith("/chat/completions")  # the text helper is untouched
