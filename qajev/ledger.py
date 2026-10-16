"""Reserve maximum liability before every paid HTTP attempt, including retries.

Integer microdollars avoid rounding down a reservation. Missing usage and failed
responses keep their allowance: they do not prove the provider did not bill it.
"""

import math
import os
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, TypeGuard
from urllib.parse import urlsplit


class CostCapReached(RuntimeError):
    pass


def _number(value: object) -> TypeGuard[int | float]:
    return type(value) is int and value >= 0 or type(value) is float and math.isfinite(value) and value >= 0


def _token(value: object) -> TypeGuard[int]:
    return type(value) is int and 0 <= value <= 100_000_000


def _micros(value, rounding=ROUND_CEILING):
    return int((Decimal(str(value)) * 1_000_000).to_integral_value(rounding=rounding))


@dataclass(frozen=True)
class _Attempt:
    serial: int
    kind: str
    reserve: int
    pricing: tuple | None
    bound_basis: str
    cloud_normalized: bool
    quote: int


@dataclass
class SharedBudget:
    """Run-wide dollars and quote-integrity bit, always read/written under one lock."""

    dollars: Any  # multiprocessing synchronized ctypes wrappers, shared with spawned workers
    frozen: Any

    @classmethod
    def create(cls, context):
        lock = context.RLock()
        return cls(context.Value("d", 0.0, lock=lock), context.Value("b", False, lock=lock))

    def get_lock(self):
        return self.dollars.get_lock()

    @property
    def value(self):
        return self.dollars.value

    @value.setter
    def value(self, value):
        self.dollars.value = value


