"""Repair-not-reject, real-world data access, dedupe, budgets, profiles and lab signals."""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_m3_agents import Scenario, final, read_result

from incident_investigator.contracts import PROFILES, Limits
from incident_investigator.lab.__main__ import snapshot as freeze
from incident_investigator.lab.app import JsonlTelemetrySink, create_app
from incident_investigator.models import ScriptedModel
from incident_investigator.repair import Repairs, Scope, repair_action, repair_result
from incident_investigator.runtime import investigate
from incident_investigator.sources.loader import load_snapshot
from incident_investigator.tools import (
    build_registry,
    catalog,
    changes,
    dependencies,
    history,
    log_counts,
    logs,
    metric_stats,
    metrics,
    trace_logs,
)

T0 = datetime(2026, 3, 1, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = T0

    def __call__(self) -> datetime:
        return self.value


def lab_snapshot(tmp_path: Path, fault: str, *, baseline: int = 3, faulted: int = 5):
    """Freeze a deterministic lab run; the last records land exactly on the cutoff."""
    clock, telemetry = Clock(), tmp_path / "telemetry.jsonl"
    with TestClient(create_app(clock, telemetry_sink=JsonlTelemetrySink(telemetry))) as client:
        for index in range(baseline):
            clock.value = T0 + timedelta(seconds=index + 1)
            client.post("/checkout")
        clock.value = T0 + timedelta(seconds=10)
        assert client.post("/admin/faults", json={"family": fault}).status_code == 200
        for index in range(faulted):
            clock.value = T0 + timedelta(seconds=11 + index)
            client.post("/checkout")
    freeze(
        Namespace(
            cutoff=clock.value,
            telemetry=telemetry,
            out=tmp_path / "case",
            case="lab-quality",
            service="checkout",
            symptoms=["checkout latency or errors increased"],
            start=T0 - timedelta(seconds=11),
        )
    )
    return load_snapshot("lab-quality", tmp_path / "case")


def events(directory):
    return [json.loads(line) for line in (directory / "trace.jsonl").read_text().splitlines()]


# 1. Repair, don't reject.


def scope(**values):
    defaults = dict(
        observations=frozenset({"obs-a"}),
        receipts=frozenset({"rcpt-a"}),
        revision="rev-1",
        hypothesis_ids=frozenset({"h-1"}),
        objection_ids=("o-1",),
        alert_service="checkout",
    )
    return Scope(**{**defaults, **values})


def test_near_miss_enums_aliases_and_unknown_fields_are_repaired():
    repairs = Repairs()
    action = repair_action(
        "reasoning",
        {
            "kind": "Result",
            "result": {
                "hypotheses": [
                    {
                        "hypothesis_id": "h",
                        "statement": "Inventory database deadline too short",
                        "confidence": "Moderate",
                        "supporting_evidence": ["obs-a"],
                        "rationale": "extra field",
                    }
                ],
                "conclusion": "Likely Cause",
                "explanation": "Timeouts follow the configuration change",
            },
        },
        set(),
        repairs,
        final_only=False,
    )
    hypothesis = action["result"]["hypotheses"][0]
    assert action["kind"] == "final"
    assert hypothesis["confidence"] == "medium"
    assert hypothesis["contradicting_evidence"] == [] and hypothesis["assumptions"] == []
    assert "rationale" not in hypothesis
    assert action["result"]["conclusion"] == "likely_cause"
    fields = dict(repairs.items)
    assert "action.kind" in fields
    assert "action.result.hypotheses[0].confidence" in fields
    assert fields["action.result.hypotheses[0].rationale"] == "unknown field dropped"
    assert fields["action.result.hypotheses[0].contradicting_evidence"].startswith("missing")


def test_tool_request_repairs_and_exact_field_rejection():
    repairs = Repairs()
    action = repair_action(
        "observability",
        {
            "kind": "tools",
            "requests": [
                {"name": "Log", "arguments": {"service": "inventory", "level": "warning"}},
                {"name": "changes", "arguments": {"service": "inventory"}},
            ],
        },
        {"logs", "metrics"},
        repairs,
        final_only=False,
    )
    assert action["requests"] == [
        {"name": "logs", "arguments": {"service": "inventory", "severity": "WARN"}}
    ]
    fields = dict(repairs.items)
    assert fields["action.requests[0].arguments.level"] == "field renamed to 'severity'"
    assert "action.requests[1].name" in fields
    with pytest.raises(ValueError, match=r"action\.requests\[0\]\.name: 'shell'"):
        repair_action(
            "observability",
            {"kind": "tools", "requests": [{"name": "shell", "arguments": {}}]},
            {"logs"},
            Repairs(),
            final_only=False,
        )
    with pytest.raises(ValueError, match="action.kind: this is the final permitted turn"):
        repair_action(
            "observability",
            {"kind": "tools", "requests": [{"name": "logs", "arguments": {}}]},
            {"logs"},
            Repairs(),
            final_only=True,
        )
    with pytest.raises(ValueError, match="action: response was not a JSON object"):
        repair_action(
            "critic", {"kind": "invalid", "raw_output": "{"}, set(), Repairs(), final_only=False
        )
    with pytest.raises(ValueError, match="action.result: no usable content"):
        repair_action("critic", final({"invalid": True}), set(), Repairs(), final_only=False)


@pytest.mark.parametrize(
    "raw",
    [
        '```json\n{"action": {"kind": "final", "result": {"summary": "s"}}}\n```',
        'I have enough evidence.\n\n```json\n{"kind": "final", "result": {"summary": "s"}}\n```',
        'Result: {"kind": "final", "result": {"summary": "s"}} done',
    ],
)
def test_json_in_markdown_fence_or_prose_is_extracted(raw):
    # Both live-smoke rejections were valid actions wrapped like this.
    repairs = Repairs()
    action = repair_action(
        "critic", {"kind": "invalid", "raw_output": raw}, set(), repairs, final_only=False
    )
    assert action["kind"] == "final" and action["result"]["summary"] == "s"
    assert repairs.items[0] == (
        "action",
        "JSON object extracted from markdown fence or surrounding text",
    )


def test_unknown_citations_are_dropped_and_claims_with_support_kept():
    repairs = Repairs()
    result = repair_result(
        "observability",
        "gathering",
        {
            "findings": [
                {
                    "statement": "Inventory errors rose",
                    "kind": "fact",
                    "references": [
                        {"kind": "observation", "id": "obs-a"},
                        {"kind": "observation", "id": "invented"},
                        {"kind": "observation", "id": "rcpt-a"},
                    ],
                },
                {
                    "statement": "Unsupported fact",
                    "kind": "fact",
                    "references": [{"kind": "observation", "id": "invented"}],
                },
            ],
            "gaps": [],
            "outputs": {},
        },
        scope(),
        repairs,
    )
    assert result["findings"] == [
        {
            "statement": "Inventory errors rose",
            "kind": "fact",
            "references": [
                {"kind": "observation", "id": "obs-a"},
                {"kind": "tool_receipt", "id": "rcpt-a"},
            ],
        }
    ]
    fields = [path for path, _ in repairs.items]
    assert "action.result.findings[0].references[1].id" in fields
    assert "action.result.findings[0].references[2].kind" in fields
    assert "action.result.findings[1]" in fields
    with pytest.raises(ValueError, match="action.result.findings: every fact cites unknown"):
        repair_result(
            "dependency",
            "gathering",
            {
                "findings": [
                    {
                        "statement": "x",
                        "kind": "fact",
                        "references": [{"kind": "observation", "id": "invented"}],
                    }
                ],
                "gaps": [],
                "outputs": {},
            },
            scope(),
            Repairs(),
        )


def test_critic_and_reporting_identities_are_runtime_owned():
    repairs = Repairs()
    review = repair_result(
        "critic",
        "critic",
        {
            "revision_id": "stale",
            "summary": "s",
            "objections": [
                {
                    "objection_id": "o",
                    "hypothesis_id": "label-h1",
                    "question": "q",
                    "evidence_ids": ["obs-a", "nope"],
                    "suggested_role": None,
                }
            ],
        },
        scope(),
        repairs,
    )
    assert review["revision_id"] == "rev-1"
    assert review["objections"][0]["hypothesis_id"] is None
    assert review["objections"][0]["evidence_ids"] == ["obs-a"]
    report = repair_result(
        "reporting",
        "reporting",
        {
            "revision_id": "rev-1",
            "summary": "s",
            "facts": [{"statement": "Guess", "kind": "interpretation", "references": []}],
            "assumptions": [],
            "recommendations": [],
            "unresolved_objection_ids": [],
        },
        scope(),
        repairs,
    )
    assert report["facts"] == [] and report["assumptions"] == ["Guess"]
    assert report["unresolved_objection_ids"] == ["o-1"]


class InventedPlanIds(Scenario):
    """Replays the paid-run Orchestrator deviation: invented initial objection IDs."""

    def __init__(self, *, drop_dependency=False, **kwargs):
        super().__init__(followup=False, **kwargs)
        self.drop_dependency = drop_dependency

    async def complete(self, messages, schema):
        context = json.loads(messages[1]["content"])
        if context["role"] == "orchestrator" and not context["objections"]:
            assignments = [
                {"role": "observability", "question": "Check logs", "objection_id": "obs-1"},
                {"role": "dependency", "question": "Check changes", "objection_id": "dep-1"},
            ]
            self.actions.append(
                final(
                    {
                        "assignments": assignments[:1] if self.drop_dependency else assignments,
                        "route": "gather",
                        "dispositions": [],
                    }
                )
            )
            return await ScriptedModel.complete(self, messages, schema)
        return await super().complete(messages, schema)


async def test_initial_plan_objection_ids_are_repaired_not_rejected(tmp_path):
    directory = await investigate(load_snapshot("dev-00"), InventedPlanIds(), tmp_path)
    result = read_result(directory)
    assert result["status"] == "completed"
    roles = {task["role"] for task in result["state"]["tasks"].values()}
    assert {"observability", "dependency", "reasoning", "critic", "reporting"} <= roles
    assert result["state"]["evidence"]
    trace = events(directory)
    assert not any(e["event"] == "validation" and e["outcome"] == "rejected" for e in trace)
    repaired = [e for e in trace if e["outcome"] == "repaired"]
    assert {e["field"] for e in repaired} == {
        "action.result.assignments[0].objection_id",
        "action.result.assignments[1].objection_id",
    }
    assert all(e["role"] == "orchestrator" and e["rule"] for e in repaired)
    assert result["repairs"]["orchestrator"] == 2
    assert result["usage"]["orchestrator"]["repairs"] == 2


async def test_missing_specialist_assignment_is_added(tmp_path):
    directory = await investigate(
        load_snapshot("dev-00"), InventedPlanIds(drop_dependency=True), tmp_path
    )
    result = read_result(directory)
    dependency = [t for t in result["state"]["tasks"].values() if t["role"] == "dependency"]
    assert len(dependency) == 1 and dependency[0]["status"] == "succeeded"
    assert any(
        e["field"] == "action.result.assignments" and "dependency" in e["rule"]
        for e in events(directory)
        if e["outcome"] == "repaired"
    )


async def test_rejection_names_the_exact_field_and_rule(tmp_path):
    class BadConfidence(Scenario):
        async def complete(self, messages, schema):
            response = await super().complete(messages, schema)
            if json.loads(messages[1]["content"])["role"] == "reasoning":
                response.action["result"]["hypotheses"][0]["confidence"] = "certain"
            return response

    directory = await investigate(load_snapshot("dev-00"), BadConfidence(followup=False), tmp_path)
    rejected = [e for e in events(directory) if e["outcome"] == "rejected"]
    assert rejected
    assert rejected[0]["error"].startswith("action.final.result.hypotheses.0.confidence:")
    assert "'low', 'medium' or 'high'" in rejected[0]["error"]


# 2. Real-world data access.


async def test_catalog_lists_real_names_without_diagnosis():
    snapshot = load_snapshot("dev-00")
    page = await catalog(snapshot)
    summary = page.summary
    assert page.source_status == "complete" and page.observations == []
    assert summary["services"] == ["checkout", "database", "inventory"]
    latency = next(m for m in summary["metrics"] if m["name"] == "request_latency")
    assert latency["unit"] == "ms" and latency["aggregations"] == ["p95"]
    assert latency["window_seconds"] == [60] and latency["first_event"] < latency["last_event"]
    assert summary["logs"]["severities"]
    assert all({"template", "count", "severity"} <= set(t) for t in summary["logs"]["templates"])
    assert summary["changes"] and all("config_keys" in c for c in summary["changes"])
    assert summary["dependencies"]["checkout"] == ["inventory"]
    assert {h["incident_id"] for h in summary["history"]}
    assert summary["valid_values"]["service"] == ["checkout", "inventory", "database"]
    scoped = await catalog(snapshot, service="database")
    assert {m["service"] for m in scoped.summary["metrics"]} <= {"database"}


def test_tool_descriptions_list_valid_enum_values():
    registry = build_registry(load_snapshot("dev-00"))
    assert {"catalog", "log_counts", "metric_stats", "trace_logs"} <= set(registry)
    for tool in registry.values():
        assert "checkout|inventory|database" in tool.description
        assert "TRACE|DEBUG|INFO|WARN|ERROR|FATAL" in tool.description
        assert "deployment|configuration" in tool.description


async def test_log_counts_group_by_attribute_and_bucket_with_citable_exemplars(tmp_path):
    snapshot = lab_snapshot(tmp_path, "bad_configuration")
    page = await log_counts(
        snapshot, service="checkout", group_by=["severity"], attribute="status", bucket_seconds=10
    )
    groups = page.summary["groups"]
    failed = [g for g in groups if g["key"] == {"severity": "ERROR", "attribute:status": 504}]
    assert sum(g["count"] for g in failed) == 5
    assert {g["bucket_start"] for g in failed} == {"2026-03-01T00:00:10.000000Z"}
    attached = {row["evidence_id"] for row in page.observations}
    assert all(g["exemplar_evidence_id"] in attached for g in groups)
    assert page.summary["total_records"] == page.available_match_count


async def test_metric_stats_before_and_after_split(tmp_path):
    snapshot = lab_snapshot(tmp_path, "database_contention")
    page = await metric_stats(
        snapshot,
        service="inventory",
        metric_name="db_query_latency",
        split_at="2026-03-01T00:00:10Z",
    )
    (series,) = page.summary["series"]
    assert series["unit"] == "ms" and series["count"] == 8
    assert series["before_split"]["max"] == 2.0
    assert series["after_split"]["min"] == 250.0
    assert series["peak_evidence_id"] == page.observations[0]["evidence_id"]


async def test_trace_logs_correlate_one_request_across_services(tmp_path):
    snapshot = lab_snapshot(tmp_path, "database_contention")
    request_id = next(
        row.data["request_id"]
        for row in snapshot.observations
        if row.source == "logs" and row.data["service"] == "database"
    )
    page = await trace_logs(snapshot, request_id=request_id)
    services = [row["data"]["service"] for row in page.observations]
    assert set(services) == {"checkout", "inventory", "database"}
    assert page.available_match_count == len(page.observations)
    with pytest.raises(ValueError, match="trace_id or request_id is required"):
        await trace_logs(snapshot)


@pytest.mark.parametrize("source", ["dev-00", "lab"])
async def test_every_record_within_cutoff_is_retrievable_by_paging(tmp_path, source):
    snapshot = (
        load_snapshot("dev-00")
        if source == "dev-00"
        else lab_snapshot(tmp_path, "traffic_overload")
    )
    expected = {
        (row.source, row.source_record_id)
        for row in snapshot.observations
        if row.available_at <= snapshot.cutoff
        and not (row.source == "history" and row.data["incident_id"] == snapshot.case_id)
    }
    if source == "lab":
        # The fixed lab clock puts the final records exactly on the cutoff instant.
        assert any(row.event_time == snapshot.cutoff for row in snapshot.observations)
    earliest = min(row.event_time for row in snapshot.observations) - timedelta(minutes=5)
    found = set()

    async def drain(function, **arguments):
        cursor = None
        while True:
            page = await function(snapshot, limit=7, cursor=cursor, **arguments)
            assert page.source_status == "complete"
            found.update((row["source"], row["source_record_id"]) for row in page.observations)
            cursor = page.next_cursor
            if not cursor:
                return

    for service in ("checkout", "inventory", "database"):
        for function in (logs, metrics, changes):
            await drain(function, service=service, start=earliest.isoformat())
        await drain(history, service=service)
    for row in snapshot.observations:
        if row.source == "dependencies":
            page = await dependencies(
                snapshot, service=row.data["service"], as_of=row.event_time.isoformat()
            )
            found.update((r["source"], r["source_record_id"]) for r in page.observations)
    assert found == expected


# 3. Query dedupe and budget awareness.


class RepeatedQuery(Scenario):
    """Observability asks the same query twice (different timestamp spelling), then finishes."""

    async def complete(self, messages, schema):
        context = json.loads(messages[1]["content"])
        if context["role"] == "observability":
            turn = sum(1 for m in messages if m["role"] == "assistant")
            if turn < 2:
                end = context["alert"]["cutoff"]
                end = end.replace("+00:00", "Z") if turn else end
                self.actions.append(
                    {
                        "kind": "tools",
                        "requests": [
                            {"name": "logs", "arguments": {"service": "inventory", "end": end}},
                            {"name": "logs", "arguments": {"service": "inventory", "end": end}},
                        ],
                    }
                )
                return await ScriptedModel.complete(self, messages, schema)
        return await super().complete(messages, schema)


async def test_identical_normalized_query_is_answered_without_a_tool_call(tmp_path):
    model = RepeatedQuery(followup=False)
    directory = await investigate(load_snapshot("dev-00"), model, tmp_path)
    result = read_result(directory)
    calls = [
        c
        for c in result["state"]["calls"].values()
        if c["kind"] == "tool" and c["request"]["name"] == "logs"
    ]
    assert len(calls) == 1
    # Turn 1 respells the cutoff; turn 2 omits it (default end): both normalize identically.
    dedupe = [e for e in events(directory) if e["event"] == "tool_dedupe"]
    assert len(dedupe) == 2 and {e["receipt_id"] for e in dedupe} == {calls[0]["call_id"]}
    assert result["usage"]["observability"]["deduplicated_queries"] == 2
    observability = [
        messages
        for messages, _ in model.requests
        if json.loads(messages[1]["content"])["role"] == "observability"
    ]
    replay = json.loads(
        next(m["content"] for m in observability[-1] if "duplicate_query" in m["content"])
    )
    assert replay["charged_tool_call"] is False
    assert replay["page"]["observation_ids"]


async def test_specialists_see_budget_and_final_turn_is_final_only(tmp_path):
    model = Scenario(followup=False)
    await investigate(load_snapshot("dev-00"), model, tmp_path, limits=Limits(task_tool_calls=3))
    dependency = [
        (messages, schema)
        for messages, schema in model.requests
        if json.loads(messages[1]["content"])["role"] == "dependency"
    ]
    first, second = dependency[0], dependency[1]
    assert (
        "Budget: 9 task model calls remain after this response; 3 tool calls remain"
        in (first[0][-1]["content"])
    )
    assert json.loads(first[0][1]["content"])["budget"]["task_tool_calls"] == 3
    # Three tools spent the task's tool allowance; the next turn may only finalize.
    assert "0 tool calls remain" in second[0][-1]["content"]
    assert "ProviderTools" not in second[1]["$defs"]


async def test_task_allowance_settles_the_task_not_the_run(tmp_path):
    directory = await investigate(
        load_snapshot("dev-00"),
        Scenario(followup=False, runaway=True),
        tmp_path,
        limits=Limits(task_model_calls=3),
    )
    result = read_result(directory)
    assert result["status"] == "completed"
    observability = next(
        t for t in result["state"]["tasks"].values() if t["role"] == "observability"
    )
    assert observability["status"] == "failed"
    assert observability["model_calls"] == 3
    assert result["state"]["tasks"][result["state"]["report_task"]]["status"] == "succeeded"


# 4. Timeouts from the run profile.


def test_demo_live_profile_drives_call_and_wall_timeouts(monkeypatch):
    from incident_investigator import cli

    live = PROFILES["demo-live"]
    assert live.call_seconds == 90
    assert live.wall_seconds >= live.call_seconds * (live.task_model_calls + 5)
    for name in ("openrouter", "codex"):
        assert cli.profile(Namespace(model=name)) == live
    assert cli.profile(Namespace(model="scripted")) == PROFILES["demo"]
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    assert cli.model(Namespace(model="openrouter")).timeout_seconds == 90
    assert cli.model(Namespace(model="codex")).timeout_seconds == 90


async def test_demo_passes_the_live_profile_to_the_investigation(tmp_path, monkeypatch):
    from incident_investigator import cli

    seen = {}

    async def capture(snapshot, model, out, **kwargs):
        seen.update(kwargs)
        return out

    monkeypatch.setattr(cli, "investigate", capture)
    await cli.demo(Namespace(model="codex", script=None, out=tmp_path, fault="traffic_overload"))
    assert seen["limits"] == PROFILES["demo-live"]


# 5. Lab realism: each fault is discoverable through catalog plus aggregation tools.

SIGNALS = {
    "bad_configuration": (
        ("inventory", "ERROR", "database query timed out at configured deadline"),
        ("inventory", "request_latency"),
    ),
    "deployment_regression": (
        ("inventory", "WARN", "v2 reservation query expanded scan"),
        ("inventory", "db_query_latency"),
    ),
    "database_contention": (
        ("database", "WARN", "lock wait while reserving inventory"),
        ("inventory", "db_query_latency"),
    ),
    "resource_exhaustion": (
        ("inventory", "ERROR", "connection pool exhausted"),
        ("inventory", "pool_in_use"),
    ),
    "traffic_overload": (
        ("inventory", "WARN", "worker concurrency saturated"),
        ("inventory", "active_requests"),
    ),
}


@pytest.mark.parametrize("fault", sorted(SIGNALS))
async def test_each_fault_is_discoverable_through_catalog_and_aggregates(tmp_path, fault):
    (service, severity, template), (metric_service, metric) = SIGNALS[fault]
    snapshot = lab_snapshot(tmp_path, fault)
    discovered = (await catalog(snapshot)).summary
    assert {"service": service, "severity": severity, "template": template} in [
        {k: t[k] for k in ("service", "severity", "template")}
        for t in discovered["logs"]["templates"]
    ]
    assert any(
        m["service"] == metric_service and m["name"] == metric for m in discovered["metrics"]
    )
    counts = await log_counts(snapshot, severity=severity)
    group = next(g for g in counts.summary["groups"] if g["key"]["template"] == template)
    assert group["count"] == 5 and group["key"]["service"] == service
    assert group["exemplar_evidence_id"] in {row["evidence_id"] for row in counts.observations}
    stats = await metric_stats(
        snapshot, service=metric_service, metric_name=metric, split_at="2026-03-01T00:00:10Z"
    )
    (series,) = stats.summary["series"]
    assert series["after_split"]["mean"] > series["before_split"]["mean"]
    if fault in {"bad_configuration", "deployment_regression"}:
        assert discovered["changes"], "the change record is discoverable"


async def test_demo_spaces_baseline_fault_and_load_in_time(tmp_path):
    from incident_investigator import cli

    directory = await cli.demo(
        Namespace(
            model="scripted",
            script=Path("tests/scripted-demo.json"),
            out=tmp_path,
            fault="bad_configuration",
        )
    )
    evidence = read_result(directory)["state"]["evidence"].values()
    change = next(row for row in evidence if row["source"] == "changes")
    requests = sorted(
        row["event_time"]
        for row in evidence
        if row["source"] == "logs" and row["data"]["service"] == "inventory"
    )
    # Baseline requests precede the configuration change by whole seconds; load follows it.
    assert requests and requests[0] < change["event_time"] < requests[-1]
    gap = datetime.fromisoformat(change["event_time"]) - datetime.fromisoformat(
        max(r for r in requests if r < change["event_time"])
    )
    assert gap >= timedelta(seconds=1)
