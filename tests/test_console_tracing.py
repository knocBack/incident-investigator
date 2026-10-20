"""Phoenix is optional; enabled-path tests use an in-memory exporter, no service."""

import asyncio
import json
from argparse import Namespace
from pathlib import Path

import pytest

from incident_investigator import cli
from incident_investigator.telemetry.tracing import TraceSession, local_url


def test_tracing_disabled_imports_no_sdk(monkeypatch, tmp_path):
    import builtins

    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith(("opentelemetry", "openinference", "phoenix")):
            raise AssertionError("disabled tracing imported optional SDK")
        return original(name, *args, **kwargs)

    monkeypatch.setenv("II_PHOENIX", "0")
    monkeypatch.setattr(builtins, "__import__", guarded)
    with TraceSession("test", "case", system="single") as observer:
        assert observer.tracer is None
        assert observer.attributes["system"] == "single"
        assert observer.attributes["graph"] == "staged"
        asyncio.run(observer.save_link(tmp_path))
    assert not (tmp_path / "phoenix.json").exists()


def test_only_local_phoenix_urls():
    assert local_url("http://127.0.0.1:6006/") == "http://127.0.0.1:6006"
    for value in (
        "https://remote.example",
        "javascript:alert(1)",
        "http://localhost@evil.example",
        "http://user:pass@localhost:6006",
        "http://localhost:6006?key=secret",
    ):
        with pytest.raises(ValueError):
            local_url(value)


@pytest.mark.parametrize("failure", ["flush", "pointer"])
async def test_optional_trace_link_failures_are_contained(failure, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from incident_investigator.telemetry import tracing

    session = TraceSession("run", "case", enabled=True, tracer=object())
    session.root = SimpleNamespace(get_span_context=lambda: SimpleNamespace(trace_id=1))

    def fail_flush(**kwargs):
        raise RuntimeError("exporter failed")

    def fail_write(*args, **kwargs):
        raise OSError("optional pointer cannot be saved")

    if failure == "flush":
        session.provider = SimpleNamespace(force_flush=fail_flush)
    else:
        monkeypatch.setattr(tracing, "write_json", fail_write)
    for final in (False, True):
        await session.save_link(tmp_path, final=final)
    if failure == "flush":
        pointer = json.loads((tmp_path / "phoenix.json").read_text(encoding="utf-8"))
        assert pointer["status"] == "collector unavailable or project not resolved"


async def test_real_runtime_span_parentage_and_payloads(monkeypatch, tmp_path):
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = sdk.TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    original = TraceSession.__init__

    def injected(self, *args, **kwargs):
        original(self, *args, **{**kwargs, "enabled": True, "tracer": tracer})

    monkeypatch.setattr(TraceSession, "__init__", injected)
    directory = await cli.demo(
        Namespace(
            model="scripted",
            script=Path("tests/scripted-demo.json"),
            out=tmp_path,
            fault="bad_configuration",
        )
    )
    spans = exporter.get_finished_spans()
    root = next(s for s in spans if s.name == "investigation")
    assert root.attributes["system"] == "multi"
    roles = {
        s.context.span_id: s
        for s in spans
        if s.attributes.get("openinference.span.kind") == "AGENT"
    }
    calls = [s for s in spans if s.attributes.get("openinference.span.kind") in {"LLM", "TOOL"}]
    assert roles and calls
    assert all(s.parent.span_id in roles for s in calls)
    assert all(s.context.trace_id == root.context.trace_id for s in calls)
    llm = next(s for s in calls if s.attributes["openinference.span.kind"] == "LLM")
    assert "NO native or callable tools" in llm.attributes["input.value"]
    assert json.loads(llm.attributes["output.value"])["kind"] == "final"
    assert llm.attributes["llm.model_name"] == "scripted"
    pointer = json.loads((directory / "phoenix.json").read_text())
    assert pointer["trace_id"] == format(root.context.trace_id, "032x")
    # The lab fault is evaluator metadata only; it must not be injected into agent spans.
    assert "fault" not in root.attributes
    provider.shutdown()


async def test_langgraph_instrumentation_and_phoenix_deep_link(monkeypatch, tmp_path):
    pytest.importorskip("openinference.instrumentation.langchain")
    import httpx
    from openinference.instrumentation.langchain import LangChainInstrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from incident_investigator.telemetry import tracing

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    monkeypatch.setattr(tracing, "provider", lambda url: provider)
    monkeypatch.setattr(tracing, "_INSTRUMENTED", True)
    monkeypatch.setenv("II_PHOENIX", "1")
    original_client = httpx.AsyncClient

    project_reads = 0

    def handler(request):
        nonlocal project_reads
        project_reads += 1
        if project_reads == 1:
            return httpx.Response(404)
        if request.method == "POST":
            assert request.url.path == "/v1/projects"
            assert json.loads(request.content) == {"name": "incident-investigator"}
        else:
            assert request.url.path == "/v1/projects/incident-investigator"
        return httpx.Response(200, json={"data": {"id": "UHJvamVjdDox"}})

    def client(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return original_client(**kwargs)

    from incident_investigator.models import ScriptedModel

    original_complete = ScriptedModel.complete

    async def complete_with_live_pointer(self, *args, **kwargs):
        pointer_path = next(tmp_path.glob("*/phoenix.json"))
        live = json.loads(pointer_path.read_text())
        assert live["status"] == "recording"
        assert live["url"] and live["trace_id"] in live["url"]
        assert not list(tmp_path.glob("*/result.json"))
        return await original_complete(self, *args, **kwargs)

    monkeypatch.setattr(ScriptedModel, "complete", complete_with_live_pointer)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    try:
        directory = await cli.demo(
            Namespace(
                model="scripted",
                script=Path("tests/scripted-demo.json"),
                out=tmp_path,
                fault="bad_configuration",
            )
        )
        pointer = json.loads((directory / "phoenix.json").read_text())
        assert (
            pointer["url"]
            == "http://127.0.0.1:6006/projects/UHJvamVjdDox/traces/" + pointer["trace_id"]
        )
        spans = exporter.get_finished_spans()
        assert any(s.name == "LangGraph" for s in spans)
        assert any(s.name == "orchestrator_plan" for s in spans)
        assert any(s.attributes.get("openinference.span.kind") == "LLM" for s in spans)
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
