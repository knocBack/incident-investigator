"""One bounded chat-completions request; no SDK retries or native tools."""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from incident_investigator.contracts import PAYLOADS, PROFILES
from incident_investigator.models.adapter import ModelCallError, ModelResult, ModelUsage

BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"


# Keep the rendered request and spend reservation on the same output ceiling.
MAX_OUTPUT_TOKENS = 16384


def output_limit(info):
    cap = info.get("top_provider", {}).get("max_completion_tokens")
    return min(MAX_OUTPUT_TOKENS, cap) if isinstance(cap, int) and cap > 0 else MAX_OUTPUT_TOKENS


def retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def backoff(retry: int, retry_after: float | None = None, *, rate_limited=False) -> float:
    floor = 4 if rate_limited and retry_after is None else 1
    return max(retry_after or 0, min(30.0, floor * 2.0**retry) + random.uniform(0, 0.5))


class ProviderCooldown:
    """One suite-local cooldown; serialize wakeups after upstream congestion."""

    def __init__(self):
        self.until = 0.0
        self._lock = asyncio.Lock()
        self.revision = 0

    def defer(self, seconds: float):
        self.until = max(self.until, time.monotonic() + seconds)
        self.revision += 1

    async def wait(self, *, deadline: float | None = None):
        waited = False
        while True:
            async with self._lock:
                now = time.monotonic()
                delay = self.until - now
                if delay <= 0:
                    # Only callers that actually waited pace the next queued caller.
                    self.until = now + 0.1 + random.uniform(0, 0.15) if waited else 0.0
                    return self.revision
                if deadline is not None and self.until >= deadline:
                    raise ModelCallError(
                        f"Provider cooldown ({delay:.3g}s) cannot fit remaining run wall time ({max(0, deadline - now):.3g}s)",
                        retryable=False,
                        status=429,
                        before_send=True,
                        failure_class="rate_limited",
                        retry_after=delay,
                    )
            # Do not hold the lock while sleeping: a shorter-lived run must be able
            # to inspect the same cooldown and refuse immediately.
            await asyncio.sleep(delay)
            waited = True


def _role(messages: Sequence[Mapping[str, Any]]) -> str:
    for message in messages:
        if message.get("role") != "user":
            continue
        try:
            context = json.loads(message.get("content", ""))
        except (ValueError, TypeError):
            continue
        if isinstance(context, dict) and context.get("role") in PAYLOADS:
            return context["role"]
    return "orchestrator"


