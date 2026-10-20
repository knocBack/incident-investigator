"""Request-sized shared reservations with provider cost settlement."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

from incident_investigator.models import ModelCallError
from incident_investigator.models.adapter import ModelUsage
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
        self.key_delta = self.returned = self.unknown = self.estimated = D(0)
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
        bound = amount(bound)
        async with self.lock:
            if not self.launch_allowed(bound):
                raise ModelCallError(
                    "suite budget reservation refused call", retryable=False, before_send=True
                )
            ident = str(uuid4())
            self.active[ident] = bound
            return ident

    async def finish(self, ident, cost, *, source="response"):
        cost = amount(cost) if cost is not None else None
        async with self.lock:
            bound = self.active.pop(ident, None)
            if bound is None:
                return
            if cost is None:
                self.unknown += bound
            else:
                self.returned += cost
                if source == "token_estimate":
                    self.estimated += cost
                if cost > bound:
                    self.unknown += self.cap + D(1)
            self.records.append(
                {
                    "reservation": str(bound),
                    "cost": str(cost) if cost is not None else None,
                    "source": source if cost is not None else "unresolved",
                }
            )

    async def release(self, ident):
        async with self.lock:
            bound = self.active.pop(ident, None)
            if bound is not None:
                self.records.append({"reservation": str(bound), "cost": "unbilled_failure"})

    def snapshot(self):
        return {
            k: str(v)
            for k, v in {
                "cap": self.cap,
                "returned": self.returned,
                "estimated": self.estimated,
                "drift": self.key_delta - self.returned,
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


def estimated_input_tokens(request):
    """Estimate the rendered request once, including schema and message framing.

    ASCII text/JSON uses three characters per token, with extra framing allowance;
    non-ASCII retains a byte allowance. This is an estimate, not a model tokenizer.
    Real billed usage settles each reservation; an overrun fences subsequent calls.
    """
    rendered = {
        "messages": request["messages"],
        "response_format": request.get("response_format", {}),
    }
    text = json.dumps(rendered, ensure_ascii=False, separators=(",", ":"))
    ascii_chars = sum(ord(char) < 128 for char in text)
    other_bytes = sum(len(char.encode("utf-8")) for char in text if ord(char) >= 128)
    return math.ceil(ascii_chars / 3) + other_bytes + 16 * len(request["messages"]) + 64


class RunSpendGuard(SpendGuard):
    """Require both a run allowance and the shared suite allowance before dispatch."""

    def __init__(self, parent, cap):
        super().__init__(0, cap)
        self.parent = parent
        self.parent_ids = {}
        self.pending = {}

    async def reserve(self, bound):
        ident = await super().reserve(bound)
        try:
            self.parent_ids[ident] = await self.parent.reserve(bound)
        except BaseException:
            await super().release(ident)
            raise
        return ident

    async def finish(self, ident, cost, *, source="response"):
        await self._settle(ident, "finish", cost, source)

    async def release(self, ident):
        await self._settle(ident, "release")

    async def _settle(self, ident, operation, cost=None, source="response"):
        parent_id = self.parent_ids.get(ident)
        if parent_id is None:
            return
        # Cancellation may leave only one guard settled. Cleanup must replay the
        # original operation, including known billing, until both have settled.
        operation, cost, source = self.pending.setdefault(ident, (operation, cost, source))
        if operation == "release":
            await self.parent.release(parent_id)
            await super().release(ident)
        else:
            await self.parent.finish(parent_id, cost, source=source)
            await super().finish(ident, cost, source=source)
        self.parent_ids.pop(ident, None)
        self.pending.pop(ident, None)


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
        info = self.catalog[model]
        structured = "structured_outputs" in info.get(
            "supported_parameters", []
        ) and model not in getattr(self.adapter, "_schema_rejected", set())
        preview = self._build_request(messages, schema, model, structured=structured)
        prompt_tokens = estimated_input_tokens(preview)
        if prompt_tokens > self.max_input_tokens:
            raise ModelCallError(
                "suite input token limit refused call", retryable=False, before_send=True
            )
        output_tokens = int(preview.get("max_tokens", 4096))
        ident = await self.guard.reserve(
            price_bound(
                self.catalog[model],
                max_input_tokens=prompt_tokens,
                output_tokens=output_tokens,
            )
        )
        try:
            result = await self.adapter.complete(messages, schema)
        except ModelCallError as exc:
            try:
                if (
                    exc.before_send
                    or exc.before_response
                    or (exc.status is not None and 400 <= exc.status < 600)
                ):
                    await self.guard.release(ident)
                else:
                    await self.guard.finish(ident, None)
            except BaseException:
                if ident in self.guard.active:
                    await self.guard.finish(ident, None)
                raise
            raise
        except BaseException:
            await self.guard.finish(ident, None)
            raise
        cost = result.usage.cost if result.usage else None
        source = "response"
        try:
            if cost is None and result.raw_id and hasattr(self.adapter, "generation_cost"):
                cost = await self.adapter.generation_cost(result.raw_id)
                source = "generation"
            if cost is None and result.usage is not None:
                usage = result.usage
                if usage.input_tokens is not None and usage.output_tokens is not None:
                    cost = price_bound(
                        self.catalog[model],
                        max_input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                    )
                    source = "token_estimate"
            await self.guard.finish(ident, cost, source=source)
        except BaseException:
            # Cancellation during lookup must not strand an active reservation.
            if ident in self.guard.active:
                await self.guard.finish(ident, None)
            raise
        if (
            cost is not None
            and source in {"response", "generation"}
            and (result.usage is None or result.usage.cost is None)
        ):
            usage = result.usage or ModelUsage()
            result = replace(result, usage=replace(usage, cost=float(cost)))
        return result
