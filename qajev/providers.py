"""Where the decisions and the text helper's values come from, chosen by the keys provided.

The decision model (Jev's role) is served three ways with the same request body:
  TypeSafe    POST https://api.typesafe.ai/v1/systemone   TYPESAFE_API_KEY, model jev-latest
  OpenRouter  POST https://openrouter.ai/api/v1/systemone  OPENROUTER_API_KEY, model ~typesafe/jev-latest
  Cloudflare  POST .../accounts/ID/ai/run/@cf/cloudflare/MODEL  CLOUDFLARE_ACCOUNT_ID + CLOUDFLARE_API_TOKEN,
              model clef-flash (default) or clef (QAJEV_CLEF_MODEL): Cloudflare's open decision models
The text helper (values Jev types) is any OpenAI-compatible chat endpoint; an OpenRouter key covers it too.
"""

import os

from .config import ROUTING_PLACEHOLDER as PLACEHOLDER  # jev's TYPESAFE_API_KEY on OpenRouter

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_SYSTEMONE = OPENROUTER_BASE + "/systemone"
OPENROUTER_JEV_MODEL = "~typesafe/jev-latest"
CLOUDFLARE_RUN = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/cloudflare/{model}"
CLEF_MODELS = {"clef-flash": 0.09, "clef": 0.24}  # USD per million input tokens (Workers AI, 2026-10); output free
PROVIDERS = ("auto", "typesafe", "openrouter", "cloudflare")


class ProviderError(RuntimeError):
    pass


def _openrouter_key(env):
    if env.get("OPENROUTER_API_KEY"):
        return env["OPENROUTER_API_KEY"], "OPENROUTER_API_KEY"
    if env.get("TEXT_MODEL_API_KEY") and "openrouter.ai" in env.get("TEXT_MODEL_BASE_URL", OPENROUTER_BASE):
        return env["TEXT_MODEL_API_KEY"], "TEXT_MODEL_API_KEY"
    return None, None


def resolve(env=None):
    """-> {jev, jev_key_name, jev_url, jev_model, text, text_key_name, text_model}. Never contains key values."""
    env = os.environ if env is None else env
    wanted = (env.get("QAJEV_JEV_PROVIDER") or "auto").lower()
    if wanted not in PROVIDERS:
        raise ProviderError(f"QAJEV_JEV_PROVIDER must be one of {PROVIDERS}")
    or_key, or_name = _openrouter_key(env)
    out: dict[str, object] = {"jev": None, "jev_key_name": None, "jev_url": None, "jev_model": None}
    typesafe_key = env.get("TYPESAFE_API_KEY") if env.get("TYPESAFE_API_KEY") != PLACEHOLDER else None
    if wanted in {"auto", "typesafe"} and typesafe_key:
        out.update(jev="typesafe", jev_key_name="TYPESAFE_API_KEY", jev_url=TYPESAFE_URL,
                   jev_model=env.get("TYPESAFE_MODEL", "jev-latest"))
    elif wanted in {"auto", "openrouter"} and or_key:
        out.update(jev="openrouter", jev_key_name=or_name, jev_url=OPENROUTER_SYSTEMONE,
                   jev_model=env.get("QAJEV_OPENROUTER_JEV_MODEL", OPENROUTER_JEV_MODEL))
    elif wanted in {"auto", "cloudflare"} and env.get("CLOUDFLARE_API_TOKEN") and env.get("CLOUDFLARE_ACCOUNT_ID"):
        model = env.get("QAJEV_CLEF_MODEL", "clef-flash")
        if model not in CLEF_MODELS:
            raise ProviderError(f"QAJEV_CLEF_MODEL must be one of {sorted(CLEF_MODELS)}")
        out.update(jev="cloudflare", jev_key_name="CLOUDFLARE_API_TOKEN", jev_model=model,
                   jev_url=CLOUDFLARE_RUN.format(account=env["CLOUDFLARE_ACCOUNT_ID"], model=model))
    elif wanted != "auto":
        need = {"typesafe": "TYPESAFE_API_KEY", "openrouter": "OPENROUTER_API_KEY",
                "cloudflare": "CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN"}[wanted]
        raise ProviderError(f"QAJEV_JEV_PROVIDER={wanted} needs {need}")
    out["decider"] = decider(out["jev_model"]) if out["jev"] else None
    text_name = "TEXT_MODEL_API_KEY" if env.get("TEXT_MODEL_API_KEY") else ("OPENROUTER_API_KEY" if or_key else None)
    out.update(text=bool(text_name), text_key_name=text_name, text_model=env.get("TEXT_MODEL"),
               text_base_url=env.get("TEXT_MODEL_BASE_URL", OPENROUTER_BASE))
    return out


