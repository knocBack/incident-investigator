"""M4 runtime regressions: unknown replay, saved siblings, fencing and finite recovery.

Observed gaps were absent public recovery and failed source pages bypassing retries.
These scripted checks exercise the real SQLite graph, not a duplicate scheduler.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace

import pytest
from langgraph.errors import NodeCancelledError
from test_m3_agents import Scenario

from incident_investigator.contracts import Limits
from incident_investigator.models import ScriptedModel
from incident_investigator.reliability import invocation_lock
from incident_investigator.runtime import Call, Investigation, investigate
from incident_investigator.sources.loader import load_snapshot


class Crash(Scenario):
    def __init__(self):
        super().__init__(followup=False)
        self.crashed = False

    async def complete(self, messages, schema):
        role = json.loads(messages[1]["content"])["role"]
        if role == "observability" and self.role_calls.get(role) == 1 and not self.crashed:
            await asyncio.sleep(0.03)  # sibling completes and saves its pending write
            self.crashed = True
            raise asyncio.CancelledError("simulated abrupt interruption")
        return await super().complete(messages, schema)


def result(directory):
    return json.loads((directory / "result.json").read_text())


@pytest.mark.parametrize("variant", ["staged"])
async def test_public_resume_preserves_siblings_evidence_and_charges_unknown_attempt(
    tmp_path, variant
):
    model = Crash()
    with pytest.raises(NodeCancelledError):
        await investigate(load_snapshot("dev-00"), model, tmp_path, graph=variant)
    directory = next(tmp_path.iterdir())
    before = result(directory)
    assert before["status"] == before["stop_category"] == "interrupted"
    assert before["state"]["evidence"]
    metadata = (directory / "run.json").read_bytes()
    await investigate(None, model, resume=directory)
    after = result(directory)
    assert after["status"] == "completed"
    assert after["stop_category"] == "inconclusive"
    assert model.role_calls["orchestrator"] == 1
    assert model.role_calls["dependency"] == model.role_calls["observability"] == 2
    assert before["state"]["evidence"].items() <= after["state"]["evidence"].items()
    assert (directory / "run.json").read_bytes() == metadata
    calls = after["state"]["calls"]
    recovered = [call for call in calls.values() if call["retry_of"]]
    assert len(recovered) == 1
    new = recovered[0]
    old = calls[new["retry_of"]]
    assert new["call_id"] != old["call_id"]
    assert new["attempt_id"] != old["attempt_id"]
    assert new["invocation_id"] != old["invocation_id"]
    assert after["receipts"][old["call_id"]]["replay_blocked"]
    assert len(after["state"]["processed"]) == len(set(after["state"]["processed"]))
    assert sum(call["kind"] == "model" for call in calls.values()) == 9
    # A second resume of a terminal run never calls the adapter or duplicates tasks.
    await investigate(None, model, resume=directory)
    assert result(directory) == after


async def test_invocation_lock_rejects_other_modifiers_and_releases_on_exit(tmp_path):
    with (
        invocation_lock(tmp_path),
        pytest.raises(ValueError, match="active modifying invocation"),
        invocation_lock(tmp_path),
    ):
        pytest.fail("second modifier acquired ownership")
    with invocation_lock(tmp_path):
        pass


@pytest.mark.parametrize(
    "changed",
    ["dataset", "prompt", "tool_contract", "runtime", "checkpoint_schema", "dependencies"],
)
async def test_version_mismatch_is_read_only_and_calls_no_model(tmp_path, changed):
    model = Crash()
    with pytest.raises(NodeCancelledError):
        await investigate(load_snapshot("dev-00"), model, tmp_path)
    directory = next(tmp_path.iterdir())
    path = directory / "run.json"
    metadata = json.loads(path.read_text())
    metadata["versions"][changed] = "incompatible"
    path.write_text(json.dumps(metadata))
    saved = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    with pytest.raises(ValueError, match="incompatible resume versions"):
        await investigate(None, ScriptedModel(), resume=directory)
    assert saved == {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


async def test_resume_keeps_original_expired_deadline_and_consumed_allowances(
    tmp_path, monkeypatch
):
    from incident_investigator import runtime

    limits = Limits(wall_seconds=60)
    model = Crash()
    with pytest.raises(NodeCancelledError):
        await investigate(load_snapshot("dev-00"), model, tmp_path, limits=limits)
    directory = next(tmp_path.iterdir())
    before = result(directory)
    metadata = (directory / "run.json").read_bytes()
    deadline = json.loads(metadata)["deadline_at"]
    # Expire the saved absolute deadline independently of checkpoint/CI speed.
    monkeypatch.setattr(
        runtime,
        "time",
        SimpleNamespace(
            time=lambda: deadline + 1,
            monotonic=runtime.time.monotonic,
        ),
    )
    await investigate(None, model, resume=directory)
    after = result(directory)
    assert after["status"] == "resource_exhausted"
    assert before["state"]["calls"].keys() == after["state"]["calls"].keys()
    assert after["state"]["limits"] == limits.model_dump()
    assert (directory / "run.json").read_bytes() == metadata


@pytest.mark.parametrize("outcome", ["timeout", "error", "partial"])
async def test_tool_failure_retries_or_retains_explicit_gap_without_losing_sibling(
    tmp_path, outcome
):
    snapshot = load_snapshot("dev-00")
    snapshot = replace(
        snapshot,
        fault_plan=MappingProxyType(
            {
                "rules": [
                    {
                        "rule_id": "transient",
                        "tool": "logs",
                        "service": "inventory",
                        "steps": [{"outcome": outcome, "keep_first": 1}],
                    }
                ]
            }
        ),
    )
    directory = await investigate(
        snapshot, Scenario(followup=False), tmp_path, limits=Limits(call_seconds=0.05)
    )
    saved = result(directory)
    assert saved["status"] == "completed"
    calls = saved["state"]["calls"]
    logs = [
        call
        for call in calls.values()
        if call["kind"] == "tool" and call["request"]["name"] == "logs"
    ]
    assert len(logs) == (1 if outcome == "partial" else 2)
    if outcome != "partial":
        assert logs[1]["retry_of"] == logs[0]["call_id"]
    else:
        assert saved["completeness"] == "partial"
        assert any(
            row["source_status"] == "partial" for row in saved["state"]["tool_receipts"].values()
        )
    assert saved["state"]["evidence"]
    assert any(
        task["role"] == "dependency" and task["status"] == "succeeded"
        for task in saved["state"]["tasks"].values()
    )


async def test_default_api_capacity_is_shared_across_investigations(tmp_path):
    active = peak = 0

    class Measured(Scenario):
        async def complete(self, messages, schema):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.005)
                return await super().complete(messages, schema)
            finally:
                active -= 1

    await asyncio.gather(
        *[
            investigate(load_snapshot("dev-00"), Measured(followup=False), tmp_path)
            for _ in range(5)
        ]
    )
    assert peak == 4


async def test_local_waiters_leave_shared_capacity_available_to_other_investigations(tmp_path):
    """A caller's queued branches must not reserve the aggregate runner permits."""
    queued = asyncio.Event()
    entered = asyncio.Event()
    release = asyncio.Event()

    class LocalCapacity(asyncio.Semaphore):
        arrivals = 0

        async def acquire(self):
            self.arrivals += 1
            if self.arrivals == 4:
                queued.set()
            return await super().acquire()

    class BlockedModel(ScriptedModel):
        calls = 0

        async def complete(self, messages, schema):
            self.calls += 1
            entered.set()
            await release.wait()
            return await super().complete(messages, schema)

    snapshot = load_snapshot("dev-00")
    busy_dir, other_dir = tmp_path / "busy", tmp_path / "other"
    busy_dir.mkdir()
    other_dir.mkdir()
    blocked = BlockedModel()
    busy = Investigation(snapshot, blocked, busy_dir, Limits(), LocalCapacity(1))
    other = Investigation(
        snapshot,
        ScriptedModel([{"kind": "final", "result": {}}]),
        other_dir,
        Limits(),
        asyncio.Semaphore(4),
    )

    def request(index):
        return {
            "call": Call(
                call_id=str(index),
                task_id="task",
                attempt_id="attempt",
                role="reasoning",
                kind="model",
                request={"messages": []},
            ).model_dump()
        }

    workers = [asyncio.create_task(busy.execute(request(index))) for index in range(4)]
    try:
        async with asyncio.timeout(5):
            await entered.wait()
            await queued.wait()
            # Busy work stays blocked: the other investigation must use a free permit.
            receipt = await other.execute(request("independent"))
        assert receipt["inbox"]["independent"]["error"] is None
        assert blocked.calls == 1
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


