from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from incident_investigator.contracts import PAYLOADS
from incident_investigator.models import ModelCallError, OpenRouterModel, ScriptedModel
from incident_investigator.models.openrouter import DEFAULT_MODEL
from incident_investigator.runtime import investigate, provider_schema
from incident_investigator.sources.loader import load_snapshot

TEST_KEY = "placeholder-for-mocked-provider"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", TEST_KEY)
    monkeypatch.setenv("II_OPENROUTER_MODEL", "test/schema:free")
    monkeypatch.delenv("II_OPENROUTER_FREE_ONLY", raising=False)
    for role in PAYLOADS:
        monkeypatch.delenv(f"II_OPENROUTER_MODEL_{role.upper()}", raising=False)


def catalogue():
    return {
        "data": [
            {
                "id": "test/schema:free",
                "supported_parameters": ["structured_outputs", "response_format", "reasoning"],
                "reasoning": {"mandatory": False},
                "pricing": {"prompt": "0", "completion": "0"},
            },
            {
                "id": "test/json:free",
                "supported_parameters": ["response_format"],
                "pricing": {"prompt": "0", "completion": "0"},
            },
        ]
    }


def messages(role="reasoning"):
    return [
        {"role": "system", "content": "Return a JSON action"},
        {"role": "user", "content": json.dumps({"role": role})},
    ]


def response(content='{"action":{"kind":"final","result":{}}}'):
    return {
        "id": "gen-mocked",
        "model": "test/schema:free",
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7, "cost": 0.001},
    }


@pytest.mark.parametrize("structured", [True, False])
async def test_request_modes_usage_and_generation_id(configured, monkeypatch, structured):
    selected = "test/schema:free" if structured else "test/json:free"
    monkeypatch.setenv("II_OPENROUTER_MODEL", selected)
    captured = []

    async def handler(request):
        captured.append(request)
        if request.method == "GET":
            assert "authorization" not in request.headers
            return httpx.Response(200, json=catalogue())
        assert request.headers["authorization"] == f"Bearer {TEST_KEY}"
        return httpx.Response(200, json=response())

    model = OpenRouterModel(transport=httpx.MockTransport(handler))
    result = await model.complete(messages(), provider_schema("reasoning"))
    await model.complete(messages(), provider_schema("reasoning"))
    assert len([request for request in captured if request.method == "GET"]) == 1
    body = json.loads(captured[1].content)
    assert body["model"] == selected
    assert "tools" not in body and "tool_choice" not in body
    assert body["provider"] == {"require_parameters": True, "allow_fallbacks": False}
    assert body["response_format"]["type"] == ("json_schema" if structured else "json_object")
    if structured:
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["reasoning"] == {"enabled": False}
    else:
        assert "Schema:" in body["messages"][-1]["content"]
    assert TEST_KEY not in json.dumps(body)
    assert result.usage.input_tokens == 12 and result.usage.output_tokens == 7
    assert result.usage.cost == 0.001
    assert result.raw_id == "gen-mocked" and result.model == selected and result.latency_ms >= 0


@pytest.mark.parametrize("reasoning", [{"mandatory": True}, {}, None])
async def test_mandatory_or_unknown_reasoning_uses_provider_default(configured, reasoning):
    def handler(request):
        if request.method == "GET":
            body = catalogue()
            body["data"][0].pop("reasoning")
            if reasoning is not None:
                body["data"][0]["reasoning"] = reasoning
            return httpx.Response(200, json=body)
        assert "reasoning" not in json.loads(request.content)
        return httpx.Response(200, json=response())

    result = await OpenRouterModel(transport=httpx.MockTransport(handler)).complete(messages(), {})
    assert result.usage.cost == 0.001


