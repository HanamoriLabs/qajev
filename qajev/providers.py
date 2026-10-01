"""Where Jev's decisions and the text helper's values come from, chosen by the keys provided.

Jev is served two ways with the same request body:
  TypeSafe    POST https://api.typesafe.ai/v1/systemone   TYPESAFE_API_KEY, model jev-latest
  OpenRouter  POST https://openrouter.ai/api/v1/systemone  OPENROUTER_API_KEY, model ~typesafe/jev-latest
The text helper (values Jev types) is any OpenAI-compatible chat endpoint; an OpenRouter key covers it too.
"""

import os

from .config import ROUTING_PLACEHOLDER as PLACEHOLDER  # jev's TYPESAFE_API_KEY on OpenRouter

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_SYSTEMONE = OPENROUTER_BASE + "/systemone"
OPENROUTER_JEV_MODEL = "~typesafe/jev-latest"
PROVIDERS = ("auto", "typesafe", "openrouter")


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
    elif wanted != "auto":
        need = "TYPESAFE_API_KEY" if wanted == "typesafe" else "OPENROUTER_API_KEY"
        raise ProviderError(f"QAJEV_JEV_PROVIDER={wanted} needs {need}")
    text_name = "TEXT_MODEL_API_KEY" if env.get("TEXT_MODEL_API_KEY") else ("OPENROUTER_API_KEY" if or_key else None)
    out.update(text=bool(text_name), text_key_name=text_name, text_model=env.get("TEXT_MODEL"),
               text_base_url=env.get("TEXT_MODEL_BASE_URL", OPENROUTER_BASE))
    return out


def apply(env=None):
    """Fill jev_ultrafast's own variables so it runs unchanged, whichever key was provided."""
    env = os.environ if env is None else env
    resolved = resolve(env)
    if resolved["jev"] == "openrouter" and not env.get("TYPESAFE_API_KEY"):
        env["TYPESAFE_API_KEY"] = PLACEHOLDER  # jev reads it by name; route() swaps in the real key
    if not env.get("TEXT_MODEL_API_KEY") and env.get("OPENROUTER_API_KEY"):
        env["TEXT_MODEL_API_KEY"] = env["OPENROUTER_API_KEY"]
        env["TEXT_MODEL_BASE_URL"] = OPENROUTER_BASE
    return resolved


def route(post_json, resolved, env=None):
    """Wrap jev's post_json so decisions go to the resolved provider."""
    env = os.environ if env is None else env
    if resolved["jev"] != "openrouter":
        return post_json

    def routed(url, key, body):
        if url == TYPESAFE_URL:
            url, key = OPENROUTER_SYSTEMONE, env[resolved["jev_key_name"]]
            body = {**body, "model": resolved["jev_model"]}
        return post_json(url, key, body)

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
    return {"jev": jev, "text": text}
