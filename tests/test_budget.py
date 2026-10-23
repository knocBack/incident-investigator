"""Budget regression: unknown completed costs formerly held full context reservations.

This is a provider-accounting defect, independent of graph behavior. All requests use
httpx.MockTransport; no paid requests or evaluator answers enter runtime context.
"""

import asyncio
import json
from decimal import Decimal

import httpx
import pytest

from incident_investigator.grading.budget import BoundedModel, SpendGuard, price_bound
from incident_investigator.models import ModelCallError, OpenRouterModel

MODEL = "test/paid"
INFO = {
    "id": MODEL,
    "context_length": 200000,
    "pricing": {"prompt": ".000001", "completion": ".000002"},
}


def bounded(monkeypatch, handler, cap="2.00"):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")
    adapter = OpenRouterModel(transport=httpx.MockTransport(handler))
    adapter._models = dict.fromkeys(adapter._models, MODEL)
    guard = SpendGuard(0, cap)
    return BoundedModel(adapter, {MODEL: INFO}, guard), guard


def completion(cost=None, *, counts=True):
    return {
        "id": "gen-synthetic",
        "choices": [{"message": {"content": '{"kind":"final"}'}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": cost} if counts else {},
    }


@pytest.mark.parametrize(
    ("response_cost", "lookup_cost", "expected", "source"),
    [
        (0.00014, None, Decimal(".00014"), "response"),
        (None, 0.00014, Decimal(".00014"), "generation"),
        (None, None, Decimal(".000154"), "token_estimate"),
    ],
)
async def test_completed_cost_settlement(monkeypatch, response_cost, lookup_cost, expected, source):
    lookups = []

    async def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [INFO]})
        if request.url.path.endswith("/generation"):
            lookups.append(request.url.params["id"])
            assert request.headers["Authorization"] == "Bearer synthetic-test-key"
            return httpx.Response(200, json={"data": {"total_cost": lookup_cost}})
        payload = json.loads(request.content)
        assert payload["usage"] == {"include": True}
        assert payload["provider"]["max_price"]["request"] == 0
        return httpx.Response(200, json=completion(response_cost))

    model, guard = bounded(monkeypatch, handler)
    await model.complete([{"role": "user", "content": "hello"}], {})
    assert guard.charged() == expected
    assert guard.unknown == 0 and not guard.active
    assert guard.records[0]["source"] == source
    assert Decimal(guard.records[0]["reservation"]) < price_bound(INFO)
    assert Decimal(guard.records[0]["reservation"]) > expected
    assert lookups == ([] if response_cost is not None else ["gen-synthetic"])
    assert guard.estimated == (expected if source == "token_estimate" else 0)


@pytest.mark.parametrize("failure", ["429", "connection", "timeout"])
async def test_unbilled_transport_failure_releases(monkeypatch, failure):
    async def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [INFO]})
        if failure == "429":
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        if failure == "connection":
            raise httpx.ConnectError("synthetic", request=request)
        raise httpx.ReadTimeout("synthetic", request=request)

    model, guard = bounded(monkeypatch, handler)
    with pytest.raises(ModelCallError):
        await model.complete([], {})
    assert guard.charged() == 0 and not guard.active
    assert guard.records[-1]["cost"] == "unbilled_failure"


async def test_two_concurrent_calls_never_exceed_cap(monkeypatch):
    entered = 0
    both_entered = asyncio.Event()
    release = asyncio.Event()
    real_spend = Decimal(0)

    async def handler(request):
        nonlocal entered, real_spend
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [INFO]})
        entered += 1
        if entered == 2:
            both_entered.set()
        await release.wait()
        real_spend += Decimal(".00014")
        assert real_spend <= guard.cap
        return httpx.Response(200, json=completion(0.00014))

    model, guard = bounded(monkeypatch, handler, cap=".080")
    tasks = [asyncio.create_task(model.complete([], {})) for _ in range(2)]
    try:
        await asyncio.wait_for(both_entered.wait(), timeout=2)
        assert len(guard.active) == 2
        assert guard.charged() + sum(guard.active.values()) <= guard.cap
        with pytest.raises(ModelCallError, match="reservation refused"):
            await model.complete([], {})
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert guard.charged() == real_spend
    # Settled calls release enough headroom for another bounded call.
    await model.complete([], {})
    assert guard.charged() == real_spend <= guard.cap


async def test_cap_refuses_before_paid_request(monkeypatch):
    async def forbidden(request):
        raise AssertionError("no HTTP request should be sent")

    model, guard = bounded(monkeypatch, forbidden, cap=".0001")
    with pytest.raises(ModelCallError, match="reservation refused") as error:
        await model.complete([], {})
    assert error.value.before_send
    assert guard.charged() == 0 and not guard.active


async def test_input_limit_refuses_before_paid_request(monkeypatch):
    async def forbidden(request):
        raise AssertionError("no HTTP request should be sent")

    model, guard = bounded(monkeypatch, forbidden)
    model.max_input_tokens = 1
    with pytest.raises(ModelCallError, match="input token limit refused") as error:
        await model.complete([], {})
    assert error.value.before_send
    assert guard.charged() == 0 and not guard.active


async def test_missing_cost_and_tokens_retains_uncertainty(monkeypatch):
    async def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [INFO]})
        if request.url.path.endswith("/generation"):
            return httpx.Response(404, json={"error": {"message": "unavailable"}})
        return httpx.Response(200, json=completion(counts=False))

    model, guard = bounded(monkeypatch, handler)
    await model.complete([], {})
    assert guard.unknown == Decimal(guard.records[0]["reservation"])
    assert not guard.active


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), -1, True])
async def test_generation_lookup_rejects_invalid_cost(monkeypatch, cost):
    async def handler(request):
        # Use raw JSON to exercise nonfinite provider data as well.
        return httpx.Response(200, content=json.dumps({"data": {"total_cost": cost}}))

    model, _ = bounded(monkeypatch, handler)
    assert await model.adapter.generation_cost("gen-synthetic") is None


async def test_timeout_after_success_headers_keeps_reservation(monkeypatch):
    class InterruptedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"id":'
            raise httpx.ReadTimeout("interrupted billed response")

    async def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [INFO]})
        return httpx.Response(200, stream=InterruptedBody())

    model, guard = bounded(monkeypatch, handler)
    with pytest.raises(ModelCallError) as error:
        await model.complete([], {})
    assert not error.value.before_response
    assert guard.unknown > 0 and not guard.active


def test_suite_default_budget_and_override():
    import argparse

    from incident_investigator.grading.suite import add_commands

    parser = argparse.ArgumentParser()
    add_commands(parser.add_subparsers(dest="command"))
    args = ["eval-suite", "--models", MODEL, "--out", "unused"]
    assert parser.parse_args(args).budget_usd == "2.00"
    assert parser.parse_args([*args, "--budget-usd", ".50"]).budget_usd == ".50"
