"""Regressions for observed provider failures, separate from diagnosis grading.

The former adapter hid retry POSTs inside one call/reservation, and surfaced 404
provider failures and bare timeouts poorly. These mocked requests exercise limits,
accounting, failure summaries, and the preflight without live provider dependence.
"""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from incident_investigator.contracts import Limits
from incident_investigator.grading.budget import BoundedModel, SpendGuard
from incident_investigator.grading.suite import leaderboard
from incident_investigator.models import ModelCallError, OpenRouterModel, ScriptedModel
from incident_investigator.models.openrouter import (
    MAX_OUTPUT_TOKENS,
    ProviderCooldown,
    backoff,
    retry_after_seconds,
)
from incident_investigator.preflight import MIN_FREE_BYTES, suite_preflight
from incident_investigator.runtime import Call, Control, Investigation, investigate
from incident_investigator.sources.loader import load_snapshot

MODEL = "test/provider"
INFO = {
    "id": MODEL,
    "context_length": 64000,
    "supported_parameters": ["structured_outputs"],
    "top_provider": {"max_completion_tokens": 65536},
    "pricing": {"prompt": "0.000001", "completion": "0.000002"},
}


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-provider-key")
    monkeypatch.setenv("II_OPENROUTER_MODEL", MODEL)
    monkeypatch.delenv("II_OPENROUTER_FREE_ONLY", raising=False)
    return [{"role": "user", "content": '{"role":"single"}'}]


@pytest.mark.parametrize(
    "status,message,retryable,kind",
    [
        (429, "Rate limit exceeded", True, "rate_limited"),
        (404, "Provider returned error", True, "provider_error"),
        (404, "No endpoints found", True, "provider_error"),
        (404, "Unknown model", False, "provider_error"),
        (400, "Provider overloaded", True, "provider_error"),
        (502, "Upstream failed", True, "provider_error"),
        (503, "Service unavailable", True, "provider_error"),
        (408, "Provider timeout", True, "timeout"),
        (401, "Invalid authentication", False, "provider_error"),
        (402, "Insufficient credits", False, "provider_error"),
    ],
)
async def test_classification_has_one_post_per_call(provider, status, message, retryable, kind):
    posts = []

    def handler(request):
        posts.append(request)
        return httpx.Response(status, json={"error": {"code": status, "message": message}})

    adapter = OpenRouterModel(transport=httpx.MockTransport(handler))
    adapter._catalog = {MODEL: INFO}
    with pytest.raises(ModelCallError, match=message) as error:
        await adapter.complete(provider, {})
    assert len(posts) == 1
    assert error.value.retryable == retryable
    assert error.value.failure_class == kind


@pytest.mark.parametrize("header", [None, "3.5", "date", "invalid"])
async def test_retry_after_is_exposed_without_a_hidden_retry(provider, header):
    if header == "date":
        header = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)

    def handler(request):
        return httpx.Response(
            429,
            headers={"Retry-After": header} if header else {},
            json={"error": {"code": 429, "message": "Rate limit"}},
        )

    model = OpenRouterModel(transport=httpx.MockTransport(handler))
    model._catalog = {MODEL: INFO}
    with pytest.raises(ModelCallError) as error:
        await model.complete(provider, {})
    delay = error.value.retry_after
    if header and header.endswith("GMT"):
        assert 28 <= delay <= 30
    elif header == "3.5":
        assert delay == 3.5
    else:
        assert delay is None
    assert model.cooldown.until >= time.monotonic() + (delay or 0) - 0.1


def test_backoff_is_exponential_jittered_and_defensive():
    assert 1 <= backoff(0) <= 1.5
    assert 2 <= backoff(1) <= 2.5
    assert 40 <= backoff(1, 40) <= 40.1
    for value in ("nan", "inf", "garbage", ""):
        assert retry_after_seconds(value) is None
    assert retry_after_seconds("-2") == 0
    assert retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT") == 0


