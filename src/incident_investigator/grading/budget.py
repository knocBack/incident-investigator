"""Conservative shared batch reservations, including failed/unknown usage."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from decimal import Decimal
from uuid import uuid4

from incident_investigator.models import ModelCallError
from incident_investigator.models.openrouter import _role

D = Decimal


def amount(value):
    value = D(str(value))
    if not value.is_finite() or value < 0:
        raise ValueError("prices, usage and budgets must be finite and nonnegative")
    return value


class SpendGuard:
    def __init__(self, initial, cap):
        self.initial, self.cap = amount(initial), amount(cap)
        self.key_delta = self.returned = self.unknown = D(0)
        self.active = {}
        self.records = []
        self.lock = asyncio.Lock()

    def charged(self):
        return max(self.key_delta, self.returned + self.unknown)

    def observe(self, usage):
        self.key_delta = max(self.key_delta, amount(usage) - self.initial)

    def launch_allowed(self, bound=0):
        return self.charged() + sum(self.active.values(), D(0)) + bound <= self.cap

    async def reserve(self, bound):
        async with self.lock:
            if not self.launch_allowed(bound):
                raise ModelCallError("suite budget reservation refused call", retryable=False)
            ident = str(uuid4())
            self.active[ident] = bound
            return ident

    async def finish(self, ident, cost):
        async with self.lock:
            bound = self.active.pop(ident)
            if cost is None:
                self.unknown += bound
            else:
                self.returned += amount(cost)
                if amount(cost) > bound:
                    self.unknown += self.cap + D(1)
            self.records.append({"reservation": str(bound), "cost": cost})

    async def release(self, ident):
        async with self.lock:
            bound = self.active.pop(ident)
            self.records.append({"reservation": str(bound), "cost": "unbilled_failure"})

    def snapshot(self):
        return {
            k: str(v)
            for k, v in {
                "cap": self.cap,
                "returned": self.returned,
                "unknown_reserve": self.unknown,
                "key_delta": self.key_delta,
                "active": sum(self.active.values(), D(0)),
                "charged": self.charged(),
            }.items()
        }


def _pricing_tiers(pricing):
    """Yield the base price map and every OpenRouter override price map."""
    yield pricing
    for override in pricing.get("overrides", []):
        if isinstance(override, Mapping):
            yield override


def _rate(tiers, keys):
    return max((amount(tier.get(key, 0)) for tier in tiers for key in keys), default=D(0))


def price_bound(info, *, max_input_tokens=64000, output_tokens=4096):
    pricing = info["pricing"]
    if not isinstance(pricing, Mapping):
        raise ValueError("catalogue pricing must be an object")
    tiers = list(_pricing_tiers(pricing))
    # Text-only requests contain no images, audio, web plugins or native tools.
    inactive = {"image", "audio", "input_audio_cache", "web_search"}
    token_rates = {
        "prompt",
        "completion",
        "input_cache_read",
        "input_cache_write",
        "internal_reasoning",
    }
    for tier in tiers:
        for key, value in tier.items():
            if key == "overrides" or key.startswith("min_"):
                continue
            if key.startswith("input_cache_"):
                continue
            if key not in inactive | token_rates and amount(value or 0) != 0:
                raise ValueError(f"unsupported nonzero catalogue price: {key}")
    # A prompt token is billed at one prompt/cache rate, never their sum.  Overrides
    # are tiered catalogue prices, so reserve the most expensive possible tier.
    prompt = _rate(
        tiers,
        ("prompt", *[key for tier in tiers for key in tier if key.startswith("input_cache_")]),
    )
    completion = _rate(tiers, ("completion", "internal_reasoning"))
    prompt_tokens = min(amount(info["context_length"]), amount(max_input_tokens))
    return (prompt_tokens * prompt + amount(output_tokens) * completion) * D("1.10")


class BoundedModel:
    def __init__(self, adapter, catalog, guard, *, max_input_tokens=64000):
        self.adapter, self.catalog, self.guard = adapter, catalog, guard
        self.max_input_tokens = int(max_input_tokens)
        if self.max_input_tokens < 1:
            raise ValueError("max input tokens must be positive")
        original = adapter.build_request
        self._build_request = original

        def configured(messages, schema, model, *, structured):
            request = original(messages, schema, model, structured=structured)
            pricing = catalog[model]["pricing"]
            request["provider"]["max_price"] = {
                "prompt": float(amount(pricing["prompt"]) * 1_000_000),
                "completion": float(amount(pricing["completion"]) * 1_000_000),
                "request": 0,
            }
            return request

        adapter.build_request = configured

    def model_for_role(self, role):
        return self.adapter.model_for_role(role)

    async def complete(self, messages, schema):
        model = self.model_for_role(_role(messages))
        # Build locally first: this includes the adapter's fallback instruction and
        # lets the guard refuse an oversize prompt before any provider request.
        preview = self._build_request(messages, schema, model, structured=False)
        prompt_bytes = len(
            json.dumps(
                {"messages": preview["messages"], "schema": schema},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if (prompt_bytes + 2) // 3 > self.max_input_tokens:
            raise ModelCallError("suite input token limit refused call", retryable=False)
        output_tokens = max(4096, int(preview.get("max_tokens", 4096)))
        ident = await self.guard.reserve(
            price_bound(
                self.catalog[model],
                max_input_tokens=self.max_input_tokens,
                output_tokens=output_tokens,
            )
        )
        try:
            result = await self.adapter.complete(messages, schema)
        except ModelCallError as exc:
            if exc.before_response or (exc.status is not None and 400 <= exc.status < 600):
                await self.guard.release(ident)
            else:
                await self.guard.finish(ident, None)
            raise
        except BaseException:
            await self.guard.finish(ident, None)
            raise
        cost = result.usage.cost if result.usage else None
        if cost is None and result.raw_id and hasattr(self.adapter, "generation_cost"):
            cost = await self.adapter.generation_cost(result.raw_id)
        await self.guard.finish(ident, cost)
        return result
