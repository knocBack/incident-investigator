from __future__ import annotations

import asyncio
import json

import pytest

from incident_investigator.contracts import Limits
from incident_investigator.models import ScriptedModel
from incident_investigator.runtime import investigate, merge_identical
from incident_investigator.sources.loader import load_snapshot
from incident_investigator.tools.faults import FaultSelection


def final(result):
    return {"kind": "final", "result": result}


def assignment(role, objection_id=None):
    return {
        "role": role,
        "question": "Check the evidence for the assigned question",
        "objection_id": objection_id,
    }


class Scenario(ScriptedModel):
    """Test-only role script. No incident expectations are present in runtime code."""

    def __init__(self, *, followup=True, runaway=False, invalid=False):
        super().__init__()
        self.followup, self.runaway, self.invalid = followup, runaway, invalid
        self.task_turns = {}
        self.role_calls = {}
        self.intervals = {}

    async def complete(self, messages, schema):
        context = json.loads(messages[1]["content"])
        role = context["role"]
        task_key = messages[1]["content"]
        turn = self.task_turns.get(task_key, 0)
        self.task_turns[task_key] = turn + 1
        self.role_calls[role] = self.role_calls.get(role, 0) + 1
        if role in {"observability", "dependency"}:
            begin = asyncio.get_running_loop().time()
            await asyncio.sleep(0.003)
            self.intervals.setdefault(role, []).append((begin, asyncio.get_running_loop().time()))
        if self.runaway and role == "observability":
            action = {
                "kind": "tools",
                "requests": [{"name": "logs", "arguments": {"service": "checkout"}}],
            }
        elif role == "orchestrator":
            if not context["objections"]:
                result = {
                    "route": "gather",
                    "assignments": [assignment("observability"), assignment("dependency")],
                    "dispositions": [],
                }
            elif not context["dispositions"] and self.followup:
                result = {
                    "route": "gather",
                    "assignments": [assignment("dependency", "o1")],
                    "dispositions": [
                        {
                            "objection_id": "o1",
                            "outcome": "gather",
                            "reason": "Check an alternative",
                        }
                    ],
                }
            else:
                result = {
                    "route": "report",
                    "assignments": [],
                    "dispositions": [
                        {
                            "objection_id": key,
                            "outcome": "unresolved",
                            "reason": "Evidence cannot settle this objection",
                        }
                        for key in context["objections"]
                    ],
                }
            action = final(result)
        elif role in {"observability", "dependency"}:
            if turn == 0:
                names = (
                    ["logs", "metrics"]
                    if role == "observability"
                    else ["dependencies", "changes", "history"]
                )
                action = {
                    "kind": "tools",
                    "requests": [
                        {"name": name, "arguments": {"service": "inventory"}} for name in names
                    ],
                }
            else:
                refs = []
                for message in messages[2:]:
                    if message["role"] == "user":
                        try:
                            data = json.loads(message["content"])
                        except json.JSONDecodeError:
                            continue
                        refs.extend(
                            {"kind": "observation", "id": row["evidence_id"]}
                            for row in (data.get("page") or {}).get("observations", [])
                        )
                if self.invalid:
                    refs = [{"kind": "observation", "id": "invented-evidence"}]
                action = final(
                    {
                        "findings": [
                            {
                                "statement": "Retrieved source observations",
                                "kind": "fact",
                                "references": refs[:2],
                            }
                        ]
                        if refs
                        else [],
                        "gaps": [] if refs else ["No usable observations"],
                        "outputs": {},
                    }
                )
        elif role == "reasoning":
            ids = list(context["evidence"])
            action = final(
                {
                    "hypotheses": [
                        {
                            "hypothesis_id": "h1",
                            "statement": "Possible dependency degradation",
                            "confidence": "low",
                            "supporting_evidence": ids[:1],
                            "contradicting_evidence": ids[1:2],
                            "assumptions": ["Temporal association is causal"],
                        }
                    ],
                    "conclusion": "inconclusive",
                    "explanation": "Evidence remains ambiguous",
                }
            )
        elif role == "critic":
            action = final(
                {
                    "revision_id": context["hypothesis_revision"],
                    "summary": "Check alternatives",
                    "objections": [
                        {
                            "objection_id": "o1",
                            "hypothesis_id": "h1",
                            "question": "Could a change explain this?",
                            "evidence_ids": [],
                            "suggested_role": "dependency",
                        }
                    ]
                    if self.followup and not context["objections"]
                    else [],
                }
            )
        else:
            ids = list(context["evidence"])
            action = final(
                {
                    "revision_id": context["hypothesis_revision"],
                    "summary": "The evidence is inconclusive",
                    "facts": [
                        {
                            "statement": "An observation was retrieved",
                            "kind": "fact",
                            "references": [{"kind": "observation", "id": ids[0]}],
                        }
                    ]
                    if ids
                    else [],
                    "assumptions": ["Cause is not yet verified"],
                    "recommendations": [
                        {
                            "action": "Review relevant configuration",
                            "verification": "Replay load and compare error count",
                            "evidence_ids": ids[:1],
                        }
                    ],
                    "unresolved_objection_ids": list(context["objections"]),
                }
            )
        self.actions.append(action)
        return await super().complete(messages, schema)


