"""Opt-in loopback Phoenix export. Local artifacts remain authoritative.

No SDK is imported and no network is used when tracing is disabled. Provider/tool
inputs are captured at their real call boundary, never evaluator definitions.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager, nullcontext, suppress
from functools import cache
from urllib.parse import quote, urlsplit

import httpx

from incident_investigator.graph_config import DEFAULT_GRAPH
from incident_investigator.workspace import write_json

_INSTRUMENTED = False
PROJECT = "incident-investigator"


def local_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Phoenix URL must be an unauthenticated loopback HTTP URL")
    return value.rstrip("/")


@cache
def provider(url):
    global _INSTRUMENTED
    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        raise ValueError("Phoenix tracing requires: uv sync --extra phoenix") from exc
    instance = TracerProvider(
        resource=Resource({"service.name": PROJECT, "openinference.project.name": PROJECT})
    )
    instance.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=url + "/v1/traces", timeout=2, headers={}),
            max_queue_size=2048,
            max_export_batch_size=128,
            export_timeout_millis=2000,
        )
    )
    LangChainInstrumentor().instrument(tracer_provider=instance)
    _INSTRUMENTED = True
    return instance


class TraceSession:
    def __init__(
        self, run_id, case_id, *, graph=DEFAULT_GRAPH, system="multi", enabled=None, tracer=None
    ):
        self.enabled = enabled if enabled is not None else os.environ.get("II_PHOENIX") == "1"
        self.url = (
            local_url(os.environ.get("II_PHOENIX_URL", "http://127.0.0.1:6006"))
            if self.enabled
            else ""
        )
        self.provider = provider(self.url) if self.enabled and tracer is None else None
        self.tracer = tracer or (self.provider.get_tracer(PROJECT) if self.provider else None)
        self.attributes = {
            "run_id": run_id,
            "case_id": case_id,
            "graph": graph,
            "system": system,
            "session.id": run_id,
        }
        self.tasks = {}
        self.root = None
        self.context = nullcontext()

    def __enter__(self):
        if self.tracer:
            self.context = self.tracer.start_as_current_span(
                "investigation", attributes={**self.attributes, "openinference.span.kind": "CHAIN"}
            )
        elif _INSTRUMENTED:
            from openinference.instrumentation import suppress_tracing

            self.context = suppress_tracing()
        self.root = self.context.__enter__()
        return self

    def __exit__(self, *args):
        for span in self.tasks.values():
            span.end()
        self.tasks.clear()
        return self.context.__exit__(*args)

    def sync(self, control):
        if not self.tracer:
            return
        from opentelemetry.trace import set_span_in_context

        for task in control.tasks.values():
            if task.task_id not in self.tasks and task.status == "running":
                self.tasks[task.task_id] = self.tracer.start_span(
                    task.role,
                    context=set_span_in_context(self.root),
                    attributes={
                        **self.attributes,
                        "openinference.span.kind": "AGENT",
                        "role": task.role,
                        "task_id": task.task_id,
                        "attempt_id": task.attempt_id,
                        "input.value": task.question,
                        "input.mime_type": "text/plain",
                    },
                )
            if task.status != "running" and (span := self.tasks.pop(task.task_id, None)):
                span.set_attribute(
                    "output.value", json.dumps(task.result or {"failure": task.failure})
                )
                span.set_attribute("output.mime_type", "application/json")
                if task.failure:
                    from opentelemetry.trace import Status, StatusCode

                    span.set_status(Status(StatusCode.ERROR))
                    span.set_attribute("error.message", task.failure)
                span.end()

    @contextmanager
    def call(self, call):
        if not self.tracer:
            yield None
            return
        from opentelemetry.trace import set_span_in_context

        parent = self.tasks.get(call.task_id) or self.root
        attributes = {
            **self.attributes,
            "role": call.role,
            "task_id": call.task_id,
            "call_id": call.call_id,
            "attempt_id": call.attempt_id,
            "openinference.span.kind": "LLM" if call.kind == "model" else "TOOL",
        }
        if call.kind == "tool":
            attributes.update(
                {
                    "tool.name": call.request["name"],
                    "tool.parameters": json.dumps(call.request["arguments"]),
                }
            )
        with self.tracer.start_as_current_span(
            f"{call.role}.{call.kind}",
            context=set_span_in_context(parent),
            attributes=attributes,
        ) as span:
            yield span

    @staticmethod
    def input(span, value):
        if span:
            span.set_attribute("input.value", json.dumps(value, ensure_ascii=False))
            span.set_attribute("input.mime_type", "application/json")
            if isinstance(value, list):
                for i, message in enumerate(value):
                    span.set_attribute(f"llm.input_messages.{i}.message.role", message["role"])
                    span.set_attribute(
                        f"llm.input_messages.{i}.message.content", message["content"]
                    )

    @staticmethod
    def output(span, receipt):
        if not span:
            return
        payload = receipt.get("payload")
        span.set_attribute("output.value", json.dumps(payload, ensure_ascii=False))
        span.set_attribute("output.mime_type", "application/json")
        if receipt.get("model"):
            span.set_attribute("llm.model_name", receipt["model"])
            span.set_attribute("llm.output_messages.0.message.role", "assistant")
            span.set_attribute("llm.output_messages.0.message.content", json.dumps(payload))
        if receipt.get("error"):
            from opentelemetry.trace import Status, StatusCode

            span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("error.message", receipt["error"])
        for field, attr in (
            ("input_tokens", "llm.token_count.prompt"),
            ("output_tokens", "llm.token_count.completion"),
            ("cost", "llm.cost.total"),
        ):
            value = (receipt.get("usage") or {}).get(field)
            if value is not None:
                span.set_attribute(attr, value)
        if isinstance(payload, dict) and payload.get("observations"):
            span.set_attribute("evidence_ids", [r["evidence_id"] for r in payload["observations"]])

    def finish(self, control):
        if self.root and self.tracer:
            from opentelemetry.trace import Status, StatusCode

            self.root.set_status(
                Status(StatusCode.OK if control["status"] == "completed" else StatusCode.ERROR)
            )
            self.root.set_attribute("execution.status", control["status"])
            self.root.set_attribute("stop_reason", control.get("stop_reason") or "none")
            self.root.set_attribute("limits", json.dumps(control["limits"]))
            task = control["tasks"].get(control.get("report_task"), {})
            self.root.set_attribute("output.value", json.dumps(task.get("result") or {}))
            self.root.set_attribute("output.mime_type", "application/json")

    async def save_link(self, directory, *, final=True):
        if not self.tracer:
            return
        trace_id = format(self.root.get_span_context().trace_id, "032x")
        pointer = {
            "trace_id": trace_id,
            "run_id": self.attributes["run_id"],
            "project": PROJECT,
            "status": "recorded" if final else "recording",
            "url": None,
        }
        if self.provider:
            import asyncio

            try:
                await asyncio.to_thread(self.provider.force_flush, timeout_millis=2000)
                # Phoenix ingests asynchronously after acknowledging OTLP. A fresh
                # project can briefly return 404 even though export already succeeded.
                async with asyncio.timeout(2):
                    async with httpx.AsyncClient(
                        timeout=2, trust_env=False, follow_redirects=False
                    ) as client:
                        for attempt in range(6):
                            response = await client.get(self.url + "/v1/projects/" + PROJECT)
                            if response.status_code == 404 and not final:
                                # Resolve/create the local project before the first call,
                                # so the trace URL is visible while its run is ongoing.
                                response = await client.post(
                                    self.url + "/v1/projects", json={"name": PROJECT}
                                )
                            if response.status_code in {404, 409} and attempt < 5:
                                await asyncio.sleep(0.2)
                                continue
                            response.raise_for_status()
                            project_id = response.json()["data"]["id"]
                            pointer["url"] = (
                                self.url
                                + f"/projects/{quote(project_id, safe='')}/traces/{trace_id}"
                            )
                            break
            except Exception:
                pointer["status"] = "collector unavailable or project not resolved"
        # An optional trace pointer must never prevent the local investigation.
        with suppress(OSError):
            write_json(directory / "phoenix.json", pointer)
