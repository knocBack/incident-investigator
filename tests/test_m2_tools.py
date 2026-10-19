from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from incident_investigator.models import CodexExecModel, ScriptedModel
from incident_investigator.sources.loader import load_snapshot
from incident_investigator.tools import logs
from incident_investigator.tools.faults import FaultSelection, apply_fault


def snapshot():
    return load_snapshot("dev-00", Path("fixtures/dev-00"))


async def test_logs_page_is_stable_and_cutoff_filtered() -> None:
    first = await logs(snapshot(), service="checkout", limit=1)
    assert first.source_status == "complete"
    assert first.available_match_count and first.next_cursor
    second = await logs(snapshot(), service="checkout", limit=1, cursor=first.next_cursor)
    assert second.observations[0]["source_record_id"] != first.observations[0]["source_record_id"]
    assert first.query_key == second.query_key


async def test_fault_wrapper_partial_and_timeout() -> None:
    def call():
        return logs(snapshot(), service="checkout", limit=2)

    partial = await apply_fault(call, FaultSelection("partial", 1))()
    assert partial.source_status == "partial" and len(partial.observations) == 1
    errored = await apply_fault(call, FaultSelection("error"))()
    assert errored.source_status == "failed" and not errored.observations
    with pytest.raises(TimeoutError):
        await apply_fault(call, FaultSelection("timeout"), timeout_seconds=0.001)()


def test_loader_rejects_unknown_case() -> None:
    with pytest.raises(ValueError, match="unknown case"):
        load_snapshot("nope", Path("fixtures/dev-00"))


async def test_scripted_model_and_codex_command(tmp_path: Path) -> None:
    model = ScriptedModel([{"kind": "tools", "requests": []}])
    result = await model.complete([], {"type": "object"})
    assert result.action["kind"] == "tools"
    command = CodexExecModel().build_command(tmp_path, tmp_path / "schema", tmp_path / "output")
    assert command[0:2] == ["codex", "exec"] and "gpt-6-luna" in command


@pytest.mark.live
@pytest.mark.skipif(not os.getenv("II_LIVE_CODEX"), reason="opt-in live model test")
def test_live_codex_structured_call() -> None:
    result = asyncio.run(CodexExecModel(timeout_seconds=30).complete([], {"type": "object"}))
    assert isinstance(result.action, dict)


async def test_metrics_dev00_is_nonempty() -> None:
    from incident_investigator.tools import metrics

    page = await metrics(snapshot(), service="inventory")
    assert page.source_status == "complete"
    assert page.observations


def test_timestamp_invalid_types_are_validation_errors() -> None:
    from pydantic import ValidationError

    from incident_investigator.tools.contracts import LogsQuery

    for value in ({}, [], object()):
        with pytest.raises(ValidationError):
            LogsQuery(service="checkout", start=value)


def test_codex_thread_id_comes_from_thread_started() -> None:
    from incident_investigator.models.adapter import _events

    usage, run_id = _events(
        '{"type":"thread.started","thread_id":"thread-1"}\n'
        '{"type":"item.completed","id":"item-1"}\n'
        '{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":3}}'
    )
    assert run_id == "thread-1"
    assert usage.input_tokens == 12 and usage.output_tokens == 3


async def test_codex_cancellation_kills_and_reaps_child(tmp_path, monkeypatch) -> None:
    from unittest.mock import AsyncMock, Mock

    started = asyncio.Event()

    async def communicate(_):
        started.set()
        await asyncio.Event().wait()

    process = Mock(returncode=None, communicate=communicate, wait=AsyncMock())
    monkeypatch.setattr("shutil.which", lambda _: "/codex")
    monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=process))
    task = asyncio.create_task(CodexExecModel().complete([], {}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


async def test_codex_unavailable_hint_matches_cli_selection(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(RuntimeError, match="--model scripted --script"):
        await CodexExecModel().complete([], {})
    command = CodexExecModel().build_command(Path("/tmp"), Path("/schema"), Path("/out"))
    assert command[command.index("view_image") - 1] == "--disable"