def read_result(directory):
    return json.loads((directory / "result.json").read_text())


async def test_e2e_parallel_followup_and_grounded_report(tmp_path):
    model = Scenario()
    directory = await investigate(load_snapshot("dev-00"), model, tmp_path)
    result = read_result(directory)
    assert result["status"] == "completed"
    assert result["state"]["rounds"] == 2
    assert model.role_calls["dependency"] == 4
    a, b = model.intervals["observability"][0], model.intervals["dependency"][0]
    assert max(a[0], b[0]) < min(a[1], b[1])
    reasoning_inputs = [
        json.loads(messages[1]["content"])
        for messages, _ in model.requests
        if json.loads(messages[1]["content"])["role"] == "reasoning"
    ]
    assert {v["role"] for v in reasoning_inputs[0]["results"].values()} >= {
        "observability",
        "dependency",
    }
    assert all(v["status"] != "running" for v in reasoning_inputs[0]["results"].values())
    known = set(result["state"]["evidence"]) | set(result["state"]["tool_receipts"])
    for hypothesis in result["hypotheses"]:
        assert (
            set(hypothesis["supporting_evidence"] + hypothesis["contradicting_evidence"]) <= known
        )
    text = (directory / "report.md").read_text()
    for section in (
        "Ranked hypotheses",
        "Supporting evidence",
        "Contradicting evidence",
        "confidence:",
        "Facts",
        "Assumptions",
        "Corrective recommendations and verification",
        "Open and unresolved objections",
        "Per-role resource usage",
    ):
        assert section in text
    assert "o1" in text
    assert set(result["usage"]) == {
        "orchestrator",
        "observability",
        "dependency",
        "reasoning",
        "critic",
        "reporting",
    }
    assert all(row["input_tokens"] == "unknown" for row in result["usage"].values())
    trace = [json.loads(line) for line in (directory / "trace.jsonl").read_text().splitlines()]
    assert {row["event"] for row in trace} >= {
        "model_call",
        "tool_call",
        "validation",
        "state_transition",
    }
    assert all(row["run_id"] == directory.name for row in trace)


async def test_runaway_stops_with_partial_report(tmp_path):
    directory = await investigate(
        load_snapshot("dev-00"), Scenario(runaway=True), tmp_path, limits=Limits(max_model_calls=7)
    )
    result = read_result(directory)
    assert result["status"] == "resource_exhausted"
    assert result["completeness"] == "partial"
    assert sum(row["model_calls"] for row in result["usage"].values()) <= 7
    assert "Stop reason" in (directory / "report.md").read_text()


async def test_timeout_is_a_gap_and_other_specialist_survives(tmp_path):
    directory = await investigate(
        load_snapshot("dev-00"),
        Scenario(followup=False),
        tmp_path,
        limits=Limits(call_seconds=0.02),
        faults={"logs": FaultSelection("timeout")},
    )
    result = read_result(directory)
    assert result["status"] == "completed"
    assert any(
        "TimeoutError" in (receipt["error"] or "")
        for receipt in result["state"]["tool_receipts"].values()
    )
    assert any(row["source"] == "changes" for row in result["state"]["evidence"].values())
    assert "Gap:" in (directory / "report.md").read_text()


async def test_concurrent_runs_are_isolated(tmp_path):
    first, second = await asyncio.gather(
        *[
            investigate(load_snapshot("dev-00"), Scenario(followup=False), tmp_path)
            for _ in range(2)
        ]
    )
    a, b = read_result(first), read_result(second)
    assert a["status"] == b["status"] == "completed"
    assert a["run_id"] != b["run_id"]
    assert not (set(a["state"]["tasks"]) & set(b["state"]["tasks"]))
    assert not (set(a["receipts"]) & set(b["receipts"]))
    assert a["usage"] != {} and b["usage"] != {}


