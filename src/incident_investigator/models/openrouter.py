"""One bounded chat-completions request; no SDK retries or native tools."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from incident_investigator.contracts import PAYLOADS, PROFILES
from incident_investigator.models.adapter import ModelCallError, ModelResult, ModelUsage

BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"


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

    def model_for_role(self, role: str) -> str:
        return self._models[role]

    def _redact(self, text: str) -> str:
        return text.replace(self._key, "[redacted]")

    def _response(self, response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError:
            body = {}
        error = body.get("error") if isinstance(body, dict) else None
        if response.is_error or error:
            code = error.get("code") if isinstance(error, dict) else None
            status = code if isinstance(code, int) else response.status_code
            if status < 400:
                status = 502
            message = error.get("message") if isinstance(error, dict) else None
            metadata = error.get("metadata", {}) if isinstance(error, dict) else {}
            raw = metadata.get("raw") if isinstance(metadata, dict) else None
            if isinstance(raw, str):
                try:
                    detail = json.loads(raw)
                except ValueError:
                    detail = raw
                if isinstance(detail, dict):
                    nested = detail.get("error", detail)
                    detail = nested.get("message") if isinstance(nested, dict) else nested
                if isinstance(detail, str) and detail:
                    message = f"{message or 'provider error'}: {detail}"
            message = self._redact(str(message or "provider returned an error"))[:1000]
            label = {
                401: "authentication failed",
                402: "insufficient credits",
                429: "rate limited",
            }.get(status, "request failed")
            raise ModelCallError(
                f"OpenRouter HTTP {status} ({label}): {message}",
                retryable=status in {408, 429} or status >= 500,
                status=status,
            )
        if not isinstance(body, dict):
            raise ModelCallError("OpenRouter returned an invalid response envelope", retryable=True)
        return body

    async def _model_info(self, client: httpx.AsyncClient, model: str) -> dict:
        async with self._catalog_lock:
            if self._catalog is None:
                # Public metadata needs no key; cache it for all turns in this adapter/run.
                body = self._response(await client.get(f"{BASE_URL}/models"))
                entries = body.get("data")
                if not isinstance(entries, list):
                    raise ModelCallError(
                        "OpenRouter returned an invalid model catalogue", retryable=True
                    )
                self._catalog = {
                    m["id"]: m
                    for m in entries
                    if isinstance(m, dict) and isinstance(m.get("id"), str)
                }
        info = self._catalog.get(model)
        if info is None:
            raise ModelCallError(f"Unknown OpenRouter model: {model}", retryable=False)
        if self._free_only:
            pricing = info.get("pricing", {})
            if (
                not model.endswith(":free")
                or str(pricing.get("prompt")) != "0"
                or str(pricing.get("completion")) != "0"
            ):
                raise ModelCallError(
                    "OpenRouter free-only mode refused a non-free model", retryable=False
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
            "max_tokens": 4096,
            "provider": {"require_parameters": True, "allow_fallbacks": False},
        }

    async def complete(self, messages: Sequence[Mapping[str, Any]], schema: dict) -> ModelResult:
        model = self.model_for_role(_role(messages))
        started = time.perf_counter()
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
                    request = self.build_request(messages, schema, model, structured=structured)
                    if (
                        "reasoning" in info.get("supported_parameters", [])
                        and isinstance(reasoning, dict)
                        and reasoning.get("mandatory") is False
                    ):
                        request["reasoning"] = {"enabled": False}
                    return self._response(
                        await client.post(
                            f"{BASE_URL}/chat/completions",
                            headers={"Authorization": f"Bearer {self._key}"},
                            json=request,
                        )
                    )

                try:
                    body = await post(structured)
                except ModelCallError as exc:
                    if not structured or exc.status != 400:
                        raise
                    # Providers may reject a large strict schema (complexity limits). JSON mode
                    # keeps the same runtime validation; remember it for this model and run.
                    self._schema_rejected.add(model)
                    body = await post(False)
        except (TimeoutError, httpx.TimeoutException):
            raise ModelCallError("OpenRouter request timed out", retryable=True) from None
        except httpx.HTTPError as exc:
            raise ModelCallError(
                f"OpenRouter transport failure: {type(exc).__name__}", retryable=True
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

        choices = body.get("choices") or []
        message = choices[0].get("message", {}) if choices and isinstance(choices[0], dict) else {}
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
            usage=ModelUsage(
                measured("prompt_tokens", integer=True),
                measured("completion_tokens", integer=True),
                measured("cost"),
            ),
            latency_ms=(time.perf_counter() - started) * 1000,
            raw_id=self._redact(str(body["id"])) if body.get("id") else None,
            model=model,
        )
