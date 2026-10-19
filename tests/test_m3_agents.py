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
                    "assignments": [assignment("dependency", next(iter(context["objections"])))],
                    "dispositions": [
                        {
                            "objection_id": next(iter(context["objections"])),
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
                            for row in data.get("observations", [])
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
                            "hypothesis_id": context["results"][context["hypothesis_revision"]][
                                "result"
                            ]["hypotheses"][0]["hypothesis_id"],
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


@pytest.mark.usefixtures("runtime_graph")
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
    assert any(o["model_label"] == "o1" for o in result["state"]["objections"].values())
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


@pytest.mark.usefixtures("runtime_graph")
async def test_runaway_stops_with_partial_report(tmp_path):
    directory = await investigate(
        load_snapshot("dev-00"), Scenario(runaway=True), tmp_path, limits=Limits(max_model_calls=7)
    )
    result = read_result(directory)
    assert result["status"] == "resource_exhausted"
    assert result["completeness"] == "partial"
    assert sum(row["model_calls"] for row in result["usage"].values()) <= 7
    assert "Stop reason" in (directory / "report.md").read_text()


@pytest.mark.usefixtures("runtime_graph")
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


@pytest.mark.usefixtures("runtime_graph")
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


@pytest.mark.usefixtures("runtime_graph")
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
    assert {"LogsQuery", "MetricsQuery", "HistoryQuery", "CatalogQuery"} <= set(specialist["$defs"])
    assert (
        len(specialist["$defs"]["ProviderTools"]["properties"]["requests"]["items"]["anyOf"]) == 7
    )


@pytest.mark.usefixtures("runtime_graph")
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


@pytest.mark.usefixtures("runtime_graph")
async def test_wall_deadline_retains_partial_summary(tmp_path):
    directory = await investigate(
        load_snapshot("dev-00"), Scenario(), tmp_path, limits=Limits(wall_seconds=0.001)
    )
    result = read_result(directory)
    assert result["status"] == "resource_exhausted"
    assert result["completeness"] == "partial"


@pytest.mark.usefixtures("runtime_graph")
async def test_no_progress_closes_gathering_with_unresolved_objection(tmp_path):
    class RepeatedGathering(Scenario):
        async def complete(self, messages, schema):
            context = json.loads(messages[1]["content"])
            if context["role"] == "orchestrator" and context["objections"]:
                self.actions.append(
                    final(
                        {
                            "route": "gather",
                            "assignments": [
                                assignment("dependency", next(iter(context["objections"])))
                            ],
                            "dispositions": [
                                {
                                    "objection_id": next(iter(context["objections"])),
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
    assert next(iter(result["state"]["dispositions"].values()))["outcome"] == "unresolved"


@pytest.mark.usefixtures("runtime_graph")
async def test_reference_missing_id_is_dropped_and_claim_kept(tmp_path):
    class MalformedOutput(Scenario):
        injected = False

        async def complete(self, messages, schema):
            response = await super().complete(messages, schema)
            context = json.loads(messages[1]["content"])
            if (
                context["role"] == "observability"
                and response.action["kind"] == "final"
                and not self.injected
            ):
                finding = response.action["result"]["findings"][0]
                finding["references"].append({"kind": "observation"})
                response.action["result"]["outputs"] = {"kind": "observation"}
                self.injected = True
            return response

    directory = await investigate(
        load_snapshot("dev-00"), MalformedOutput(followup=False), tmp_path
    )
    result = read_result(directory)
    assert result["status"] == "completed"
    task = next(t for t in result["state"]["tasks"].values() if t["role"] == "observability")
    assert task["status"] == "succeeded"
    assert task["corrections"] == 0
    # Scalar outputs are data, not references; the valid citation keeps the claim.
    assert task["result"]["outputs"] == {"kind": "observation"}
    assert task["result"]["findings"][0]["references"]
    events = [json.loads(line) for line in (directory / "trace.jsonl").read_text().splitlines()]
    assert not any(e["event"] == "validation" and e["outcome"] == "rejected" for e in events)
    repaired = [e for e in events if e["event"] == "validation" and e["outcome"] == "repaired"]
    assert [e["field"] for e in repaired] == ["action.result.findings[0].references[2].id"]
    assert result["repairs"]["observability"] == 1


@pytest.mark.usefixtures("runtime_graph")
async def test_review_round_limit_reports_and_orchestrator_sees_budget(tmp_path):
    model = Scenario()
    directory = await investigate(
        load_snapshot("dev-00"), model, tmp_path, limits=Limits(max_review_rounds=1)
    )
    result = read_result(directory)
    assert result["status"] == "completed"
    assert result["state"]["rounds"] == 1
    assert result["state"]["tasks"][result["state"]["report_task"]]["status"] == "succeeded"
    assert all(d["outcome"] == "unresolved" for d in result["state"]["dispositions"].values())
    contexts = [
        json.loads(messages[1]["content"])
        for messages, _ in model.requests
        if json.loads(messages[1]["content"])["role"] == "orchestrator"
    ]
    assert [(c["rounds"], c["max_review_rounds"]) for c in contexts] == [(0, 1), (1, 1)]


@pytest.mark.usefixtures("runtime_graph")
async def test_reused_model_labels_get_distinct_runtime_ids(tmp_path):
    from uuid import UUID

    class ReusedLabels(Scenario):
        async def complete(self, messages, schema):
            response = await super().complete(messages, schema)
            context = json.loads(messages[1]["content"])
            if context["role"] == "critic" and context["objections"]:
                hypothesis = context["results"][context["hypothesis_revision"]]["result"][
                    "hypotheses"
                ][0]
                response.action["result"]["objections"] = [
                    {
                        "objection_id": "o1",
                        "hypothesis_id": hypothesis["hypothesis_id"],
                        "question": "Still missing a causal check",
                        "evidence_ids": [],
                        "suggested_role": "dependency",
                    }
                ]
            return response

    directory = await investigate(load_snapshot("dev-00"), ReusedLabels(), tmp_path)
    result = read_result(directory)
    assert result["status"] == "completed"
    objections = list(result["state"]["objections"].values())
    hypotheses = [
        h
        for t in result["state"]["tasks"].values()
        if t["role"] == "reasoning"
        for h in t["result"]["hypotheses"]
    ]
    for items, id_field, label in (
        (objections, "objection_id", "o1"),
        (hypotheses, "hypothesis_id", "h1"),
    ):
        assert len(items) == 2
        assert len({item[id_field] for item in items}) == 2
        assert all(
            item["model_label"] == label and str(UUID(item[id_field])) == item[id_field]
            for item in items
        )
    assert set(result["state"]["dispositions"]) == {o["objection_id"] for o in objections}


async def test_waiting_for_slot_does_not_consume_call_timeout(tmp_path):
    from incident_investigator.runtime import Call, Investigation

    slot = asyncio.Semaphore(0)
    model = ScriptedModel([final({})])
    runner = Investigation(
        load_snapshot("dev-00"), model, tmp_path, Limits(call_seconds=0.01), slot
    )
    call = Call(
        call_id="queued",
        task_id="task",
        attempt_id="attempt",
        role="reasoning",
        kind="model",
        request={"messages": []},
    )
    pending = asyncio.create_task(runner.execute({"call": call.model_dump()}))
    await asyncio.sleep(0.03)
    assert not pending.done()
    slot.release()
    result = await pending
    assert result["inbox"]["queued"]["error"] is None
    assert len(model.requests) == 1


@pytest.mark.usefixtures("runtime_graph")
async def test_context_receipts_keep_ids_without_repeated_observations(tmp_path):
    model = Scenario(followup=False)
    directory = await investigate(load_snapshot("dev-00"), model, tmp_path)
    result = read_result(directory)
    reasoning = next(
        json.loads(messages[1]["content"])
        for messages, _ in model.requests
        if json.loads(messages[1]["content"])["role"] == "reasoning"
    )
    assert reasoning["evidence"]
    for receipt in reasoning["tool_receipts"].values():
        page = receipt["page"]
        assert "observations" not in page
        assert set(page["observation_ids"]) <= set(reasoning["evidence"])
    assert any(
        row.get("payload", {}).get("observations")
        for row in result["receipts"].values()
        if isinstance(row.get("payload"), dict)
    )


@pytest.mark.parametrize("support", ["empty_hypotheses", "empty_support", "receipt_only"])
@pytest.mark.usefixtures("runtime_graph")
async def test_likely_cause_requires_supporting_observation(tmp_path, support):
    class UnsupportedCause(Scenario):
        async def complete(self, messages, schema):
            response = await super().complete(messages, schema)
            context = json.loads(messages[1]["content"])
            if context["role"] == "reasoning":
                body = response.action["result"]
                body["conclusion"] = "likely_cause"
                if support == "empty_hypotheses":
                    body["hypotheses"] = []
                else:
                    body["hypotheses"][0]["supporting_evidence"] = (
                        [next(iter(context["tool_receipts"]))] if support == "receipt_only" else []
                    )
            return response

    directory = await investigate(
        load_snapshot("dev-00"), UnsupportedCause(followup=False), tmp_path
    )
    result = read_result(directory)
    assert result["status"] == "completed"
    assert result["state"]["tasks"][result["state"]["report_task"]]["status"] == "succeeded"
    task = next(t for t in result["state"]["tasks"].values() if t["role"] == "reasoning")
    assert task["status"] == "succeeded" and task["corrections"] == 0
    # An unsupported cause claim is downgraded, never accepted as likely_cause.
    assert result["diagnosis"] == "inconclusive"
    events = [json.loads(line) for line in (directory / "trace.jsonl").read_text().splitlines()]
    assert any(
        e["outcome"] == "repaired" and e["field"] == "action.result.conclusion" for e in events
    )


@pytest.mark.usefixtures("runtime_graph")
async def test_report_evidence_counts_and_empty_sections(tmp_path):
    class CountedEvidence(Scenario):
        async def complete(self, messages, schema):
            response = await super().complete(messages, schema)
            context = json.loads(messages[1]["content"])
            if context["role"] == "reasoning":
                hypothesis = response.action["result"]["hypotheses"][0]
                hypothesis["supporting_evidence"] = list(context["evidence"])[:2]
                hypothesis["contradicting_evidence"] = []
            return response

    directory = await investigate(
        load_snapshot("dev-00"), CountedEvidence(followup=False), tmp_path
    )
    text = (directory / "report.md").read_text()
    assert "### Supporting evidence (2)" in text
    assert "### Contradicting evidence (0)" in text
    # A separate empty replay has explicit prose in each empty section.
    from pathlib import Path

    actions = json.loads(Path("tests/scripted-demo.json").read_text())
    empty = await investigate(load_snapshot("dev-00"), ScriptedModel(actions), tmp_path)
    text = (empty / "report.md").read_text()
    assert "## Facts\n\nNone recorded." in text
    assert "\n{}\n" not in text
    assert "Proposed actions only; this investigation applies no fixes.\n\nNone recorded." in text


@pytest.mark.parametrize("expected_status", ["failed", "resource_exhausted"])
def test_cli_nonzero_status_preserves_artifacts(tmp_path, monkeypatch, capsys, expected_status):
    from incident_investigator import cli

    async def bounded_investigate(snapshot, model, out, **kwargs):
        kwargs.pop("limits")
        limits = Limits(max_model_calls=1) if expected_status == "resource_exhausted" else Limits()
        return await investigate(snapshot, model, out, limits=limits, **kwargs)

    monkeypatch.setattr(cli, "investigate", bounded_investigate)
    monkeypatch.setattr(
        "sys.argv",
        [
            "incident-investigator",
            "investigate",
            "--case",
            "dev-00",
            "--model",
            "scripted",
            "--out",
            str(tmp_path),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    from pathlib import Path

    report = Path(capsys.readouterr().out.strip())
    assert report.is_file()
    result = read_result(report.parent)
    assert result["status"] == expected_status
    assert (report.parent / "trace.jsonl").is_file()


def test_unknown_case_prints_concise_error_without_creating_run(tmp_path, monkeypatch, capsys):
    from incident_investigator import cli

    monkeypatch.setattr(
        "sys.argv",
        ["incident-investigator", "investigate", "--case", "missing", "--out", str(tmp_path)],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert "unknown case ID: missing" in error
    assert "Traceback" not in error
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("decision_index", [0, 1])
@pytest.mark.usefixtures("runtime_graph")
async def test_live_demo_2_decisions_are_repaired_into_reports(tmp_path, decision_index):
    from pathlib import Path

    saved = json.loads(Path("tests/live-demo-2-decisions.json").read_text())

    class ReplayDecision(Scenario):
        async def complete(self, messages, schema):
            context = json.loads(messages[1]["content"])
            if context["role"] == "critic":
                hypothesis_id = context["results"][context["hypothesis_revision"]]["result"][
                    "hypotheses"
                ][0]["hypothesis_id"]
                self.actions.append(
                    final(
                        {
                            "revision_id": context["hypothesis_revision"],
                            "summary": "Two open questions",
                            "objections": [
                                {
                                    "objection_id": label,
                                    "hypothesis_id": hypothesis_id,
                                    "question": "Causality remains unproven",
                                    "evidence_ids": [],
                                    "suggested_role": "observability",
                                }
                                for label in ["O1", "O2"]
                            ],
                        }
                    )
                )
                return await ScriptedModel.complete(self, messages, schema)
            if context["role"] == "orchestrator" and context["objections"]:
                assert context["objection_ids"] == list(context["objections"])
                text = json.dumps(saved["decisions"][decision_index])
                for old_id, current_id in zip(
                    saved["objection_ids"], context["objection_ids"], strict=True
                ):
                    text = text.replace(old_id, current_id)
                # The typo in the first saved UUID intentionally remains unknown.
                self.actions.append(json.loads(text))
                return await ScriptedModel.complete(self, messages, schema)
            return await super().complete(messages, schema)

    directory = await investigate(load_snapshot("dev-00"), ReplayDecision(), tmp_path)
    result = read_result(directory)
    assert result["status"] == "completed"
    assert result["completeness"] == "partial"
    assert result["state"]["tasks"][result["state"]["report_task"]]["status"] == "succeeded"
    assert len(result["state"]["dispositions"]) == 2
    assert all(d["outcome"] == "unresolved" for d in result["state"]["dispositions"].values())
    assert any(
        d["reason"] == "not disposed by orchestrator"
        for d in result["state"]["dispositions"].values()
    )
    decisions = [t for t in result["state"]["tasks"].values() if t["role"] == "orchestrator"]
    assert all(t["status"] == "succeeded" and t["corrections"] == 0 for t in decisions)
    events = [json.loads(line) for line in (directory / "trace.jsonl").read_text().splitlines()]
    assert any(e["event"] == "validation" and e["outcome"] == "repaired" for e in events)
    assert not any(e["event"] == "validation" and e["outcome"] == "rejected" for e in events)


@pytest.mark.usefixtures("runtime_graph")
async def test_duplicate_and_unknown_decision_references_repair_without_correction(tmp_path):
    class DuplicateDecision(Scenario):
        async def complete(self, messages, schema):
            context = json.loads(messages[1]["content"])
            if context["role"] == "orchestrator" and context["objections"]:
                key = context["objection_ids"][0]
                item = {
                    "objection_id": key,
                    "outcome": "unresolved",
                    "reason": "Insufficient evidence",
                }
                self.actions.append(
                    final(
                        {
                            "route": "report",
                            "assignments": [],
                            "dispositions": [item, item, {**item, "objection_id": "unknown"}],
                        }
                    )
                )
                return await ScriptedModel.complete(self, messages, schema)
            return await super().complete(messages, schema)

    directory = await investigate(load_snapshot("dev-00"), DuplicateDecision(), tmp_path)
    result = read_result(directory)
    assert result["status"] == "completed"
    assert len(result["state"]["dispositions"]) == 1
    assert "unknown" not in result["state"]["dispositions"]
    events = [json.loads(line) for line in (directory / "trace.jsonl").read_text().splitlines()]
    repaired = [e for e in events if e["event"] == "validation" and e["outcome"] == "repaired"]
    assert [e["field"] for e in repaired] == [
        "action.result.dispositions[1].objection_id",
        "action.result.dispositions[2].objection_id",
    ]
    assert result["repairs"]["orchestrator"] == 2


@pytest.mark.parametrize("failed_role", ["orchestrator", "critic", "reasoning", "decide"])
@pytest.mark.usefixtures("runtime_graph")
async def test_exhausted_control_step_still_runs_reporting(tmp_path, failed_role):
    class FailedControl(Scenario):
        async def complete(self, messages, schema):
            context = json.loads(messages[1]["content"])
            role = context["role"]
            if role == failed_role or (
                failed_role == "decide" and role == "orchestrator" and context["objections"]
            ):
                self.actions.append(final({"invalid": True}))
                return await ScriptedModel.complete(self, messages, schema)
            return await super().complete(messages, schema)

    directory = await investigate(load_snapshot("dev-00"), FailedControl(), tmp_path)
    result = read_result(directory)
    assert result["status"] == "completed"
    assert result["completeness"] == "partial"
    assert result["state"]["tasks"][result["state"]["report_task"]]["status"] == "succeeded"
    assert all(d["outcome"] == "unresolved" for d in result["state"]["dispositions"].values())
    if failed_role in {"orchestrator", "reasoning"}:
        assert result["report"]["revision_id"] is None
        assert result["diagnosis"] == "inconclusive"
    failures = [t for t in result["state"]["tasks"].values() if t["status"] == "failed"]
    assert len(failures) == 1 and failures[0]["corrections"] == 1


@pytest.mark.usefixtures("runtime_graph")
async def test_reporting_failure_remains_failed_with_partial_artifacts(tmp_path):
    class FailedReporting(Scenario):
        async def complete(self, messages, schema):
            if json.loads(messages[1]["content"])["role"] == "reporting":
                self.actions.append(final({"invalid": True}))
                return await ScriptedModel.complete(self, messages, schema)
            return await super().complete(messages, schema)

    directory = await investigate(
        load_snapshot("dev-00"), FailedReporting(followup=False), tmp_path
    )
    result = read_result(directory)
    assert result["status"] == "failed"
    assert result["completeness"] == "partial"
    assert "reporting:" in result["stop_reason"]