def apply(env=None):
    """Fill jev_ultrafast's own variables so it runs unchanged, whichever key was provided."""
    env = os.environ if env is None else env
    resolved = resolve(env)
    if resolved["jev"] in {"openrouter", "cloudflare"} and not env.get("TYPESAFE_API_KEY"):
        env["TYPESAFE_API_KEY"] = PLACEHOLDER  # jev reads it by name; route() swaps in the real key
    if not env.get("TEXT_MODEL_API_KEY") and env.get("OPENROUTER_API_KEY"):
        env["TEXT_MODEL_API_KEY"] = env["OPENROUTER_API_KEY"]
        env["TEXT_MODEL_BASE_URL"] = OPENROUTER_BASE
    return resolved


def decider(model):
    """The decision model's name as people know it: Jev, Clef or Clef-flash."""
    return {"clef-flash": "Clef-flash", "clef": "Clef"}.get(str(model), "Jev")


def route(post_json, resolved, env=None):
    """Wrap jev's post_json so decisions go to the resolved provider."""
    env = os.environ if env is None else env
    if resolved["jev"] == "cloudflare":
        return _clef(post_json, resolved, env)
    if resolved["jev"] != "openrouter":
        return post_json

    def routed(url, key, body):
        if url == TYPESAFE_URL:
            url, key = OPENROUTER_SYSTEMONE, env[resolved["jev_key_name"]]
            body = {**body, "model": resolved["jev_model"]}
        return post_json(url, key, body)

    return routed


def _clef(post_json, resolved, env):
    """Decisions to Clef on Workers AI. Two differences from Jev's API are bridged here: Clef refuses a choice
    question with a single option (that answer is certain, so QAJev gives it, probability 1), and Workers AI wraps
    its answer in {"result": ...}. The cost is Workers AI's per-token price, from Clef's own token count."""
    model = resolved["jev_model"]

    def routed(url, key, body):
        if url != TYPESAFE_URL:
            return post_json(url, key, body)
        sure, asked = {}, {}
        for qid, question in (body.get("questions") or {}).items():
            options = list(question.get("criteria") or {}) if question.get("type") == "choice" else None
            if options is not None and len(options) == 1:
                sure[qid] = {"type": "choice", "choice": options[0], "probabilities": {options[0]: 1.0},
                             "confidence": 1.0}
            else:
                asked[qid] = question
        out = {"model": model, "answers": {}, "usage": {}}
        if asked:
            got = post_json(resolved["jev_url"], env[resolved["jev_key_name"]], {**body, "model": model,
                                                                                 "questions": asked})
            out = (got or {}).get("result", got) or out
        out.setdefault("answers", {}).update(sure)
        usage = out.setdefault("usage", {}) or {}
        usage["cost_usd"] = round((usage.get("input_tokens") or 0) * CLEF_MODELS[model] / 1e6, 8)
        out["usage"] = usage
        return out

    return routed


def check_openrouter_key(key, timeout=10):
    """Free validity check (GET /api/v1/key spends nothing). -> HTTP status, or None when unreachable."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(OPENROUTER_BASE + "/key", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return None


def describe(resolved):
    jev = f"{resolved['jev']} ({resolved['jev_model']}, {resolved['jev_key_name']})" if resolved["jev"] else "none"
    text = f"{resolved['text_model']} via {resolved['text_base_url']} ({resolved['text_key_name']})" \
        if resolved["text"] else "none"
    return {"jev": jev, "text": text, "decider": resolved.get("decider")}