@pytest.mark.parametrize("code,retryable", [(401, False), (402, False), (429, True), (503, True)])
async def test_http_failures_are_clear_redacted_and_not_retried(configured, code, retryable):
    calls = 0

    def handler(request):
        nonlocal calls
        if request.method == "GET":
            return httpx.Response(200, json=catalogue())
        calls += 1
        return httpx.Response(
            code, json={"error": {"code": code, "message": f"provider detail {TEST_KEY}"}}
        )

    model = OpenRouterModel(transport=httpx.MockTransport(handler))
    with pytest.raises(ModelCallError) as exc:
        await model.complete(messages(), {})
    assert exc.value.retryable is retryable
    assert f"OpenRouter HTTP {code}" in str(exc.value) and "provider detail" in str(exc.value)
    assert TEST_KEY not in str(exc.value)
    assert calls == 1


async def test_embedded_429_is_retryable(configured):
    def handler(request):
        return httpx.Response(
            200,
            json=catalogue()
            if request.method == "GET"
            else {"error": {"code": 429, "message": "free tier rate limit"}},
        )

    with pytest.raises(ModelCallError, match="free tier rate limit") as exc:
        await OpenRouterModel(transport=httpx.MockTransport(handler)).complete(messages(), {})
    assert exc.value.retryable


async def test_hard_timeout_has_no_hidden_retry(configured):
    calls = 0

    async def handler(request):
        nonlocal calls
        if request.method == "GET":
            return httpx.Response(200, json=catalogue())
        calls += 1
        await asyncio.Event().wait()

    with pytest.raises(ModelCallError, match="timed out"):
        await OpenRouterModel(
            timeout_seconds=0.01, transport=httpx.MockTransport(handler)
        ).complete(messages(), {})
    assert calls == 1


@pytest.mark.parametrize("content", ["not JSON", "[]"])
async def test_invalid_output_reaches_runtime_correction_with_usage(configured, content):
    def handler(request):
        return httpx.Response(
            200, json=catalogue() if request.method == "GET" else response(content)
        )

    result = await OpenRouterModel(transport=httpx.MockTransport(handler)).complete(messages(), {})
    assert result.action["kind"] == "invalid"
    assert result.usage.input_tokens == 12


def test_default_and_role_override_selection(configured, monkeypatch):
    monkeypatch.delenv("II_OPENROUTER_MODEL")
    monkeypatch.setenv("II_OPENROUTER_MODEL_OBSERVABILITY", "test/json:free")
    model = OpenRouterModel()
    assert model.model_for_role("observability") == "test/json:free"
    assert model.model_for_role("critic") == DEFAULT_MODEL


def test_key_is_required_from_documented_env_only(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY is required"):
        OpenRouterModel()


async def test_free_only_refuses_paid_model_before_post(configured, monkeypatch):
    monkeypatch.setenv("II_OPENROUTER_FREE_ONLY", "1")
    monkeypatch.setenv("II_OPENROUTER_MODEL", "test/paid")

    def handler(request):
        assert request.method == "GET"
        return httpx.Response(
            200, json={"data": [{"id": "test/paid", "pricing": {"prompt": "1", "completion": "1"}}]}
        )

    with pytest.raises(ModelCallError, match="free-only"):
        await OpenRouterModel(transport=httpx.MockTransport(handler)).complete(messages(), {})


async def test_mocked_api_investigation_records_role_models_cost_and_no_credentials(
    configured, monkeypatch, tmp_path
):
    monkeypatch.setenv("II_OPENROUTER_MODEL_OBSERVABILITY", "test/json:free")
    script = ScriptedModel(json.loads(Path("tests/scripted-demo.json").read_text()))
    calls = []

    async def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=catalogue())
        body = json.loads(request.content)
        calls.append(body)
        result = await script.complete(body["messages"], {})
        return httpx.Response(200, json=response(json.dumps(result.action)))

    directory = await investigate(
        load_snapshot("dev-00"), OpenRouterModel(transport=httpx.MockTransport(handler)), tmp_path
    )
    result = json.loads((directory / "result.json").read_text())
    assert result["status"] == "completed"
    assert result["models"]["observability"] == "test/json:free"
    assert result["models"]["critic"] == "test/schema:free"
    assert result["usage"]["observability"]["model"] == "test/json:free"
    assert result["usage"]["observability"]["cost"] == 0.002
    assert all(
        row.get("raw_id") == "gen-mocked" for row in result["receipts"].values() if row.get("model")
    )
    assert "test/json:free" in (directory / "report.md").read_text()
    assert "Cost (provider)" in (directory / "report.md").read_text()
    for path in directory.iterdir():
        assert TEST_KEY.encode() not in path.read_bytes()
    assert len(calls) == 8


