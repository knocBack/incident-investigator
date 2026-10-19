"""Bounded six-role graph: serialized ownership, parallel single-call workers."""

from __future__ import annotations

import asyncio
import json
import time
from functools import reduce
from operator import or_
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
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
from incident_investigator.models import ModelAdapter
from incident_investigator.sources.loader import CaseSnapshot
from incident_investigator.tools import build_registry
from incident_investigator.tools.contracts import QUERY_MODELS, ToolPage
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


class Call(Record):
    call_id: str
    task_id: str
    attempt_id: str
    role: Role
    kind: Literal["model", "tool"]
    request: dict[str, Any]
    retry: int = 0
    retry_of: str | None = None
    fault: dict[str, Any] | None = None


class Control(Record):
    run_id: str
    case_id: str
    limits: Limits
    revision: int = 0
    status: Literal["running", "completed", "failed", "resource_exhausted"] = "running"
    stop_reason: str | None = None
    stage: str = "start"
    tasks: dict[str, Task] = Field(default_factory=dict)
    batch: list[str] = Field(default_factory=list)
    calls: dict[str, Call] = Field(default_factory=dict)
    evidence: dict[str, dict[str, Any]] = Field(default_factory=dict)
    processed: list[str] = Field(default_factory=list)
    tool_receipts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    coverage: list[str] = Field(default_factory=list)
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


