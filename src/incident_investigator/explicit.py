"""Explicit stage edges with checkpointed, stage-local model/tool loops.

Gathering shares one admission/commit loop across its two parallel specialists.
This preserves global budgets, cross-task dedupe and authoritative provenance;
workers only publish immutable receipts through the inbox reducer.
"""

from __future__ import annotations

from collections.abc import Callable

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from incident_investigator.contracts import ALLOWED_TOOLS, Plan
from incident_investigator.runtime import Control, GraphState, Investigation


def _stage(
    runner: Investigation,
    roles: tuple[str, ...],
    enter: Callable[[Control], None],
    finish: Callable[[Control], None] | None = None,
):
    """One stage, with every external call in a separate checkpointed worker."""
    graph = StateGraph(GraphState)

    def begin(state):
        c = Control.model_validate(state["control"])
        enter(c)
        return {"control": c.model_dump(mode="json")}

    def commit(state):
        c = runner.receive(state)
        if c.status == "running":
            runner.schedule(c)
        return runner.checkpoint(c)

    def settle(state):
        c = Control.model_validate(state["control"])
        if c.status == "running" and finish:
            finish(c)
        return {"control": c.model_dump(mode="json")}

    def route(state):
        c = state["control"]
        if c["status"] != "running" or not c["dispatch"]:
            return "settle"
        return [
            Send(
                call["role"] if call["kind"] == "model" else "tool",
                {"call": call},
            )
            for key in c["dispatch"]
            for call in [c["calls"][key]]
        ]

    graph.add_node("begin", begin)
    graph.add_node("commit", commit)
    graph.add_node("settle", settle)
    workers = [*roles, *(["tool"] if any(role in ALLOWED_TOOLS for role in roles) else [])]
    for role in workers:
        graph.add_node(role, runner.execute)
        graph.add_edge(role, "commit")
    graph.add_edge(START, "begin")
    graph.add_edge("begin", "commit")
    graph.add_conditional_edges("commit", route, [*workers, "settle"])
    graph.add_edge("settle", END)
    return graph.compile()


def build_graph(runner: Investigation) -> StateGraph:
    """Workflow decisions are graph edges; no controller or advance() is used."""
    graph = StateGraph(GraphState)

    def save_plan(c):
        task = c.tasks[c.batch[0]]
        if task.status == "succeeded":
            for disposition in Plan.model_validate(task.result).dispositions:
                c.dispositions[disposition.objection_id] = disposition.model_dump()

    def gather(c):
        runner.round(c, Plan.model_validate(c.tasks[c.batch[0]].result))

    def reason(c):
        if c.stage in {"plan", "decide"}:
            runner.round(c, Plan.model_validate(c.tasks[c.batch[0]].result))
        else:
            runner.reason(c)

    def review_done(c):
        if c.tasks[c.batch[0]].status == "succeeded":
            runner.accept_review(c)

    def report(c):
        task = c.tasks[c.batch[0]]
        reason = (
            f"{task.role}: {task.failure}"
            if c.stage != "gathering" and task.status != "succeeded"
            else None
        )
        runner.report(c, partial_reason=reason)

    def report_done(c):
        task = c.tasks[c.batch[0]]
        if task.status == "succeeded":
            c.status = "completed"
        else:
            runner.close(c, "failed", f"{task.role}: {task.failure}")

    stages = {
        "orchestrator_plan": (("orchestrator",), runner.plan, save_plan),
        "gathering": (("observability", "dependency"), gather, None),
        "reasoning": (("reasoning",), reason, None),
        "critic": (("critic",), runner.review, review_done),
        "orchestrator_decide": (("orchestrator",), runner.decide, save_plan),
        "reporting": (("reporting",), report, report_done),
    }
    for name, (roles, enter, finish) in stages.items():
        graph.add_node(name, _stage(runner, roles, enter, finish))

    def failure(state):
        c = state["control"]
        if c["status"] != "running":
            return END
        if c["tasks"][c["batch"][0]]["status"] != "succeeded":
            return "reporting"
        return None

    def plan_route(state):
        if destination := failure(state):
            return destination
        c = state["control"]
        plan = c["tasks"][c["batch"][0]]["result"]
        if (
            plan["route"] == "report"
            or c["no_progress"] >= c["limits"]["max_no_progress_rounds"]
            or c["rounds"] >= c["limits"]["max_review_rounds"]
        ):
            return "reporting"
        return "gathering" if plan["route"] == "gather" else "reasoning"

    def gathered(state):
        return "reasoning" if state["control"]["status"] == "running" else END

    def reasoned(state):
        return failure(state) or "critic"

    def reviewed(state):
        return failure(state) or (
            "orchestrator_decide" if state["control"]["objections"] else "reporting"
        )

    graph.add_edge(START, "orchestrator_plan")
    for name in ("orchestrator_plan", "orchestrator_decide"):
        graph.add_conditional_edges(name, plan_route, ["gathering", "reasoning", "reporting", END])
    graph.add_conditional_edges("gathering", gathered, ["reasoning", END])
    graph.add_conditional_edges("reasoning", reasoned, ["critic", "reporting", END])
    graph.add_conditional_edges("critic", reviewed, ["orchestrator_decide", "reporting", END])
    graph.add_edge("reporting", END)
    return graph
