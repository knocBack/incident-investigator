"""Shared bounded investigation runtime; the staged workflow is the default."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from functools import cache, reduce
from operator import or_
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import NodeCancelledError
from pydantic import Field, TypeAdapter, ValidationError, create_model

from incident_investigator.contracts import (
    ALLOWED_TOOLS,
    PAYLOADS,
    Limits,
    Plan,
    Record,
    Role,
    action_type,
)
from incident_investigator.graph_config import DEFAULT_GRAPH, GraphVariant
from incident_investigator.models import CodexExecModel, ModelAdapter, ModelCallError, ScriptedModel
from incident_investigator.reliability import (
    invocation_lock,
    resume_metadata,
    runner_capacity,
    save_source,
    versions,
)
from incident_investigator.repair import (
    Repairs,
    Scope,
    repair_action,
    repair_decision,
    repair_result,
)
from incident_investigator.sources.loader import CaseSnapshot
from incident_investigator.telemetry.tracing import TraceSession
from incident_investigator.tools import DESCRIPTIONS, build_registry, query_identity
from incident_investigator.tools.contracts import QUERY_MODELS, ToolPage
from incident_investigator.tools.core import UNBOUNDED_START, describe_validation
from incident_investigator.tools.faults import FaultSelection, apply_fault


def identity() -> str:
    return str(uuid4())


def merge_identical(left: dict, right: dict) -> dict:
    merged = dict(left)
    for key, value in right.items():
        if key in merged and merged[key] != value:
            raise ValueError(f"conflicting identity: {key}")
        merged[key] = value
    return merged


class Task(Record):
    task_id: str
    attempt_id: str
    role: Role
    question: str
    input_revision: int
    status: Literal["running", "succeeded", "failed"] = "running"
    phase: Literal["model", "waiting", "tools", "settled"] = "model"
    messages: list[dict[str, Any]]
    visible_refs: list[str]
    pending: list[str] = Field(default_factory=list)
    requests: list[dict[str, Any]] = Field(default_factory=list)
    result: dict[str, Any] | None = None
    failure: str | None = None
    model_calls: int = 0
    tool_calls: int = 0
    corrections: int = 0
    repairs: int = 0


class Call(Record):
    call_id: str
    task_id: str
    attempt_id: str
    role: Role
    kind: Literal["model", "tool"]
    request: dict[str, Any]
    invocation_id: str | None = None
    retry: int = 0
    retry_of: str | None = None
    fault: dict[str, Any] | None = None
    query: str | None = None


class Control(Record):
    run_id: str
    case_id: str
    limits: Limits
    graph: GraphVariant = DEFAULT_GRAPH
    system: Literal["multi", "single"] = "multi"
    revision: int = 0
    status: Literal[
        "running", "completed", "inconclusive", "interrupted", "failed", "resource_exhausted"
    ] = "running"
    stop_reason: str | None = None
    partial: bool = False
    models: dict[str, str] = Field(default_factory=dict)
    stage: str = "start"
    tasks: dict[str, Task] = Field(default_factory=dict)
    batch: list[str] = Field(default_factory=list)
    calls: dict[str, Call] = Field(default_factory=dict)
    evidence: dict[str, dict[str, Any]] = Field(default_factory=dict)
    processed: list[str] = Field(default_factory=list)
    tool_receipts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    coverage: list[str] = Field(default_factory=list)
    # Normalized page identity -> receipt that already answered it (complete pages only).
    query_index: dict[str, str] = Field(default_factory=dict)
    repairs: dict[str, int] = Field(default_factory=dict)
    deduplicated: dict[str, int] = Field(default_factory=dict)
    fault_counters: dict[str, int] = Field(default_factory=dict)
    rounds: int = 0
    no_progress: int = 0
    progress_before: int = 0
    hypothesis_revision: str | None = None
    review_task: str | None = None
    objections: dict[str, dict[str, Any]] = Field(default_factory=dict)
    dispositions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    report_task: str | None = None
    dispatch: list[str] = Field(default_factory=list)


class GraphState(TypedDict):
    control: dict[str, Any]
    inbox: Annotated[dict[str, dict[str, Any]], merge_identical]


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Codex structured outputs require every property, including nullable ones."""
    if schema.get("type") == "object":
        schema.setdefault("properties", {})
        schema["required"] = list(schema["properties"])
        schema["additionalProperties"] = False
    for value in schema.values():
        if isinstance(value, dict):
            _strict_schema(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _strict_schema(item)
    # JSON Schema discriminators are unnecessary for provider validation.
    schema.pop("discriminator", None)
    if "oneOf" in schema:
        schema["anyOf"] = schema.pop("oneOf")
    return schema


def provider_schema(role: str, *, allow_tools: bool = True) -> dict:
    """Concrete tool argument schemas avoid free-form objects in strict output."""
    return copy.deepcopy(_provider_schema(role, allow_tools))


@cache
def _provider_schema(role: str, allow_tools: bool) -> dict:
    final = create_model(
        PAYLOADS[role].__name__ + "Final",
        __base__=Record,
        kind=(Literal["final"], ...),
        result=(PAYLOADS[role], ...),
    )
    variants = [final]
    if role in ALLOWED_TOOLS and allow_tools:
        requests = [
            create_model(
                name.title() + "Request",
                __base__=Record,
                name=(Literal[name], ...),
                arguments=(QUERY_MODELS[name], ...),
            )
            for name in sorted(ALLOWED_TOOLS[role])
        ]
        tools = create_model(
            "ProviderTools",
            __base__=Record,
            kind=(Literal["tools"], ...),
            requests=(list[reduce(or_, requests)], Field(min_length=1, max_length=5)),
        )
        variants.insert(0, tools)
    schema = _strict_schema(TypeAdapter(reduce(or_, variants)).json_schema())
    definitions = schema.pop("$defs", {})
    return {
        "type": "object",
        "$defs": definitions,
        "properties": {"action": schema},
        "required": ["action"],
        "additionalProperties": False,
    }


def model_input(role, request):
    """Render the same bounded input for execution and the console cost preview."""
    remaining = request.get("remaining_model_calls_after_this", 1)
    tools_left = request.get("remaining_tool_calls", 1)
    schema = provider_schema(role, allow_tools=remaining > 0 and tools_left > 0)
    messages = [
        *request["messages"],
        {
            "role": "user",
            "content": (
                'Return the tagged action as JSON under the "action" wrapper. '
                "You have NO native or callable tools. Prior user messages contain "
                "the real registry tool results. Read them and advance the investigation; "
                "do not repeat a completed query without a new evidence question. "
                "Return final findings once you have enough evidence, including gaps. "
                f"Budget: {remaining} task model calls remain after this "
                f"response; {tools_left} tool calls remain. "
                "If either is 0, return a final result now, not more tools."
            ),
        },
    ]
    return messages, schema


class Investigation:
    def __init__(
        self,
        snapshot: CaseSnapshot,
        model: ModelAdapter,
        out: Path,
        limits: Limits,
        semaphore: asyncio.Semaphore,
        *,
        faults: dict[str, FaultSelection] | None = None,
        graph: GraphVariant = DEFAULT_GRAPH,
        system: Literal["multi", "single"] = "multi",
    ) -> None:
        if graph != DEFAULT_GRAPH:
            raise ValueError(f"unknown graph: {graph}")
        self.snapshot, self.model, self.out, self.limits = snapshot, model, out, limits
        self.semaphore = semaphore
        self.registry = build_registry(snapshot)
        self.faults = faults or {}
        self.variant = graph
        if system not in {"multi", "single"}:
            raise ValueError(f"unknown system: {system}")
        self.system = system
        self.invocation_id = identity()
        self.started = time.monotonic()
        self.deadline = self.started + limits.wall_seconds
        self.observer = TraceSession(
            out.name, snapshot.case_id, graph=graph, system=system, enabled=False
        )
        self.adapters = {role: TypeAdapter(action_type(body)) for role, body in PAYLOADS.items()}

    def trace(self, event: str, **values: Any) -> None:
        row = {
            "run_id": self.out.name,
            "graph": self.variant,
            "system": self.system,
            "event": event,
            "at": time.time(),
            **values,
        }
        row.setdefault("task_id", None)
        row.setdefault("attempt_id", None)
        row.setdefault("call_id", None)
        row.setdefault("role", "runtime")
        row.setdefault("tokens", "unknown")
        row.setdefault("latency_ms", 0)
        row.setdefault("outcome", "ok")
        with (self.out / "trace.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def context(self, control: Control, role: Role, question: str) -> list[dict[str, Any]]:
        prompt = (Path(__file__).parent / "prompts" / f"{role}.md").read_text()
        # No source paths, planning material, snapshot internals or fault schedule.
        inputs = {
            "role": role,
            "question": question,
            "alert": dict(self.snapshot.alert),
            "hypothesis_revision": control.hypothesis_revision,
            "report_completeness": "partial" if control.partial else "complete",
            "stop_reason": control.stop_reason,
            "results": {
                key: {
                    "role": task.role,
                    "status": task.status,
                    "result": task.result,
                    "failure": task.failure,
                }
                for key, task in control.tasks.items()
                if task.phase == "settled"
            },
            "evidence": control.evidence,
            "tool_receipts": control.tool_receipts,
            "objections": control.objections,
            "dispositions": control.dispositions,
            **(
                {
                    "rounds": control.rounds,
                    "max_review_rounds": control.limits.max_review_rounds,
                    "objection_ids": list(control.objections),
                }
                if role == "orchestrator"
                else {}
            ),
            "tools": {
                name: {
                    "description": DESCRIPTIONS[name],
                    "arguments": QUERY_MODELS[name].model_json_schema(),
                }
                for name in sorted(ALLOWED_TOOLS.get(role, set()))
            },
            **(
                {
                    "budget": {
                        "task_model_calls": control.limits.max_model_calls
                        if role == "single"
                        else control.limits.task_model_calls,
                        "task_tool_calls": control.limits.max_tool_calls
                        if role == "single"
                        else control.limits.task_tool_calls,
                        "note": "Identical repeated queries are answered from earlier "
                        "receipts without a tool call. Start with catalog to learn names.",
                    }
                }
                if role in ALLOWED_TOOLS
                else {}
            ),
        }
        prompt += (
            "\nYou have NO native or callable tools. Retrieve data only by returning "
            "a JSON tools action naming these registry tools. Do not claim they are "
            "unavailable because they are not callable. An empty registry means your "
            "role cannot retrieve. Registry names and argument schemas: "
            + json.dumps(inputs["tools"])
        )
        return [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(inputs, ensure_ascii=False)},
        ]

    def task(self, control: Control, role: Role, question: str) -> str:
        task_id = identity()
        control.tasks[task_id] = Task(
            task_id=task_id,
            attempt_id=identity(),
            role=role,
            question=question,
            input_revision=control.revision,
            messages=self.context(control, role, question),
            visible_refs=[*control.evidence, *control.tool_receipts],
        )
        return task_id

    def close(self, control: Control, status: str, reason: str) -> None:
        control.status, control.stop_reason = status, reason
        for task in control.tasks.values():
            if task.status == "running":
                task.status, task.phase, task.failure = "failed", "settled", reason
        for key in control.objections:
            if control.dispositions.get(key, {}).get("outcome") != "unresolved":
                control.dispositions[key] = {
                    "objection_id": key,
                    "outcome": "unresolved",
                    "reason": reason,
                }

    def scope(self, c: Control, task: Task) -> Scope:
        visible = set(task.visible_refs)
        hypotheses = (
            c.tasks[c.hypothesis_revision].result["hypotheses"] if c.hypothesis_revision else []
        )
        return Scope(
            observations=frozenset(visible & set(c.evidence)),
            receipts=frozenset(visible & set(c.tool_receipts)),
            revision=c.hypothesis_revision,
            hypothesis_ids=frozenset(h["hypothesis_id"] for h in hypotheses),
            objection_ids=tuple(c.objections),
            alert_service=str(self.snapshot.alert.get("affected_service", "the affected service")),
        )

    def record_repairs(self, c: Control, task: Task, call_id: str, repairs: Repairs) -> None:
        for path, rule in repairs.items:
            self.trace(
                "validation",
                task_id=task.task_id,
                attempt_id=task.attempt_id,
                call_id=call_id,
                role=task.role,
                outcome="repaired",
                field=path,
                rule=rule,
            )
        task.repairs += len(repairs.items)
        c.repairs[task.role] = c.repairs.get(task.role, 0) + len(repairs.items)
        if repairs and task.phase != "settled":
            # Multi-turn specialists learn from what the runtime had to normalize.
            task.messages.append(
                {
                    "role": "user",
                    "content": "Runtime repaired your last action: "
                    + "; ".join(f"{path}: {rule}" for path, rule in repairs.items)[:2000],
                }
            )

    def fault_selection(self, c: Control, name: str, arguments: dict) -> FaultSelection:
        if name in self.faults:
            return self.faults[name]
        for rule in self.snapshot.fault_plan["rules"]:
            if rule["tool"] != name or rule["service"] != arguments.get("service"):
                continue
            if rule.get("window_start"):
                from datetime import datetime

                def point(value):
                    return datetime.fromisoformat(value.replace("Z", "+00:00"))

                start, end = point(rule["window_start"]), point(rule["window_end"])
                if name == "dependencies":
                    at = (
                        point(arguments["as_of"])
                        if arguments.get("as_of")
                        else self.snapshot.cutoff
                    )
                    if not start <= at < end:
                        continue
                else:
                    lower = (
                        point(arguments["start"])
                        if arguments.get("start")
                        else (None if name in UNBOUNDED_START else self.snapshot.alert_start)
                    )
                    upper = (
                        point(arguments["end"]) if arguments.get("end") else self.snapshot.cutoff
                    )
                    if upper <= start or (lower is not None and lower >= end):
                        continue
            key = rule["rule_id"]
            index = c.fault_counters.get(key, 0)
            c.fault_counters[key] = index + 1
            step = rule["steps"][index] if index < len(rule["steps"]) else {"outcome": "none"}
            return FaultSelection(step["outcome"], step.get("keep_first"), key)
        return FaultSelection()

    def admit(
        self,
        c: Control,
        task: Task,
        kind: str,
        request: dict,
        *,
        retry: int = 0,
        retry_of: str | None = None,
        query: str | None = None,
    ) -> None:
        total = sum(call.kind == kind for call in c.calls.values())
        cap = c.limits.max_model_calls if kind == "model" else c.limits.max_tool_calls
        count = task.model_calls if kind == "model" else task.tool_calls
        task_cap = (
            cap
            if task.role == "single"
            else c.limits.task_model_calls
            if kind == "model"
            else c.limits.task_tool_calls
        )
        if time.monotonic() >= self.deadline or total >= cap:
            self.close(c, "resource_exhausted", f"{kind} call or wall time limit")
            return
        if count >= task_cap:
            # A task allowance settles only that task; its batch still reaches Reasoning.
            task.status, task.phase = "failed", "settled"
            task.failure = f"task {kind}-call allowance ({task_cap}) exhausted"
            return
        if kind == "model":
            tool_total = sum(call.kind == "tool" for call in c.calls.values())
            request = {
                **request,
                "remaining_model_calls_after_this": task_cap - count - 1,
                "remaining_tool_calls": max(
                    0,
                    min(
                        (
                            c.limits.max_tool_calls
                            if task.role == "single"
                            else c.limits.task_tool_calls
                        )
                        - task.tool_calls,
                        c.limits.max_tool_calls - tool_total,
                    ),
                ),
            }
        call_id = identity()
        fault = (
            self.fault_selection(c, request["name"], request["arguments"])
            if kind == "tool"
            else None
        )
        c.calls[call_id] = Call(
            call_id=call_id,
            task_id=task.task_id,
            attempt_id=task.attempt_id,
            role=task.role,
            kind=kind,
            request=json.loads(json.dumps(request)),
            invocation_id=self.invocation_id,
            retry=retry,
            retry_of=retry_of,
            query=query,
            fault={
                "outcome": fault.outcome,
                "keep_first": fault.keep_first,
                "rule_id": fault.rule_id,
            }
            if fault
            else None,
        )
        if kind == "model":
            task.model_calls += 1
        else:
            task.tool_calls += 1
        task.pending.append(call_id)
        task.phase = "waiting"
        c.dispatch.append(call_id)

    def accept(self, c: Control, call: Call, receipt: dict) -> None:
        task = c.tasks[call.task_id]
        task.pending.remove(call.call_id)
        if receipt.get("replay_blocked"):
            # Unknown prior execution retains its charge; a successor is a new attempt.
            task.attempt_id = identity()
        if receipt.get("error"):
            if call.retry < c.limits.call_retries and receipt.get("retryable", True):
                self.admit(
                    c,
                    task,
                    call.kind,
                    call.request,
                    retry=call.retry + 1,
                    retry_of=call.call_id,
                    query=call.query,
                )
                return
            if call.kind == "model":
                task.status, task.phase, task.failure = "failed", "settled", receipt["error"]
                return
        if call.kind == "tool":
            payload = receipt.get("payload")
            if payload:
                page = ToolPage.model_validate(payload)
                for observation in page.observations:
                    key = observation["evidence_id"]
                    c.evidence = merge_identical(c.evidence, {key: observation})
                    if key not in task.visible_refs:
                        task.visible_refs.append(key)
                if (
                    page.source_status == "complete"
                    and not page.observations
                    and page.summary is None
                    and page.query_key not in c.coverage
                ):
                    c.coverage.append(page.query_key)
                if page.source_status == "complete" and call.query:
                    c.query_index.setdefault(call.query, call.call_id)
            # Retain full pages in raw receipts and canonical evidence, not twice in context.
            summary = (
                {key: value for key, value in payload.items() if key != "observations"}
                if payload
                else None
            )
            observations = payload["observations"] if payload else []
            if summary is not None:
                summary["observation_ids"] = [row["evidence_id"] for row in observations]
            if not payload or payload["source_status"] != "complete":
                c.partial = True
            c.tool_receipts[call.call_id] = {
                "source_status": payload["source_status"] if payload else "failed",
                "tool": call.request["name"],
                "error": receipt.get("error"),
                "page": summary,
            }
            task.visible_refs.append(call.call_id)
            task.messages.append(
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "tool_receipt_id": call.call_id,
                            **c.tool_receipts[call.call_id],
                            "observations": observations,
                        }
                    ),
                }
            )
            if not task.pending:
                task.phase = "model"
            return
        action = receipt["payload"]
        task.messages.append({"role": "assistant", "content": json.dumps(action)})
        repairs = Repairs()
        try:
            if receipt.get("finish_reason") == "length":
                raise ValueError("output_truncated")
            allowed = ALLOWED_TOOLS.get(task.role, set())
            final_only = call.request.get("remaining_model_calls_after_this", 1) <= 0 or (
                call.request.get("remaining_tool_calls", 1) <= 0
            )
            action = repair_action(task.role, action, allowed, repairs, final_only=final_only)
            if action["kind"] == "final":
                scope = self.scope(c, task)
                if task.role == "orchestrator" and c.stage == "decide":
                    try:
                        decision = Plan.model_validate(action["result"]).model_dump()
                    except ValidationError as exc:
                        raise ValueError(describe_validation(exc, "action.result")) from exc
                    action["result"], reopened = repair_decision(decision, scope, repairs)
                    if reopened:
                        c.partial = True
                        c.stop_reason = (
                            "orchestrator decision repaired; unresolved objections retained"
                        )
                else:
                    action["result"] = repair_result(
                        task.role, c.stage, action["result"], scope, repairs
                    )
            data = self.adapters[task.role].validate_python(action).model_dump(mode="json")
            if data["kind"] == "tools":
                requests = []
                for index, request in enumerate(data["requests"]):
                    try:
                        query_identity(self.snapshot, request["name"], request["arguments"])
                    except ValueError as exc:
                        repairs.add(
                            f"action.requests[{index}].arguments", f"request dropped: {exc}"
                        )
                        continue
                    requests.append(request)
                if not requests:
                    raise ValueError(
                        "; ".join(f"{path}: {rule}" for path, rule in repairs.items[-5:])
                    )
                task.requests = requests
                task.phase = "tools"
            else:
                # Model IDs are labels only; authoritative identities are runtime allocated.
                field = {
                    "reasoning": ("hypotheses", "hypothesis_id"),
                    "single": ("hypotheses", "hypothesis_id"),
                    "critic": ("objections", "objection_id"),
                }.get(task.role)
                if field:
                    for item in data["result"][field[0]]:
                        item["model_label"] = item[field[1]]
                        item[field[1]] = identity()
                if task.role == "single":
                    data["result"]["report"]["revision_id"] = task.task_id
                task.result, task.status, task.phase = data["result"], "succeeded", "settled"
            self.record_repairs(c, task, call.call_id, repairs)
            self.trace(
                "validation",
                task_id=task.task_id,
                attempt_id=task.attempt_id,
                call_id=call.call_id,
                role=task.role,
                outcome="accepted",
            )
        except (ValueError, ValidationError) as exc:
            error = (
                describe_validation(exc, "action") if isinstance(exc, ValidationError) else str(exc)
            )[:1000]
            self.trace(
                "validation",
                task_id=task.task_id,
                attempt_id=task.attempt_id,
                call_id=call.call_id,
                role=task.role,
                outcome="rejected",
                error=error,
            )
            allowance = task.model_calls < (
                c.limits.max_model_calls if task.role == "single" else c.limits.task_model_calls
            )
            if task.corrections == 0 and allowance:
                task.corrections += 1
                task.phase = "model"
                task.messages.append(
                    {
                        "role": "user",
                        "content": f"Correct this invalid action once. Field and rule: {error}",
                    }
                )
            else:
                task.status, task.phase, task.failure = (
                    "failed",
                    "settled",
                    "output_truncated"
                    if receipt.get("finish_reason") == "length"
                    else f"invalid output: {error}",
                )

    def report(self, c: Control, partial_reason: str | None = None) -> None:
        if partial_reason:
            c.partial = True
            c.stop_reason = partial_reason
        for key in c.objections:
            if c.dispositions.get(key, {}).get("outcome") != "unresolved":
                c.dispositions[key] = {
                    "objection_id": key,
                    "outcome": "unresolved",
                    "reason": "gathering closed; objection remains unproven",
                }
        c.stage = "reporting"
        c.report_task = self.task(
            c,
            "reporting",
            "Report the reviewed revision and every unresolved objection. Propose corrective actions and verification; never claim fixes applied.",
        )
        c.batch = [c.report_task]

    def round(self, c: Control, plan: Plan) -> None:
        if c.rounds >= c.limits.max_review_rounds:
            self.report(c)
            return
        c.rounds += 1
        c.progress_before = len(c.evidence) + len(c.coverage)
        if plan.route == "gather":
            c.stage = "gathering"
            c.batch = [self.task(c, a.role, a.question) for a in plan.assignments]
        else:
            self.reason(c)

    def reason(self, c: Control) -> None:
        c.stage = "reasoning"
        c.batch = [
            self.task(
                c,
                "reasoning",
                "Rank hypotheses using ALL settled specialist outcomes, support AND contradictions. Address prior objections; inconclusive is valid.",
            )
        ]

    def start_single(self, c: Control) -> None:
        c.stage = "single"
        c.batch = [
            self.task(
                c,
                "single",
                "Investigate the alert, test alternatives, and report the supported cause or justified uncertainty.",
            )
        ]

    def plan(self, c: Control) -> None:
        c.stage = "plan"
        c.batch = [
            self.task(
                c,
                "orchestrator",
                "Plan initial independent Observability and Dependency assignments for this alert. Include both roles.",
            )
        ]

    def review(self, c: Control) -> None:
        c.hypothesis_revision = c.batch[0]
        c.stage = "critic"
        c.batch = [
            self.task(
                c,
                "critic",
                "Review this exact hypothesis revision. Raise concrete objections and useful evidence questions, or return an empty objections list.",
            )
        ]

    def accept_review(self, c: Control) -> None:
        c.review_task = c.batch[0]
        for objection in c.tasks[c.review_task].result["objections"]:
            c.objections[objection["objection_id"]] = objection
        progress = len(c.evidence) + len(c.coverage) > c.progress_before
        c.no_progress = 0 if progress else c.no_progress + 1

    def decide(self, c: Control) -> None:
        c.stage = "decide"
        c.batch = [
            self.task(
                c,
                "orchestrator",
                "Dispose EVERY recorded objection: targeted gathering, reviewed reanalysis, or explicit unresolved with reason. Report if no useful work remains.",
            )
        ]

    def dispatch_tools(self, c: Control, task: Task) -> None:
        """Admit new queries; answer identical normalized queries from earlier receipts."""
        requests, task.requests = task.requests, []
        seen: set[str] = set()
        notes = []
        for request in requests:
            key = query_identity(self.snapshot, request["name"], request["arguments"])
            if key in seen:
                notes.append(f"{request['name']}: repeated within one action; skipped")
                continue
            seen.add(key)
            if key in c.query_index:
                self.replay(c, task, c.query_index[key], request)
                continue
            if task.tool_calls >= (
                c.limits.max_tool_calls if task.role == "single" else c.limits.task_tool_calls
            ):
                notes.append(f"{request['name']}: task tool-call allowance exhausted; not run")
                continue
            self.admit(c, task, "tool", request, query=key)
            if c.status != "running":
                return
        if notes:
            task.messages.append({"role": "user", "content": "Runtime note: " + "; ".join(notes)})
        if not task.pending:
            task.phase = "model"

    def replay(self, c: Control, task: Task, receipt_id: str, request: dict) -> None:
        """Duplicate query: return the earlier receipt's IDs and summary, charge no tool call."""
        receipt = c.tool_receipts[receipt_id]
        ids = receipt["page"]["observation_ids"]
        message = {
            "tool_receipt_id": receipt_id,
            "duplicate_query": True,
            "charged_tool_call": False,
            "note": "Identical normalized query already answered; ask a different question "
            "or return your final result.",
            **receipt,
        }
        if receipt_id not in task.visible_refs:
            # Another task retrieved it; this task has not seen the records yet.
            message["observations"] = [c.evidence[key] for key in ids]
            task.visible_refs.extend([receipt_id, *(k for k in ids if k not in task.visible_refs)])
        task.messages.append({"role": "user", "content": json.dumps(message)})
        c.deduplicated[task.role] = c.deduplicated.get(task.role, 0) + 1
        self.trace(
            "tool_dedupe",
            task_id=task.task_id,
            attempt_id=task.attempt_id,
            role=task.role,
            outcome="answered_from_receipt",
            receipt_id=receipt_id,
            tool=request["name"],
        )

    def receive(self, state: GraphState) -> Control:
        """The shared authoritative receipt commit and deadline check."""
        c = Control.model_validate(state["control"])
        c.dispatch = []
        # Saved completions win before an unresolved/error receipt can exhaust retries.
        receipts = state.get("inbox", {}).items()
        for call_id, receipt in sorted(receipts, key=lambda item: bool(item[1].get("error"))):
            if call_id in c.processed:
                continue
            c.processed.append(call_id)
            if c.status == "running":
                self.accept(c, c.calls[call_id], receipt)
        if c.status == "running" and time.monotonic() >= self.deadline:
            self.close(c, "resource_exhausted", "wall time limit")
        return c

    def schedule(self, c: Control) -> None:
        """Reserve calls serially before parallel workers execute them."""
        for task_id in c.batch:
            if c.status != "running":
                break
            task = c.tasks[task_id]
            if task.status != "running" or task.pending:
                continue
            if task.phase == "tools":
                self.dispatch_tools(c, task)
            if task.phase == "model" and task.status == "running" and not task.pending:
                self.admit(c, task, "model", {"messages": task.messages})

    def checkpoint(self, c: Control) -> dict:
        if c.status != "running":
            c.dispatch = []
        self.observer.sync(c)
        c.revision += 1
        self.trace(
            "state_transition",
            stage=c.stage,
            model_calls=sum(call.kind == "model" for call in c.calls.values()),
            tool_calls=sum(call.kind == "tool" for call in c.calls.values()),
            revision=c.revision,
            status=c.status,
            outcome=c.stop_reason or c.status,
        )
        return {"control": c.model_dump(mode="json")}

    async def execute(self, state: dict) -> dict:
        call = Call.model_validate(state["call"])
        if call.invocation_id is not None and call.invocation_id != self.invocation_id:
            return {
                "inbox": {
                    call.call_id: {
                        "payload": None,
                        "error": "prior invocation completion unknown",
                        "replay_blocked": True,
                        "retryable": True,
                        "usage": None,
                    }
                }
            }
        with self.observer.call(call) as span:
            return await self._execute(state, span)

    async def _execute(self, state: dict, span) -> dict:
        call = Call.model_validate(state["call"])
        started = time.monotonic()
        receipt = {"payload": None, "error": None, "usage": None}
        if span and call.kind == "model":
            resolve = getattr(self.model, "model_for_role", None)
            span.set_attribute(
                "llm.model_name",
                resolve(call.role)
                if resolve
                else "scripted"
                if isinstance(self.model, ScriptedModel)
                else type(self.model).__name__,
            )
        try:
            async with asyncio.timeout(max(0.001, self.deadline - time.monotonic())):
                async with self.semaphore, runner_capacity():
                    if time.monotonic() >= self.deadline:
                        raise TimeoutError("run deadline expired before dispatch")
                    timeout = min(
                        self.limits.call_seconds, max(0.001, self.deadline - time.monotonic())
                    )
                    async with asyncio.timeout(timeout):
                        if call.kind == "model":
                            messages, schema = model_input(call.role, call.request)
                            self.observer.input(span, messages)
                            result = await self.model.complete(messages, schema)
                            receipt["payload"] = result.action.get("action", result.action)
                            receipt["raw_id"] = result.raw_id
                            receipt["model"] = result.model
                            receipt["finish_reason"] = result.finish_reason
                            receipt["provider_latency_ms"] = result.latency_ms
                            if result.usage:
                                receipt["usage"] = {
                                    "input_tokens": result.usage.input_tokens,
                                    "output_tokens": result.usage.output_tokens,
                                    "cost": result.usage.cost,
                                }
                        else:

                            async def query():
                                payload = await self.registry[call.request["name"]].ainvoke(
                                    call.request["arguments"], config={"callbacks": []}
                                )
                                return ToolPage.model_validate(payload)

                            self.observer.input(span, call.request)
                            fault = FaultSelection(**call.fault)
                            page = await apply_fault(query, fault, timeout_seconds=timeout)()
                            receipt["payload"] = page.model_dump(mode="json")
                            if page.source_status == "failed":
                                receipt["error"] = (
                                    page.error.message if page.error else "source unavailable"
                                )
                                receipt["retryable"] = page.error.retryable if page.error else True
        except Exception as exc:
            receipt["error"] = f"{type(exc).__name__}: {exc}"
            receipt["retryable"] = getattr(exc, "retryable", True)
            if call.kind == "model" and isinstance(exc, ModelCallError) and exc.before_send:
                receipt["before_send"] = True
                receipt["usage"] = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0}
        self.observer.output(span, receipt)
        receipt["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
        self.trace(
            f"{call.kind}_call",
            task_id=call.task_id,
            attempt_id=call.attempt_id,
            call_id=call.call_id,
            role=call.role,
            tokens=receipt["usage"] or "unknown",
            model=receipt.get("model"),
            generation_id=receipt.get("raw_id"),
            finish_reason=receipt.get("finish_reason"),
            latency_ms=receipt["latency_ms"],
            outcome=receipt["error"] or "received",
        )
        return {"inbox": {call.call_id: receipt}}

    def graph(self):
        """Select a workflow while sharing admission, execution and receipts."""
        if self.system == "single":
            from incident_investigator.single import build_single_graph

            return build_single_graph(self)
        from incident_investigator.staged import build_staged_graph

        return build_staged_graph(self)


def model_names(model: ModelAdapter, system: str = "multi") -> dict[str, str]:
    resolve = getattr(model, "model_for_role", None)
    return {
        role: resolve(role)
        if resolve
        else "scripted"
        if isinstance(model, ScriptedModel)
        else "gpt-6-luna"
        if isinstance(model, CodexExecModel)
        else type(model).__name__
        for role in (["single"] if system == "single" else [r for r in PAYLOADS if r != "single"])
    }


async def saved_progress(compiled, config) -> dict | None:
    """Include the active nested stage when exporting interrupted progress."""
    saved = await compiled.aget_state(config, subgraphs=True)
    states = []

    def visit(state):
        if isinstance(state.values, dict) and "control" in state.values:
            states.append(state.values)
        for task in state.tasks:
            if task.state is not None and hasattr(task.state, "values"):
                visit(task.state)

    visit(saved)
    return max(states, key=lambda state: state["control"]["revision"]) if states else None


async def investigate(
    snapshot: CaseSnapshot | None,
    model: ModelAdapter,
    out: Path | None = None,
    *,
    limits: Limits | None = None,
    semaphore: asyncio.Semaphore | None = None,
    faults: dict[str, FaultSelection] | None = None,
    graph: GraphVariant = DEFAULT_GRAPH,
    system: Literal["multi", "single"] = "multi",
    resume: Path | None = None,
    _run_id: str | None = None,
) -> Path:
    """Start or resume one fenced investigation under its original finite limits."""
    from langsmith import tracing_context

    from incident_investigator.reporting import export_report, retain_snapshot
    from incident_investigator.workspace import home, write_json

    if graph != DEFAULT_GRAPH:
        raise ValueError(f"unknown graph: {graph}")
    out = out if out is not None else home() / "runs"
    run_dir = Path(resume) if resume else out / (_run_id or identity())
    if not resume:
        run_dir.mkdir(parents=True, exist_ok=False)
    with invocation_lock(run_dir):
        models = model_names(model, system)
        if resume:
            metadata, restored = resume_metadata(run_dir)
            if snapshot is not None and snapshot.dataset_digest != restored.dataset_digest:
                raise ValueError("incompatible resume dataset")
            snapshot = restored
            system = metadata.get("system", "multi")
            models = model_names(model, system)
            if models != metadata["models"]:
                raise ValueError("incompatible resume model configuration")
            if limits is not None and limits.model_dump() != metadata["limits"]:
                raise ValueError("resume must retain saved limits")
            limits = Limits.model_validate(metadata["limits"])
            graph = metadata["graph"]
            faults = {key: FaultSelection(**value) for key, value in metadata["faults"].items()}
        else:
            if snapshot is None:
                raise ValueError("a snapshot is required for a fresh investigation")
            limits = limits or Limits()
            metadata = {
                "run_id": run_dir.name,
                "launch_id": os.environ.get("II_LAUNCH_ID"),
                "created": time.time(),
                "dataset_digest": snapshot.dataset_digest,
                "case_id": snapshot.case_id,
                "limits": limits.model_dump(),
                "graph": graph,
                "system": system,
                "models": models,
                "deadline_at": time.time() + limits.wall_seconds,
                "versions": versions(snapshot),
                "faults": {
                    key: {
                        "outcome": value.outcome,
                        "keep_first": value.keep_first,
                        "rule_id": value.rule_id,
                    }
                    for key, value in (faults or {}).items()
                },
            }
            save_source(snapshot, run_dir)
            retain_snapshot(snapshot, run_dir)
            write_json(run_dir / "run.json", metadata)
        runner = Investigation(
            snapshot,
            model,
            run_dir,
            limits,
            semaphore or asyncio.Semaphore(4),
            faults=faults,
            graph=graph,
            system=system,
        )
        runner.deadline = time.monotonic() + max(0, metadata["deadline_at"] - time.time())
        initial = Control(
            run_id=metadata["run_id"],
            case_id=snapshot.case_id,
            limits=limits,
            models=models,
            graph=graph,
            system=system,
        )
        config = {
            "configurable": {"thread_id": metadata["run_id"]},
            "callbacks": [],
            "recursion_limit": 1000,
        }
        with TraceSession(
            metadata["run_id"], snapshot.case_id, graph=graph, system=system
        ) as observer:
            runner.observer = observer
            await observer.save_link(run_dir, final=False)
            async with AsyncSqliteSaver.from_conn_string(str(run_dir / "state.sqlite")) as saver:
                compiled = runner.graph().compile(checkpointer=saver)
                try:
                    with tracing_context(enabled=False):
                        result = await compiled.ainvoke(
                            None
                            if resume
                            else {"control": initial.model_dump(mode="json"), "inbox": {}},
                            config=config,
                            durability="sync",
                        )
                except BaseException as exc:
                    # Export is derived; the checkpoint remains resumable at its last durable boundary.
                    progress = await saved_progress(compiled, config)
                    if progress:
                        progress = copy.deepcopy(progress)
                        c = progress["control"]
                        c["status"] = (
                            "interrupted"
                            if isinstance(
                                exc, asyncio.CancelledError | KeyboardInterrupt | NodeCancelledError
                            )
                            else "failed"
                        )
                        c["partial"] = True
                        c["stop_reason"] = c["status"]
                        export_report(progress, snapshot, run_dir)
                    raise
            observer.finish(result["control"])
        export_report(result, snapshot, run_dir)
        await runner.observer.save_link(run_dir)
    return run_dir