class Ledger:
    def __init__(self, cap_usd=1.0, typesafe_usd_per_call=0.0005, shared=None, unknown_usd_per_attempt=None):
        if not _number(cap_usd) or not _number(typesafe_usd_per_call):
            raise CostCapReached("cost cap and per-attempt estimate must be finite, non-negative dollars")
        explicit = unknown_usd_per_attempt
        if explicit is None and os.environ.get("QAJEV_MODEL_MAX_USD"):
            try:
                explicit = float(os.environ["QAJEV_MODEL_MAX_USD"])
            except ValueError:
                raise CostCapReached("QAJEV_MODEL_MAX_USD must be a positive conservative upper bound") from None
        if explicit is not None and (not _number(explicit) or explicit <= 0):
            raise CostCapReached("the explicit model upper bound must be finite and positive")
        self.cap_usd = cap_usd
        self.cap = _micros(cap_usd, ROUND_FLOOR)
        self.typesafe_usd_per_call = typesafe_usd_per_call
        self.explicit_bound = _micros(explicit) if explicit is not None else None
        # The existing multiprocessing.Value('d') stores dollars, including in-flight liabilities.
        self.shared = shared
        self.lock = threading.RLock()
        self.calls = {"typesafe": 0, "text": 0}
        self.errors = 0
        self.tokens = {"typesafe": 0, "text": 0}
        self.usd = {"typesafe": 0, "text": 0}
        self.text_cost_reported = True
        self.basis = {"reported": 0, "token_priced": 0, "unknown": 0, "local_free": 0}
        self.bounds = {"documented": 0, "operator_estimate": 0, "cloud_enforced": 0}
        self.actual = self.estimated = self.unknown = 0
        self.active = {}
        self.serial = 0
        self.bound_violated = False
        self.guard_hold = 0  # conservative shared quarantine only for legacy bare-Value callers

    @staticmethod
    def provider(url):
        parsed = urlsplit(url)
        return "typesafe" if parsed.hostname == "api.typesafe.ai" or parsed.path.endswith(("/systemone", "/decide")) \
            or "/@cf/cloudflare/clef" in parsed.path else "text"

    @staticmethod
    def _pricing(url, body):
        """Supplier context/rates verified 16 Oct 2026; no cross-host model aliases.

        (input context tokens, USD/M input, USD/M output, maximum output).
        Bounds depend on suppliers enforcing documented context/output limits.
        """
        try:
            parsed, model = urlsplit(url), body.get("model")
            port = parsed.port
        except (TypeError, ValueError):
            raise CostCapReached("model requests need a valid HTTPS URL without embedded credentials") from None
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise CostCapReached("model requests need HTTPS and no embedded URL credentials")
        if port not in (None, 443) or parsed.query or parsed.fragment:
            return None  # modified endpoints never inherit a supplier's price/context qualification
        if parsed.hostname == "api.typesafe.ai" and parsed.path == "/v1/systemone" and model in (
            None, "jev-latest", "jev-1.13.0", "jev-preview"
        ):
            if body.get("long_context") or body.get("long-context"):
                raise CostCapReached("long-context requests have no enabled reservation contract")
            return 65_536, Decimal("0.042"), Decimal(0), 0
        if parsed.hostname == "api.cloudflare.com":
            prices = {"clef": (65_536, "0.24"), "clef-flash": (24_576, "0.038"),
                      "clef-omni": (64_000, "0.15")}
            for name, (context, price) in prices.items():
                if parsed.path.endswith("/ai/run/@cf/cloudflare/" + name):
                    return context, Decimal(price), Decimal(0), 0
            if parsed.path.endswith("/ai/run/@cf/google/gemma-4-26b-a4b-it"):
                output = body.get("max_tokens", 16_384)
                if type(output) is not int or not 1 <= output <= 16_384:
                    raise CostCapReached("Gemma max_tokens must be 1..16384 for its bounded output cost")
                body["max_tokens"] = output
                return 256_000, Decimal("0.10"), Decimal("0.30"), output
        return None

    def _liability(self):
        return sum(self.usd.values()) + sum(a.reserve for a in self.active.values()) + self.guard_hold

    def spent(self):
        with self.lock, self.shared.get_lock() if self.shared is not None else nullcontext():
            return self.shared.value if self.shared is not None else self._liability() / 1e6

    def before(self, url, body, verified_bound=None):
        if not isinstance(body, dict):
            raise CostCapReached("a bounded model attempt needs a JSON object")
        pricing = self._pricing(url, body)
        if verified_bound is not None and (type(verified_bound) is not int or verified_bound <= 0):
            raise CostCapReached("authenticated Cloud model bound must be a positive integer in microdollars")
        if pricing:
            context, input_price, output_price, output = pricing
            reserve = int((context * input_price + output * output_price).to_integral_value(rounding=ROUND_CEILING))
            quote = reserve
            operator_floor = self.explicit_bound or 0
            if urlsplit(url).hostname == "api.typesafe.ai":
                operator_floor = max(operator_floor, _micros(self.typesafe_usd_per_call))
            bound_basis = "operator_estimate" if operator_floor > reserve else "documented"
            reserve = max(reserve, operator_floor, verified_bound or 0)
        elif verified_bound is not None:
            reserve = max(verified_bound, self.explicit_bound or 0)
            quote = verified_bound
            bound_basis = "operator_estimate" if (self.explicit_bound or 0) > verified_bound else "cloud_enforced"
        elif self.explicit_bound is not None:
            reserve, bound_basis = self.explicit_bound, "operator_estimate"
            quote = reserve
        else:
            raise CostCapReached("no documented model price/context bound; set an explicit conservative "
                                 "QAJEV_MODEL_MAX_USD upper bound before a paid request")
        parsed = urlsplit(url)
        if parsed.hostname == "openrouter.ai" and parsed.path.endswith("/chat/completions"):
            usage = body.get("usage")
            body["usage"] = {**(usage if isinstance(usage, dict) else {}), "include": True}
        with self.lock, self.shared.get_lock() if self.shared is not None else nullcontext():
            liability = _micros(self.shared.value) if self.shared is not None else self._liability()
            frozen = isinstance(self.shared, SharedBudget) and self.shared.frozen.value
            if self.bound_violated or frozen or liability + reserve > self.cap:
                raise CostCapReached(f"cost cap ${self.cap_usd:.6g}: next attempt needs "
                                     f"${reserve / 1e6:.6f}; no call made")
            self.serial += 1
            attempt = _Attempt(self.serial, self.provider(url), reserve, pricing, bound_basis,
                               verified_bound is not None, quote)
            self.active[attempt.serial] = attempt
            self.bounds[bound_basis] += 1
            if self.shared is not None:
                self.shared.value = round(self.shared.value + reserve / 1e6, 6)
            return attempt

    @staticmethod
    def _usage(result, pricing, cloud_normalized=False, quote=None):
        if isinstance(result, dict) and isinstance(result.get("result"), dict):
            result = result["result"]
        usage = result.get("usage") if isinstance(result, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        input_values = [usage[key] for key in ("prompt_tokens", "input_tokens") if key in usage]
        output_values = [usage[key] for key in ("completion_tokens", "output_tokens") if key in usage]
        valid_inputs = [value for value in input_values if _token(value)]
        valid_outputs = [value for value in output_values if _token(value)]
        input_tokens = max(valid_inputs, default=None)
        output_tokens = max(valid_outputs, default=None)
        total = usage.get("total_tokens")
        tokens = total if _token(total) else (input_tokens or 0) + (output_tokens or 0)
        supplied_tokens = [*input_values, *output_values, *([total] if "total_tokens" in usage else [])]
        supplied_money = [usage[key] for key in ("cost", "cost_usd", "total_cost", "usd") if key in usage]
        valid_money = [_micros(value) for value in supplied_money if _number(value)]
        breach = bool(quote is not None and any(value > quote for value in valid_money))
        if pricing:
            context, _, output_price, maximum_output = pricing
            breach |= any(value > context for value in valid_inputs) or bool(
                output_price and any(value > maximum_output for value in valid_outputs))
        if any(not _token(value) for value in supplied_tokens) or any(not _number(value) for value in supplied_money):
            return None, "unknown", tokens, breach
        if cloud_normalized:
            basis = usage.get("cost_basis")
            cost = usage.get("cost_usd")
            if usage.get("cost_uncertain") is not False or basis not in ("reported", "token_priced", "local_free"):
                return None, "unknown", tokens, breach
            if not _number(cost) or basis == "local_free" and cost != 0:
                return None, "unknown", tokens, breach
            return _micros(cost), basis, tokens, breach
        if valid_money:
            return max(valid_money), "reported", tokens, breach
        if pricing and _token(input_tokens):
            context, input_price, output_price, maximum_output = pricing
            if input_tokens <= context and (not output_price and
                                            (output_tokens is None or _token(output_tokens)) or
                                            _token(output_tokens) and output_tokens <= maximum_output):
                cost = Decimal(str(input_tokens)) * input_price + Decimal(str(output_tokens or 0)) * output_price
                return int(cost.to_integral_value(rounding=ROUND_CEILING)), "token_priced", tokens, breach
        return None, "unknown", tokens, breach

    def _settle(self, attempt, result, failed=False):
        with self.lock, self.shared.get_lock() if self.shared is not None else nullcontext():
            if self.active.get(getattr(attempt, "serial", None)) is not attempt:
                return  # idempotent settlement only on the ledger owning this attempt
            del self.active[attempt.serial]
            cost, basis, tokens, breach = (None, "unknown", 0, False) if failed else self._usage(
                result, attempt.pricing, attempt.cloud_normalized, attempt.quote)
            cost = attempt.reserve if cost is None else cost
            self.usd[attempt.kind] += cost
            self.tokens[attempt.kind] += tokens
            self.basis[basis] += 1
            if basis == "reported":
                self.actual += cost
            elif basis == "token_priced":
                self.estimated += cost
            else:
                self.unknown += cost
            if attempt.kind == "text" and basis != "reported":
                self.text_cost_reported = False
            if failed:
                self.errors += 1
            else:
                self.calls[attempt.kind] += 1
            self.bound_violated |= breach or cost > attempt.reserve
            if self.shared is not None:
                self.shared.value = round(self.shared.value + (cost - attempt.reserve) / 1e6, 6)
                if self.bound_violated:
                    if isinstance(self.shared, SharedBudget):
                        self.shared.frozen.value = True
                    elif not self.guard_hold:
                        # A bare Value has no shared bit. Add a full-cap safety hold,
                        # beyond all existing liabilities, so even refunding every
                        # other in-flight reservation cannot reopen admission.
                        self.guard_hold += self.cap
                        self.shared.value = round(self.shared.value + self.cap / 1e6, 6)

    def after(self, url, result, attempt):
        self._settle(attempt, result)

    def failed(self, attempt):
        self._settle(attempt, None, failed=True)

    def wrap(self, post_json, verified_bounds=None):
        """Wrap ONE HTTP attempt; retries and routing must be outside.

        verified_bounds is supplied only by the authenticated QAJev job adapter,
        keyed by exact final URL. Request bodies cannot select or lower bounds.
        """
        def ledgered(url, key, body):
            attempt = self.before(url, body, (verified_bounds or {}).get(url))
            try:
                result = post_json(url, key, body)
            except BaseException:
                self.failed(attempt)
                raise
            self.after(url, result, attempt)
            return result

        return ledgered

    def summary(self):
        with self.lock, self.shared.get_lock() if self.shared is not None else nullcontext():
            reserved = sum(a.reserve for a in self.active.values()) + self.guard_hold
            return {
                "cap_usd": self.cap_usd, "usd": self._liability() / 1e6,
                "usd_typesafe_estimated": self.usd["typesafe"] / 1e6,
                "usd_text": self.usd["text"] / 1e6, "text_cost_reported": self.text_cost_reported,
                "calls": dict(self.calls), "tokens": dict(self.tokens), "errors": self.errors,
                "actual_usd": self.actual / 1e6, "estimated_usd": self.estimated / 1e6,
                "unknown_usd": self.unknown / 1e6, "reserved_usd": reserved / 1e6,
                "guard_hold_usd": self.guard_hold / 1e6,
                "cost_basis": dict(self.basis), "reservation_basis": dict(self.bounds),
                "bound_violated": bool(self.bound_violated or
                                       isinstance(self.shared, SharedBudget) and self.shared.frozen.value),
            }