@pytest.mark.parametrize("tool", ["history", "log_counts", "metric_stats", "trace_logs"])
def test_baseline_query_fault_window_matches_retrieval(tmp_path, tool):
    snapshot = load_snapshot("dev-00")
    start = snapshot.alert_start
    from datetime import timedelta

    lower = start - timedelta(minutes=5)
    snapshot = replace(
        snapshot,
        fault_plan=MappingProxyType(
            {
                "rules": [
                    {
                        "rule_id": "baseline",
                        "tool": tool,
                        "service": "inventory",
                        "window_start": lower.isoformat(),
                        "window_end": start.isoformat(),
                        "steps": [{"outcome": "error"}],
                    }
                ]
            }
        ),
    )
    runner = Investigation(snapshot, ScriptedModel(), tmp_path, Limits(), asyncio.Semaphore(1))
    from incident_investigator.runtime import Control

    c = Control(run_id="test", case_id="dev-00", limits=Limits())
    assert runner.fault_selection(c, tool, {"service": "inventory"}).outcome == "error"


async def test_sigkill_then_resume_in_fresh_process(tmp_path):
    """Unclean death cannot run the exception/export path or release a Python lock."""
    import os
    import signal
    import subprocess
    import sys

    script = """
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "tests"))
from test_m3_agents import Scenario
from incident_investigator.runtime import investigate
from incident_investigator.sources.loader import load_snapshot
class TranscriptScenario(Scenario):
    async def complete(self, messages, schema):
        context = json.loads(messages[1]["content"])
        # Derive the scripted turn from durable receipts, without process-local state.
        has_receipts = any('"tool_receipt_id"' in m["content"] for m in messages[2:])
        self.task_turns[messages[1]["content"]] = int(has_receipts)
        if sys.argv[1] == "crash" and context["role"] == "observability" and has_receipts:
            await asyncio.sleep(0.1)
            Path(sys.argv[3]).write_text("ready")
            await asyncio.Event().wait()
        return await super().complete(messages, schema)
model = TranscriptScenario(followup=False)
if sys.argv[1] == "crash":
    asyncio.run(investigate(load_snapshot("dev-00"), model, Path(sys.argv[2])))
else:
    asyncio.run(investigate(None, model, resume=Path(sys.argv[2])))
"""
    root, marker = tmp_path / "runs", tmp_path / "ready"
    process = subprocess.Popen([sys.executable, "-c", script, "crash", str(root), str(marker)])
    try:
        async with asyncio.timeout(10):
            while not marker.exists():
                assert process.poll() is None
                await asyncio.sleep(0.02)
        os.kill(process.pid, signal.SIGKILL)
        await asyncio.to_thread(process.wait, 5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    directory = next(root.iterdir())
    resumed = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script, "resume", str(directory)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert resumed.returncode == 0, resumed.stderr
    saved = result(directory)
    assert saved["status"] == "completed"
    assert len([row for row in saved["receipts"].values() if row.get("replay_blocked")]) == 1
    assert sum(call["kind"] == "tool" for call in saved["state"]["calls"].values()) == 5
    assert len(saved["state"]["evidence"]) == len(set(saved["state"]["evidence"]))


def test_cli_resume_uses_saved_graph_and_source(tmp_path, monkeypatch, capsys):
    from incident_investigator.cli import main

    model = Crash()
    with pytest.raises(NodeCancelledError):
        asyncio.run(investigate(load_snapshot("dev-00"), model, tmp_path, graph="staged"))
    directory = next(tmp_path.iterdir())
    monkeypatch.setattr("incident_investigator.cli.model", lambda args: model)
    monkeypatch.setattr(
        "sys.argv", ["incident-investigator", "investigate", "--resume", str(directory)]
    )
    main()
    assert str(directory / "report.md") in capsys.readouterr().out
    assert result(directory)["graph"] == "staged"
    assert result(directory)["status"] == "completed"
