"""Conservative shared batch reservations, including failed/unknown usage."""

from __future__ import annotations

import asyncio
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


def price_bound(info):
    pricing = info["pricing"]
    # Text-only requests contain no images, audio, web plugins or native tools.
    inactive = {"image", "audio", "input_audio_cache", "web_search"}
    token_rates = {
        "prompt",
        "completion",
        "input_cache_read",
        "input_cache_write",
        "internal_reasoning",
    }
    for key, value in pricing.items():
        if key not in inactive | token_rates and amount(value or 0) != 0:
            raise ValueError(f"unsupported nonzero catalogue price: {key}")
    # Include cache rates conservatively, even when the provider discounts them.
    prompt = sum(
        (amount(pricing.get(k, 0)) for k in ("prompt", "input_cache_read", "input_cache_write")),
        D(0),
    )
    completion = max(amount(pricing["completion"]), amount(pricing.get("internal_reasoning", 0)))
    return (amount(info["context_length"]) * prompt + D(4096) * completion) * D("1.10")


class BoundedModel:
    def __init__(self, adapter, catalog, guard):
        self.adapter, self.catalog, self.guard = adapter, catalog, guard
        original = adapter.build_request

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
        ident = await self.guard.reserve(price_bound(self.catalog[model]))
        try:
            result = await self.adapter.complete(messages, schema)
        except BaseException:
            await self.guard.finish(ident, None)
            raise
        await self.guard.finish(ident, result.usage.cost if result.usage else None)
        return result