async def test_unknown_ids_receive_one_correction_then_specialist_settles(tmp_path):
    directory = await investigate(
        load_snapshot("dev-00"), Scenario(followup=False, invalid=True), tmp_path
    )
    result = read_result(directory)
    assert result["status"] == "completed"
    specialists = [
        t for t in result["state"]["tasks"].values() if t["role"] in {"observability", "dependency"}
    ]
    assert all(
        t["status"] == "failed" and t["corrections"] == 1 and t["result"] is None
        for t in specialists
    )


def test_identity_merge_replay_and_conflict():
    original = {"a": {"value": 1}}
    assert merge_identical(original, original) == original
    assert merge_identical(original, {"b": 2}) == {"a": {"value": 1}, "b": 2}
    assert original == {"a": {"value": 1}}
    with pytest.raises(ValueError, match="conflicting identity"):
        merge_identical(original, {"a": {"value": 2}})


@pytest.mark.parametrize(
    "event",
    [
        {"type": "item.started", "item": {"type": "command_execution"}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call"}},
        {"type": "tool.started"},
        {"type": "item.completed", "item": {"type": "image_generation"}},
        {"type": "item.completed", "item": {"type": "web_search"}},
    ],
)
def test_codex_execution_events_are_rejected(event):
    from incident_investigator.models.adapter import _events

    with pytest.raises(RuntimeError, match="forbidden"):
        _events(json.dumps(event))


def test_provider_schemas_keep_tool_arguments_and_root_references():
    from incident_investigator.contracts import PAYLOADS
    from incident_investigator.runtime import provider_schema

    for role in PAYLOADS:
        schema = provider_schema(role)

        def check(node, schema=schema):
            if isinstance(node, dict):
                if "$ref" in node:
                    assert node["$ref"][8:] in schema["$defs"]
                if node.get("type") == "object":
                    assert node["additionalProperties"] is False
                    assert set(node["required"]) == set(node["properties"])
                for child in node.values():
                    check(child)
            elif isinstance(node, list):
                for child in node:
                    check(child)

        check(schema)
    specialist = provider_schema("observability")
    assert {"LogsQuery", "MetricsQuery", "HistoryQuery"} <= set(specialist["$defs"])
    assert (
        len(specialist["$defs"]["ProviderTools"]["properties"]["requests"]["items"]["anyOf"]) == 3
    )


async def test_inprocess_lab_demo_scripted(tmp_path):
    from argparse import Namespace
    from pathlib import Path

    from incident_investigator.cli import demo

    directory = await demo(
        Namespace(
            model="scripted",
            script=Path("tests/scripted-demo.json"),
            out=tmp_path,
            fault="bad_configuration",
        )
    )
    result = read_result(directory)
    assert result["status"] == "completed"
    assert any(row["source"] == "changes" for row in result["state"]["evidence"].values())
    assert any(row["source"] == "metrics" for row in result["state"]["evidence"].values())


async def test_wall_deadline_retains_partial_summary(tmp_path):
    directory = await investigate(
        load_snapshot("dev-00"), Scenario(), tmp_path, limits=Limits(wall_seconds=0.001)
    )
    result = read_result(directory)
    assert result["status"] == "resource_exhausted"
    assert result["completeness"] == "partial"


async def test_no_progress_closes_gathering_with_unresolved_objection(tmp_path):
    class RepeatedGathering(Scenario):
        async def complete(self, messages, schema):
            context = json.loads(messages[1]["content"])
            if context["role"] == "orchestrator" and context["objections"]:
                self.actions.append(
                    final(
                        {
                            "route": "gather",
                            "assignments": [assignment("dependency", "o1")],
                            "dispositions": [
                                {
                                    "objection_id": "o1",
                                    "outcome": "gather",
                                    "reason": "Repeat the query",
                                }
                            ],
                        }
                    )
                )
                return await ScriptedModel.complete(self, messages, schema)
            return await super().complete(messages, schema)

    directory = await investigate(load_snapshot("dev-00"), RepeatedGathering(), tmp_path)
    result = read_result(directory)
    assert result["status"] == "completed"
    assert result["state"]["rounds"] == 3
    assert result["state"]["no_progress"] == 2
    assert result["state"]["dispositions"]["o1"]["outcome"] == "unresolved"