def provider_schema(role: str) -> dict:
    """Concrete tool argument schemas avoid free-form objects in strict output."""
    final = create_model(
        PAYLOADS[role].__name__ + "Final",
        __base__=Record,
        kind=(Literal["final"], ...),
        result=(PAYLOADS[role], ...),
    )
    variants = [final]
    if role in ALLOWED_TOOLS:
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
    ) -> None:
        self.snapshot, self.model, self.out, self.limits = snapshot, model, out, limits
        self.semaphore = semaphore
        self.registry = build_registry(snapshot)
        self.faults = faults or {}
        self.started = time.monotonic()
        self.deadline = self.started + limits.wall_seconds
        self.adapters = {role: TypeAdapter(action_type(body)) for role, body in PAYLOADS.items()}

    def trace(self, event: str, **values: Any) -> None:
        row = {"run_id": self.out.name, "event": event, "at": time.time(), **values}
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
            "tools": {
                name: QUERY_MODELS[name].model_json_schema()
                for name in ALLOWED_TOOLS.get(role, set())
            },
        }
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

    def check_refs(self, value: Any, allowed: set[str]) -> None:
        if isinstance(value, dict):
            if (
                "kind" in value
                and value["kind"] in {"observation", "tool_receipt"}
                and value.get("id") not in allowed
            ):
                raise ValueError("unknown or invisible evidence reference")
            for key, item in value.items():
                if key in {"supporting_evidence", "contradicting_evidence", "evidence_ids"}:
                    if not set(item) <= allowed:
                        raise ValueError("unknown or invisible evidence ID")
                else:
                    self.check_refs(item, allowed)
        elif isinstance(value, list):
            for item in value:
                self.check_refs(item, allowed)

    def validate_result(self, c: Control, task: Task, result: dict) -> None:
        self.check_refs(result, set(task.visible_refs))
        if task.role == "orchestrator":
            plan = Plan.model_validate(result)
            if c.stage == "plan":
                if (
                    plan.route != "gather"
                    or {a.role for a in plan.assignments} != {"observability", "dependency"}
                    or plan.dispositions
                    or any(a.objection_id for a in plan.assignments)
                ):
                    raise ValueError("initial plan must gather with both specialist roles")
            else:
                ids = [d.objection_id for d in plan.dispositions]
                if len(set(ids)) != len(ids) or set(ids) != set(c.objections):
                    raise ValueError("decide must dispose every recorded objection exactly once")
                for disposition in plan.dispositions:
                    assigned = any(
                        a.objection_id == disposition.objection_id for a in plan.assignments
                    )
                    if (disposition.outcome == "gather") != assigned:
                        raise ValueError("gather disposition requires a targeted assignment")
                    if disposition.outcome == "reanalyze" and plan.route != "reanalyze":
                        raise ValueError("reanalysis requires reviewed reanalysis route")
                if any(a.objection_id not in c.objections for a in plan.assignments):
                    raise ValueError("follow-up must target a known objection")
                if plan.route == "report" and any(
                    d.outcome != "unresolved" for d in plan.dispositions
                ):
                    raise ValueError(
                        "report closure must explicitly preserve unresolved objections"
                    )
            if (plan.route == "gather") != bool(plan.assignments):
                raise ValueError("only gathering can carry assignments")
        elif task.role == "reasoning":
            ids = [h["hypothesis_id"] for h in result["hypotheses"]]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate hypothesis identity")
        elif task.role in {"critic", "reporting"}:
            if result["revision_id"] != c.hypothesis_revision:
                raise ValueError("wrong hypothesis revision")
            if task.role == "critic":
                hypotheses = c.tasks[c.hypothesis_revision].result["hypotheses"]
                known = {h["hypothesis_id"] for h in hypotheses}
                ids = [o["objection_id"] for o in result["objections"]]
                if len(ids) != len(set(ids)) or any(key in c.objections for key in ids):
                    raise ValueError("objection IDs must be new and unique")
                if any(
                    o["hypothesis_id"] is not None and o["hypothesis_id"] not in known
                    for o in result["objections"]
                ):
                    raise ValueError("unknown hypothesis in objection")
            elif set(result["unresolved_objection_ids"]) != set(c.objections):
                raise ValueError("report must include every unresolved objection")

    def fault_selection(self, c: Control, name: str, arguments: dict) -> FaultSelection:
        if name in self.faults:
            return self.faults[name]
        for rule in self.snapshot.fault_plan["rules"]:
            if rule["tool"] != name or rule["service"] != arguments["service"]:
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
                        else (None if name == "history" else self.snapshot.alert_start)
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
    ) -> None:
        total = sum(call.kind == kind for call in c.calls.values())
        cap = c.limits.max_model_calls if kind == "model" else c.limits.max_tool_calls
        count = task.model_calls if kind == "model" else task.tool_calls
        task_cap = c.limits.task_model_calls if kind == "model" else c.limits.task_tool_calls
        if time.monotonic() >= self.deadline or total >= cap or count >= task_cap:
            self.close(c, "resource_exhausted", f"{kind} call/task or wall time limit")
            return
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
            retry=retry,
            retry_of=retry_of,
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
        if receipt.get("error"):
            if call.retry < c.limits.call_retries:
                self.admit(
                    c, task, call.kind, call.request, retry=call.retry + 1, retry_of=call.call_id
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
                    and page.query_key not in c.coverage
                ):
                    c.coverage.append(page.query_key)
            c.tool_receipts[call.call_id] = {
                "tool": call.request["name"],
                "error": receipt.get("error"),
                "page": payload,
            }
            task.visible_refs.append(call.call_id)
            task.messages.append(
                {
                    "role": "user",
                    "content": json.dumps(
                        {"tool_receipt_id": call.call_id, **c.tool_receipts[call.call_id]}
                    ),
                }
            )
            if not task.pending:
                task.phase = "model"
            return
        action = receipt["payload"]
        task.messages.append({"role": "assistant", "content": json.dumps(action)})
        try:
            parsed = self.adapters[task.role].validate_python(action)
            data = parsed.model_dump(mode="json")
            if data["kind"] == "tools":
                allowed = ALLOWED_TOOLS.get(task.role, set())
                for request in data["requests"]:
                    if request["name"] not in allowed:
                        raise ValueError("role is not allowed this tool")
                    QUERY_MODELS[request["name"]].model_validate(request["arguments"])
                task.requests = data["requests"]
                task.phase = "tools"
            else:
                self.validate_result(c, task, data["result"])
                task.result, task.status, task.phase = data["result"], "succeeded", "settled"
            self.trace(
                "validation",
                task_id=task.task_id,
                attempt_id=task.attempt_id,
                call_id=call.call_id,
                role=task.role,
                outcome="accepted",
            )
        except (ValueError, ValidationError) as exc:
            self.trace(
                "validation",
                task_id=task.task_id,
                attempt_id=task.attempt_id,
                call_id=call.call_id,
                role=task.role,
                outcome="rejected",
                error=str(exc)[:1000],
            )
            if task.corrections == 0:
                task.corrections += 1
                task.phase = "model"
                task.messages.append(
                    {"role": "user", "content": f"Correct this invalid action once: {exc}"}
                )
            else:
                task.status, task.phase, task.failure = (
                    "failed",
                    "settled",
                    f"invalid output: {exc}",
                )

    def report(self, c: Control) -> None:
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
            self.close(c, "resource_exhausted", "review round limit prevents required follow-up")
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

    def advance(self, c: Control) -> None:
        tasks = [c.tasks[key] for key in c.batch]
        if any(task.status == "running" for task in tasks):
            return
        if c.stage != "gathering" and tasks[0].status != "succeeded":
            self.close(c, "failed", f"{tasks[0].role}: {tasks[0].failure}")
            return
        if c.stage in {"plan", "decide"}:
            plan = Plan.model_validate(tasks[0].result)
            for disposition in plan.dispositions:
                c.dispositions[disposition.objection_id] = disposition.model_dump()
            if plan.route == "report" or c.no_progress >= c.limits.max_no_progress_rounds:
                self.report(c)
            else:
                self.round(c, plan)
        elif c.stage == "gathering":
            self.reason(c)
        elif c.stage == "reasoning":
            c.hypothesis_revision = tasks[0].task_id
            c.stage = "critic"
            c.batch = [
                self.task(
                    c,
                    "critic",
                    "Review this exact hypothesis revision. Raise concrete objections and useful evidence questions, or return an empty objections list.",
                )
            ]
        elif c.stage == "critic":
            c.review_task = tasks[0].task_id
            for objection in tasks[0].result["objections"]:
                c.objections[objection["objection_id"]] = objection
            progress = len(c.evidence) + len(c.coverage) > c.progress_before
            c.no_progress = 0 if progress else c.no_progress + 1
            if c.objections:
                c.stage = "decide"
                c.batch = [
                    self.task(
                        c,
                        "orchestrator",
                        "Dispose EVERY recorded objection: targeted gathering, reviewed reanalysis, or explicit unresolved with reason. Report if no useful work remains.",
                    )
                ]
            else:
                self.report(c)
        elif c.stage == "reporting":
            c.status = "completed"

    async def controller(self, state: GraphState) -> dict:
        c = Control.model_validate(state["control"])
        c.dispatch = []
        for call_id, receipt in state.get("inbox", {}).items():
            if call_id in c.processed:
                continue
            c.processed.append(call_id)
            if c.status == "running":
                self.accept(c, c.calls[call_id], receipt)
        if c.status == "running" and time.monotonic() >= self.deadline:
            self.close(c, "resource_exhausted", "wall time limit")
        if c.status == "running":
            if c.stage == "start":
                c.stage = "plan"
                c.batch = [
                    self.task(
                        c,
                        "orchestrator",
                        "Plan initial independent Observability and Dependency assignments for this alert. Include both roles.",
                    )
                ]
            else:
                self.advance(c)
            for task_id in c.batch:
                if c.status != "running":
                    break
                task = c.tasks[task_id]
                if task.status != "running" or task.pending:
                    continue
                if task.phase == "model":
                    self.admit(c, task, "model", {"messages": task.messages})
                elif task.phase == "tools":
                    requests, task.requests = task.requests, []
                    for request in requests:
                        self.admit(c, task, "tool", request)
                        if c.status != "running":
                            break
        if c.status != "running":
            c.dispatch = []
        c.revision += 1
        self.trace(
            "state_transition",
            stage=c.stage,
            revision=c.revision,
            status=c.status,
            outcome=c.stop_reason or c.status,
        )
        return {"control": c.model_dump(mode="json")}

    async def execute(self, state: dict) -> dict:
        call = Call.model_validate(state["call"])
        started = time.monotonic()
        receipt = {"payload": None, "error": None, "usage": None}
        timeout = min(self.limits.call_seconds, max(0.001, self.deadline - started))
        try:
            async with asyncio.timeout(timeout), self.semaphore:
                if call.kind == "model":
                    schema = provider_schema(call.role)
                    messages = [
                        *call.request["messages"],
                        {
                            "role": "user",
                            "content": 'Return the tagged action as JSON. If the output schema wraps it in "action", use that wrapper. Do not execute any native tools, commands, web searches, file access or delegate. Only request the supplied frozen-source tools in the JSON action.',
                        },
                    ]
                    result = await self.model.complete(messages, schema)
                    receipt["payload"] = result.action.get("action", result.action)
                    if result.usage:
                        receipt["usage"] = {
                            "input_tokens": result.usage.input_tokens,
                            "output_tokens": result.usage.output_tokens,
                        }
                else:

                    async def query():
                        payload = await self.registry[call.request["name"]].ainvoke(
                            call.request["arguments"], config={"callbacks": []}
                        )
                        return ToolPage.model_validate(payload)

                    fault = FaultSelection(**call.fault)
                    page = await apply_fault(query, fault, timeout_seconds=timeout)()
                    receipt["payload"] = page.model_dump(mode="json")
        except Exception as exc:
            receipt["error"] = f"{type(exc).__name__}: {exc}"
        receipt["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
        self.trace(
            f"{call.kind}_call",
            task_id=call.task_id,
            attempt_id=call.attempt_id,
            call_id=call.call_id,
            role=call.role,
            tokens=receipt["usage"] or "unknown",
            latency_ms=receipt["latency_ms"],
            outcome=receipt["error"] or "received",
        )
        return {"inbox": {call.call_id: receipt}}

    def graph(self):
        graph = StateGraph(GraphState)
        graph.add_node("controller", self.controller)
        for role in PAYLOADS:
            graph.add_node(role, self.execute)
            graph.add_edge(role, "controller")
        graph.add_node("tool", self.execute)
        graph.add_edge("tool", "controller")
        graph.add_edge(START, "controller")

        def route(state):
            c = state["control"]
            if c["status"] != "running":
                return END
            return [
                Send(
                    c["calls"][key]["role"] if c["calls"][key]["kind"] == "model" else "tool",
                    {"call": c["calls"][key]},
                )
                for key in c["dispatch"]
            ]

        graph.add_conditional_edges("controller", route)
        return graph


async def investigate(
    snapshot: CaseSnapshot,
    model: ModelAdapter,
    out: Path = Path("runs"),
    *,
    limits: Limits | None = None,
    semaphore: asyncio.Semaphore | None = None,
    faults: dict[str, FaultSelection] | None = None,
) -> Path:
    """Run one fresh investigation. Each run owns its graph, files and fault counters."""
    from incident_investigator.reporting import export_report

    limits = limits or Limits()
    run_id = identity()
    run_dir = out / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    runner = Investigation(
        snapshot, model, run_dir, limits, semaphore or asyncio.Semaphore(4), faults=faults
    )
    initial = Control(run_id=run_id, case_id=snapshot.case_id, limits=limits)
    async with AsyncSqliteSaver.from_conn_string(str(run_dir / "state.sqlite")) as saver:
        graph = runner.graph().compile(checkpointer=saver)
        # callbacks=[] alone cannot override LANGSMITH_TRACING inherited by native tools.
        from langsmith import tracing_context

        with tracing_context(enabled=False):
            result = await graph.ainvoke(
                {"control": initial.model_dump(mode="json"), "inbox": {}},
                config={
                    "configurable": {"thread_id": run_id},
                    "callbacks": [],
                    "recursion_limit": 1000,
                },
                durability="sync",
            )
    export_report(result, snapshot, run_dir)
    return run_dir