async def test_shared_cooldown_spaces_concurrent_attempts():
    cooldown = ProviderCooldown()
    cooldown.defer(0.025)
    started = time.monotonic()

    async def ready():
        await cooldown.wait()
        return time.monotonic() - started

    first, second, third = await asyncio.gather(ready(), ready(), ready())
    assert first >= 0.02
    assert second - first >= 0.09
    assert third - second >= 0.09


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError, httpx.ReadError])
async def test_transport_failure_is_clear_and_counted(provider, failure):
    def handler(request):
        raise failure("connection reset", request=request)

    adapter = OpenRouterModel(timeout_seconds=0.2, transport=httpx.MockTransport(handler))
    adapter._catalog = {MODEL: INFO}
    guard = SpendGuard(0, 1)
    model = BoundedModel(adapter, {MODEL: INFO}, guard)
    with pytest.raises(ModelCallError, match="single") as error:
        await model.complete(provider, {})
    assert error.value.failure_class == (
        "timeout" if failure is httpx.ReadTimeout else "provider_error"
    )
    assert len(guard.records) == 1 and not guard.active


@pytest.mark.parametrize("status", [429, 404, 503, 402])
async def test_runtime_retry_reservations_and_call_caps(provider, tmp_path, monkeypatch, status):
    # Backoff timing itself is tested separately; this checks real runtime/guard integration.
    monkeypatch.setattr("incident_investigator.runtime.backoff", lambda *args: 0)
    monkeypatch.setattr(ProviderCooldown, "defer", lambda *args: None)
    posts = 0

    def handler(request):
        nonlocal posts
        posts += 1
        return httpx.Response(
            status, json={"error": {"code": status, "message": "Provider returned error"}}
        )

    adapter = OpenRouterModel(transport=httpx.MockTransport(handler))
    adapter._catalog = {MODEL: INFO}
    guard = SpendGuard(0, 1)
    directory = await investigate(
        load_snapshot("dev-00"),
        BoundedModel(adapter, {MODEL: INFO}, guard),
        tmp_path,
        system="single",
        limits=Limits(max_model_calls=2, call_retries=2),
    )
    result = json.loads((directory / "result.json").read_text())
    count = 1 if status == 402 else 2
    assert posts == len(guard.records) == result["usage"]["single"]["model_calls"] == count
    calls = list(result["state"]["calls"].values())
    if count == 2:
        assert calls[1]["retry_of"] == calls[0]["call_id"]
    assert all(row["cost"] == "unbilled_failure" for row in guard.records)
    assert result["usage"]["single"]["cost"] == "unknown"
    kind = "rate_limited" if status == 429 else "provider_error"
    assert result["failure_counts"][kind] == count
    assert result["usage"]["single"]["failure_counts"][kind] == count


async def test_retry_after_cannot_exceed_remaining_run_wall_time(provider, tmp_path):
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "600"}, json={"error": {"code": 429}})

    adapter = OpenRouterModel(transport=httpx.MockTransport(handler))
    adapter._catalog = {MODEL: INFO}
    started = time.monotonic()
    directory = await investigate(
        load_snapshot("dev-00"),
        adapter,
        tmp_path,
        system="single",
        limits=Limits(wall_seconds=0.4, call_seconds=0.1),
    )
    result = json.loads((directory / "result.json").read_text())
    assert time.monotonic() - started < 3
    assert result["failure_counts"]["rate_limited"] == 1
    assert "Run wall timeout (0.4s)" in json.dumps(result)
    assert result["failure_counts"]["timeout"] >= 1


async def test_call_timeout_names_role_and_applied_limit(tmp_path):
    class Slow:
        async def complete(self, messages, schema):
            await asyncio.Event().wait()

    runner = Investigation(
        load_snapshot("dev-00"), Slow(), tmp_path, Limits(call_seconds=0.01), asyncio.Semaphore(1)
    )
    call = Call(
        call_id="slow",
        task_id="task",
        attempt_id="attempt",
        role="reporting",
        kind="model",
        request={"messages": []},
    )
    receipt = (await runner.execute({"call": call.model_dump()}))["inbox"]["slow"]
    assert receipt["failure_class"] == "timeout"
    assert "Model call timeout (0.01s) for role reporting" in receipt["error"]


