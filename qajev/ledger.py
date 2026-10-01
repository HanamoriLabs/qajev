"""Model-call ledger with a hard cost cap, checked before every paid call.

TypeSafe reports tokens, not dollars, so its calls are priced at a flat estimate (measured at about
$0.0005 per decision). OpenRouter is asked to include the real cost in each response.
"""

import threading


class CostCapReached(RuntimeError):
    pass


class Ledger:
    def __init__(self, cap_usd=1.0, typesafe_usd_per_call=0.0005, shared=None):
        self.cap_usd = cap_usd
        self.typesafe_usd_per_call = typesafe_usd_per_call
        self.shared = shared  # multiprocessing.Value('d') when several workers share one cap
        self.lock = threading.Lock()
        self.calls = {"typesafe": 0, "text": 0}
        self.errors = 0
        self.tokens = {"typesafe": 0, "text": 0}
        self.usd = {"typesafe": 0.0, "text": 0.0}
        self.text_cost_reported = True

    @staticmethod
    def provider(url):
        # "typesafe" = Jev decisions, whichever service answers them (TypeSafe or OpenRouter's systemone).
        return "typesafe" if "typesafe.ai" in url or url.endswith("/systemone") else "text"

    def spent(self):
        if self.shared is not None:
            return self.shared.value
        return self.usd["typesafe"] + self.usd["text"]

    def before(self, url, body):
        if self.spent() >= self.cap_usd:
            raise CostCapReached(f"cost cap ${self.cap_usd:.2f} reached; no call made")
        if "openrouter.ai" in url and url.endswith("/chat/completions") and isinstance(body, dict):
            body.setdefault("usage", {"include": True})

    def after(self, url, result):
        kind = self.provider(url)
        usage = (result or {}).get("usage") or {}
        tokens = usage.get("total_tokens") or (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))
        cost = None
        for key in ("cost", "cost_usd", "total_cost", "usd"):
            if isinstance(usage.get(key), (int, float)):
                cost = float(usage[key])
                break
        if cost is None:
            if kind == "typesafe":
                cost = self.typesafe_usd_per_call
            else:
                cost = 0.0
                self.text_cost_reported = False
        with self.lock:
            self.calls[kind] += 1
            self.tokens[kind] += int(tokens or 0)
            self.usd[kind] += cost
            if self.shared is not None:
                with self.shared.get_lock():
                    self.shared.value += cost

    def failed(self):
        with self.lock:
            self.errors += 1

    def wrap(self, post_json):
        def ledgered(url, key, body):
            self.before(url, body)
            try:
                result = post_json(url, key, body)
            except Exception:
                self.failed()
                raise
            self.after(url, result)
            return result

        return ledgered

    def summary(self):
        return {
            "cap_usd": self.cap_usd,
            "usd": round(self.usd["typesafe"] + self.usd["text"], 5),
            "usd_typesafe_estimated": round(self.usd["typesafe"], 5),
            "usd_text": round(self.usd["text"], 5),
            "text_cost_reported": self.text_cost_reported,
            "calls": dict(self.calls),
            "tokens": dict(self.tokens),
            "errors": self.errors,
        }
