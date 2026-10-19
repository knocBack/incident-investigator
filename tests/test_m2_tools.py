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