async def test_output_cap_is_supported_and_recorded(provider):
    posts = []

    def handler(request):
        posts.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"action":'}, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 8192, "cost": 0.01},
            },
        )

    adapter = OpenRouterModel(transport=httpx.MockTransport(handler))
    adapter._catalog = {MODEL: {**INFO, "top_provider": {"max_completion_tokens": 8192}}}
    result = await adapter.complete(provider, {})
    assert MAX_OUTPUT_TOKENS == 16384
    assert posts[0]["max_tokens"] == result.output_limit == 8192
    assert result.finish_reason == "length" and result.usage.output_tokens == 8192


async def test_schema_fallback_is_a_separately_reserved_runtime_call(
    provider, tmp_path, monkeypatch
):
    monkeypatch.setattr("incident_investigator.runtime.backoff", lambda *args: 0)
    monkeypatch.setattr(ProviderCooldown, "defer", lambda *args: None)
    script = ScriptedModel(json.loads(Path("tests/scripted-paired.json").read_text())["single"])
    modes = []

    async def handler(request):
        body = json.loads(request.content)
        modes.append(body["response_format"]["type"])
        if len(modes) == 1:
            return httpx.Response(400, json={"error": {"message": "Schema too complex"}})
        action = await script.complete(body["messages"], {})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(action.action)}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0},
            },
        )

    adapter = OpenRouterModel(transport=httpx.MockTransport(handler))
    adapter._catalog = {MODEL: INFO}
    guard = SpendGuard(0, 1)
    directory = await investigate(
        load_snapshot("dev-00"),
        BoundedModel(adapter, {MODEL: INFO}, guard),
        tmp_path,
        system="single",
    )
    result = json.loads((directory / "result.json").read_text())
    assert result["status"] == "completed"
    assert modes == ["json_schema", "json_object", "json_object"]
    assert len(guard.records) == result["usage"]["single"]["model_calls"] == len(modes)
    assert result["failure_counts"]["provider_error"] == 1


async def test_budget_censoring_and_leaderboard_keep_failure_messages(provider, tmp_path):
    from incident_investigator.grading.grader import expectation, grade_run

    adapter = OpenRouterModel(transport=httpx.MockTransport(lambda r: pytest.fail("must not send")))
    guard = SpendGuard(0, 0)
    directory = await investigate(
        load_snapshot("dev-00"),
        BoundedModel(adapter, {MODEL: INFO}, guard),
        tmp_path,
        system="single",
    )
    result = json.loads((directory / "result.json").read_text())
    assert result["failure_counts"]["budget_censored"] == 1
    grade = grade_run(directory, expectation("bad_configuration"))
    grade["failures"] = result["failures"]
    board = leaderboard([grade], tmp_path / "board")
    assert board["models"][0]["failure_counts"]["budget_censored"] == 1
    assert (
        board["models"][0]["faults"]["bad_configuration"]["failure_counts"]["budget_censored"] == 1
    )
    assert "reservation refused" in board["runs"][0]["failures"][0]["message"]


async def test_provider_backoff_does_not_change_tool_retry_policy(tmp_path):
    runner = Investigation(
        load_snapshot("dev-00"), ScriptedModel(), tmp_path, Limits(), asyncio.Semaphore(1)
    )
    control = Control(run_id="run", case_id="dev-00", limits=Limits())
    task_id = runner.task(control, "observability", "retrieve")
    task = control.tasks[task_id]
    runner.admit(control, task, "tool", {"name": "catalog", "arguments": {}})
    call = control.calls[task.pending[0]]
    runner.accept(
        control,
        call,
        {"error": "intentional retrieval failure", "retryable": True, "retry_after": 600},
    )
    successor = control.calls[task.pending[0]]
    assert successor.retry == 1 and successor.not_before == 0
    assert task.failures[0]["kind"] == "tool"


@pytest.mark.parametrize("remaining", [0.1, 1, None])
async def test_preflight_checks_key_credit_models_and_disk(
    provider, tmp_path, monkeypatch, remaining
):
    monkeypatch.setattr(
        "incident_investigator.preflight.shutil.disk_usage",
        lambda p: SimpleNamespace(free=MIN_FREE_BYTES + 1),
    )
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"data": {"usage": 0.2, "limit_remaining": remaining}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await suite_preflight(client, [MODEL], {MODEL: INFO}, ".20", tmp_path)
    assert result["key_reachable"] and result["models_present"] == [MODEL]
    assert result["free_disk_bytes"] > MIN_FREE_BYTES
    assert bool(result["warnings"]) == (remaining != 1)
    assert calls == ["/api/v1/key"]