@pytest.mark.parametrize("code,expected_calls", [(401, 2), (429, 4)])
async def test_runtime_owns_retry_policy_and_scrubs_provider_errors(
    configured, tmp_path, code, expected_calls
):
    calls = 0

    def handler(request):
        nonlocal calls
        if request.method == "GET":
            return httpx.Response(200, json=catalogue())
        calls += 1
        return httpx.Response(code, json={"error": {"code": code, "message": TEST_KEY}})

    directory = await investigate(
        load_snapshot("dev-00"), OpenRouterModel(transport=httpx.MockTransport(handler)), tmp_path
    )
    result = json.loads((directory / "result.json").read_text())
    assert result["status"] == "failed"  # Reporting also receives the provider failure.
    assert calls == expected_calls
    for path in directory.iterdir():
        assert TEST_KEY.encode() not in path.read_bytes()


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("II_LIVE_OPENROUTER") != "1", reason="explicit opt-in OpenRouter live test"
)
async def test_live_openrouter_free_json(monkeypatch):
    monkeypatch.setenv("II_OPENROUTER_FREE_ONLY", "1")
    model = OpenRouterModel()
    assert all(model.model_for_role(role).endswith(":free") for role in PAYLOADS)
    result = await model.complete(
        [{"role": "user", "content": "Return a JSON object with ok true."}],
        {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
    )
    assert result.action["ok"] is True


async def test_every_role_prompt_explains_json_registry_requests(tmp_path):
    model = ScriptedModel(json.loads(Path("tests/scripted-demo.json").read_text()))
    await investigate(load_snapshot("dev-00"), model, tmp_path)
    assert len(model.requests) == 8
    roles = set()
    for rendered, _ in model.requests:
        context = json.loads(rendered[1]["content"])
        roles.add(context["role"])
        assert "NO native or callable tools" in rendered[0]["content"]
        assert "Registry names and argument schemas:" in rendered[0]["content"]
        for name in context["tools"]:
            assert name in rendered[0]["content"]
    assert roles == set(PAYLOADS)


async def test_upstream_rate_limit_detail_is_surfaced_and_redacted(configured):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=catalogue())
        return httpx.Response(
            429,
            json={
                "error": {
                    "code": 429,
                    "message": "Provider returned error",
                    "metadata": {
                        "raw": json.dumps(
                            {"error": {"message": f"upstream quota exhausted: {TEST_KEY}"}}
                        )
                    },
                }
            },
        )

    with pytest.raises(ModelCallError, match="upstream quota exhausted") as exc:
        await OpenRouterModel(transport=httpx.MockTransport(handler)).complete(messages(), {})
    assert exc.value.retryable
    assert TEST_KEY not in str(exc.value)


async def test_last_specialist_response_schema_only_allows_final(tmp_path):
    from incident_investigator.contracts import Limits
    from incident_investigator.runtime import Call, Investigation

    model = ScriptedModel([{"kind": "final", "result": {}}])
    runner = Investigation(load_snapshot("dev-00"), model, tmp_path, Limits(), asyncio.Semaphore(1))
    call = Call(
        call_id="last",
        task_id="task",
        attempt_id="attempt",
        role="observability",
        kind="model",
        request={"messages": [], "remaining_model_calls_after_this": 0},
    )
    await runner.execute({"call": call.model_dump()})
    schema = model.requests[0][1]
    assert "ProviderTools" not in schema["$defs"]
    assert schema["properties"]["action"]["properties"]["kind"]["const"] == "final"