class OpenRouterModel:
    """Resolve role models from env; credentials enter Authorization headers only."""

    def __init__(
        self,
        *,
        timeout_seconds: float = PROFILES["demo-live"].call_seconds,
        transport: httpx.AsyncBaseTransport | None = None,
        cooldown: ProviderCooldown | None = None,
    ) -> None:
        self._key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not self._key:
            raise ValueError("OPENROUTER_API_KEY is required for --model openrouter")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("OpenRouter timeout must be finite and positive")
        self.timeout_seconds, self._transport = timeout_seconds, transport
        default = os.environ.get("II_OPENROUTER_MODEL", DEFAULT_MODEL)
        self._models = {
            role: os.environ.get(f"II_OPENROUTER_MODEL_{role.upper()}", default).strip()
            for role in PAYLOADS
        }
        if any(
            not re.fullmatch(r"[\w.-]+/[\w.:-]+", model) or self._key in model
            for model in self._models.values()
        ):
            raise ValueError("OpenRouter model names must be organization/model IDs")
        self._free_only = os.environ.get("II_OPENROUTER_FREE_ONLY") == "1"
        self._catalog: dict[str, dict[str, Any]] | None = None
        self._catalog_lock = asyncio.Lock()
        self._schema_rejected: set[str] = set()
        self.cooldown = cooldown or ProviderCooldown()

    async def wait_ready(self, *, deadline=None):
        return await self.cooldown.wait(deadline=deadline)

    def cooldown_ready(self, ticket):
        return ticket == self.cooldown.revision

    def model_for_role(self, role: str) -> str:
        return self._models[role]

    def _redact(self, text: str) -> str:
        return text.replace(self._key, "[redacted]")

    def _response(self, response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        if response.is_error or error:
            code = error.get("code") if isinstance(error, dict) else None
            status = code if isinstance(code, int) else response.status_code
            if status < 400:
                status = 502
            message = error.get("message") if isinstance(error, dict) else None
            detail = error.get("detail") if isinstance(error, dict) else None
            if detail:
                message = f"{message or 'provider error'}: {detail}"
            metadata = error.get("metadata", {}) if isinstance(error, dict) else {}
            raw = metadata.get("raw") if isinstance(metadata, dict) else None
            if isinstance(raw, str):
                try:
                    detail = json.loads(raw)
                except ValueError:
                    detail = raw
                if isinstance(detail, dict):
                    nested = detail.get("error", detail)
                    detail = (
                        ": ".join(
                            str(nested[key]) for key in ("message", "detail") if nested.get(key)
                        )
                        if isinstance(nested, dict)
                        else nested
                    )
                if isinstance(detail, str) and detail:
                    message = f"{message or 'provider error'}: {detail}"
            message = self._redact(str(message or "provider returned an error"))[:1000]
            label = {
                401: "authentication failed",
                402: "insufficient credits",
                429: "rate limited",
            }.get(status, "request failed")
            lower = message.lower()
            permanent = status in {401, 402, 403} or any(
                text in lower
                for text in (
                    "unknown model",
                    "invalid model",
                    "not a valid model",
                    "model not found",
                    "does not exist",
                    "not found for account",
                )
            )
            transient = not permanent and (
                status in {408, 429}
                or status >= 500
                or (
                    status == 404
                    and any(
                        text in lower for text in ("provider returned error", "no endpoints found")
                    )
                )
                or any(
                    text in lower
                    for text in ("overloaded", "temporarily unavailable", "connection reset")
                )
            )
            failure_class = (
                "rate_limited"
                if status == 429
                else "timeout"
                if status == 408
                else "provider_error"
            )
            raise ModelCallError(
                f"OpenRouter HTTP {status} ({label}): {message}",
                retryable=transient,
                failure_class=failure_class,
                retry_after=retry_after_seconds(response.headers.get("Retry-After")),
                status=status,
                before_response=True,
            )
        if not isinstance(body, dict):
            raise ModelCallError(
                "OpenRouter returned an invalid response envelope",
                retryable=True,
                failure_class="provider_error",
            )
        return body

    async def _model_info(self, client: httpx.AsyncClient, model: str) -> dict:
        async with self._catalog_lock:
            if self._catalog is None:
                # Public metadata needs no key; cache it for all turns in this adapter/run.
                body = self._response(await client.get(f"{BASE_URL}/models"))
                entries = body.get("data")
                if not isinstance(entries, list):
                    raise ModelCallError(
                        "OpenRouter returned an invalid model catalogue",
                        retryable=True,
                        before_send=True,
                        failure_class="provider_error",
                    )
                self._catalog = {
                    m["id"]: m
                    for m in entries
                    if isinstance(m, dict) and isinstance(m.get("id"), str)
                }
        info = self._catalog.get(model)
        if info is None:
            raise ModelCallError(
                f"Unknown OpenRouter model: {model}",
                retryable=False,
                before_send=True,
                failure_class="provider_error",
            )
        if self._free_only:
            pricing = info.get("pricing", {})
            if (
                not model.endswith(":free")
                or str(pricing.get("prompt")) != "0"
                or str(pricing.get("completion")) != "0"
            ):
                raise ModelCallError(
                    "OpenRouter free-only mode refused a non-free model",
                    retryable=False,
                    before_send=True,
                    failure_class="provider_error",
                )
        return info

    @staticmethod
    def build_request(
        messages: Sequence[Mapping[str, Any]], schema: dict, model: str, *, structured: bool
    ) -> dict:
        rendered = [dict(message) for message in messages]
        if structured:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "incident_action",
                    "strict": True,
                    "schema": schema,
                },
            }
        else:
            response_format = {"type": "json_object"}
            rendered.append(
                {
                    "role": "user",
                    "content": "Return only a JSON object matching this action schema. No native tools exist. "
                    "To retrieve evidence return a tools action. Schema: " + json.dumps(schema),
                }
            )
        return {
            "model": model,
            "messages": rendered,
            "response_format": response_format,
            "stream": False,
            "usage": {"include": True},
            "max_tokens": MAX_OUTPUT_TOKENS,
            "provider": {"require_parameters": True, "allow_fallbacks": True},
        }

    async def complete(self, messages: Sequence[Mapping[str, Any]], schema: dict) -> ModelResult:
        model = self.model_for_role(_role(messages))
        started = time.perf_counter()
        received_response = False
        try:
            async with (
                asyncio.timeout(self.timeout_seconds),
                httpx.AsyncClient(
                    timeout=httpx.Timeout(self.timeout_seconds),
                    transport=self._transport,
                    follow_redirects=False,
                ) as client,
            ):
                info = await self._model_info(client, model)
                structured = (
                    "structured_outputs" in info.get("supported_parameters", [])
                    and model not in self._schema_rejected
                )
                reasoning = info.get("reasoning")

                async def post(structured: bool) -> dict:
                    nonlocal received_response
                    request = self.build_request(messages, schema, model, structured=structured)
                    if (
                        "reasoning" in info.get("supported_parameters", [])
                        and isinstance(reasoning, dict)
                        and reasoning.get("mandatory") is False
                    ):
                        request["reasoning"] = {"enabled": False}
                    request["max_tokens"] = output_limit(info)
                    async with client.stream(
                        "POST",
                        f"{BASE_URL}/chat/completions",
                        headers={"Authorization": f"Bearer {self._key}"},
                        json=request,
                    ) as response:
                        received_response = response.is_success
                        await response.aread()
                    return self._response(response)

                try:
                    body = await post(structured)
                except ModelCallError as exc:
                    if (
                        structured
                        and exc.status == 400
                        and any(
                            word in str(exc).lower()
                            for word in ("schema", "structured", "response_format")
                        )
                    ):
                        # The next runtime call uses JSON mode, with its own allowance
                        # and reservation. Never hide a second POST in this call.
                        self._schema_rejected.add(model)
                        exc.retryable = True
                    if exc.retryable:
                        self.cooldown.defer(
                            backoff(
                                0, exc.retry_after, rate_limited=exc.failure_class == "rate_limited"
                            )
                        )
                    raise
        except (TimeoutError, httpx.TimeoutException):
            self.cooldown.defer(backoff(0))
            raise ModelCallError(
                f"OpenRouter model call timed out after {self.timeout_seconds:g}s for role {_role(messages)}",
                failure_class="timeout",
                retryable=True,
                before_response=not received_response,
            ) from None
        except httpx.HTTPError as exc:
            self.cooldown.defer(backoff(0))
            raise ModelCallError(
                f"OpenRouter transport failure for role {_role(messages)}: {type(exc).__name__}",
                failure_class="provider_error",
                retryable=True,
                before_response=not received_response,
            ) from None
        usage = body.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}

        def measured(name, *, integer=False):
            value = usage.get(name)
            if (
                not isinstance(value, int | float)
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
                or (integer and not isinstance(value, int))
            ):
                return None
            return value

        choices = body.get("choices")
        if (
            not isinstance(choices, list)
            or not choices
            or not isinstance(choices[0], dict)
            or not isinstance(choices[0].get("message"), dict)
        ):
            raise ModelCallError(
                "OpenRouter returned an invalid response message",
                retryable=True,
                failure_class="provider_error",
            )
        message = choices[0]["message"]
        content = self._redact(str(message.get("content") or ""))
        try:
            action = json.loads(content)
        except ValueError:
            action = None
        if not isinstance(action, dict) or message.get("tool_calls"):
            # Invalid output is a correction turn, not a hidden provider retry. Retain usage.
            action = {"kind": "invalid", "raw_output": content[:16384]}
        action = json.loads(self._redact(json.dumps(action)))
        return ModelResult(
            action=action,
            output_limit=output_limit(info),
            usage=ModelUsage(
                measured("prompt_tokens", integer=True),
                measured("completion_tokens", integer=True),
                measured("cost"),
            ),
            latency_ms=(time.perf_counter() - started) * 1000,
            raw_id=self._redact(str(body["id"])) if body.get("id") else None,
            model=model,
            finish_reason=self._redact(str(choices[0]["finish_reason"]))[:100]
            if choices[0].get("finish_reason") is not None
            else None,
        )

    async def generation_cost(self, generation_id: str) -> float | None:
        """Best-effort cost lookup for a completed response without usage."""
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, transport=self._transport
            ) as client:
                response = await client.get(
                    f"{BASE_URL}/generation",
                    params={"id": generation_id},
                    headers={"Authorization": f"Bearer {self._key}"},
                )
                body = self._response(response)
        except (ModelCallError, httpx.HTTPError, TimeoutError):
            return None
        data = body.get("data", body)
        cost = data.get("total_cost", data.get("cost")) if isinstance(data, dict) else None
        return (
            cost
            if isinstance(cost, int | float)
            and not isinstance(cost, bool)
            and math.isfinite(cost)
            and cost >= 0
            else None
        )