@pytest.mark.parametrize("failure", ["disk", "model", "auth"])
async def test_preflight_refuses_expected_failures(provider, tmp_path, monkeypatch, failure):
    monkeypatch.setattr(
        "incident_investigator.preflight.shutil.disk_usage",
        lambda p: SimpleNamespace(free=1 if failure == "disk" else MIN_FREE_BYTES + 1),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(401))
    ) as client:
        with pytest.raises(
            ValueError,
            match={"disk": "free disk", "model": "unknown model", "auth": "key check failed"}[
                failure
            ],
        ):
            await suite_preflight(
                client, [MODEL], {} if failure == "model" else {MODEL: INFO}, ".20", tmp_path
            )


async def test_console_preflight_includes_live_checks(provider, tmp_path, monkeypatch):
    from incident_investigator.console_jobs import RunRequest
    from incident_investigator.console_preflight import LaunchPreflight
    from incident_investigator.storage import repositories

    async def catalogue(self):
        return {MODEL: INFO}

    async def get(self, url, **kwargs):
        assert url.endswith("/key")
        return httpx.Response(200, json={"data": {"usage": 0, "limit_remaining": 0.1}})

    monkeypatch.setattr(LaunchPreflight, "catalogue", catalogue)
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    result = await LaunchPreflight().check(
        RunRequest(provider="openrouter", model_id=MODEL, faults=["case-02"], budget_usd=".20"),
        repositories(root=tmp_path),
    )
    assert result["launch_allowed"]
    assert result["preflight"]["key_reachable"]
    assert "cannot cover suite cap" in result["message"]
    assert "free disk" in result["message"]


async def test_final_key_sampling_failure_retains_suite_results(provider, tmp_path, monkeypatch):
    from incident_investigator import cli
    from incident_investigator.grading import suite

    async def preflight(*args, **kwargs):
        return {"usage": 0, "message": "Mocked preflight"}

    async def usage(client):
        raise httpx.ReadTimeout("key telemetry unavailable")

    async def get(self, url):
        return httpx.Response(200, request=httpx.Request("GET", url), json={"data": [INFO]})

    async def complete(self, messages, schema):
        raise ModelCallError(
            "Provider returned error",
            retryable=False,
            before_response=True,
            failure_class="provider_error",
        )

    monkeypatch.setattr(suite, "suite_preflight", preflight)
    monkeypatch.setattr(suite, "key_usage", usage)
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    monkeypatch.setattr(OpenRouterModel, "complete", complete)
    args = cli.build_parser().parse_args(
        [
            "eval-suite",
            "--models",
            MODEL,
            "--system",
            "both",
            "--faults",
            "case-02",
            "--budget-usd",
            "1",
            "--out",
            str(tmp_path / "suite"),
        ]
    )
    result = await suite.run_suite(args)
    assert len(result["runs"]) == 2
    assert all(g["failure_counts"]["provider_error"] > 0 for g in result["runs"])
    assert (args.out / "leaderboard.json").exists()
    after = json.loads((args.out / "spend-after.json").read_text())
    assert after["usage_delta"] is None
    assert len(json.loads((args.out / "key-usage-errors.json").read_text())) == 3


async def test_queued_dispatch_observes_cooldown_set_while_waiting_for_capacity(tmp_path):
    from incident_investigator.models.adapter import ModelResult

    cooldown = ProviderCooldown()
    completed = []

    class Model:
        async def wait_ready(self):
            await cooldown.wait()

        async def complete(self, messages, schema):
            completed.append(time.monotonic())
            return ModelResult({}, None, 0, None)

    capacity = asyncio.Semaphore(0)
    runner = Investigation(load_snapshot("dev-00"), Model(), tmp_path, Limits(), capacity)
    call = Call(
        call_id="queued",
        task_id="task",
        attempt_id="attempt",
        role="single",
        kind="model",
        request={"messages": []},
    )
    pending = asyncio.create_task(runner.execute({"call": call.model_dump()}))
    await asyncio.sleep(0.01)
    blocked_at = time.monotonic()
    cooldown.defer(0.03)
    capacity.release()
    await pending
    assert completed[0] - blocked_at >= 0.025
