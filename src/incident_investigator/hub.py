"""Deprecated controller-hub topology, retained only for optional comparisons.

Deprecated; removed if final evaluation shows no difference. Shared validation,
call admission and execution remain in runtime.py; only legacy routing lives here.
"""

from __future__ import annotations

from functools import partial

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from incident_investigator.contracts import PAYLOADS, Plan
from incident_investigator.runtime import Control, GraphState, Investigation


def advance(runner: Investigation, c: Control) -> None:
    """Advance the legacy hub stage machine after its current batch settles."""
    tasks = [c.tasks[key] for key in c.batch]
    if any(task.status == "running" for task in tasks):
        return
    if c.stage != "gathering" and tasks[0].status != "succeeded":
        reason = f"{tasks[0].role}: {tasks[0].failure}"
        if c.stage == "reporting":
            runner.close(c, "failed", reason)
        else:
            runner.report(c, partial_reason=reason)
        return
    if c.stage in {"plan", "decide"}:
        plan = Plan.model_validate(tasks[0].result)
        for disposition in plan.dispositions:
            c.dispositions[disposition.objection_id] = disposition.model_dump()
        if plan.route == "report" or c.no_progress >= c.limits.max_no_progress_rounds:
            runner.report(c)
        else:
            runner.round(c, plan)
    elif c.stage == "gathering":
        runner.reason(c)
    elif c.stage == "reasoning":
        runner.review(c)
    elif c.stage == "critic":
        runner.accept_review(c)
        if c.objections:
            runner.decide(c)
        else:
            runner.report(c)
    elif c.stage == "reporting":
        c.status = "completed"


async def controller(runner: Investigation, state: GraphState) -> dict:
    """Commit receipts and schedule one bounded wave for the legacy hub."""
    c = runner.receive(state)
    # A task can settle at admission (allowance spent) with nothing dispatched; advance
    # again so the graph never stops while running. Each pass settles or dispatches work.
    for _ in range(8):
        if c.status != "running":
            break
        if c.stage == "start":
            runner.plan(c)
        else:
            advance(runner, c)
        runner.schedule(c)
        if c.dispatch:
            break
    return runner.checkpoint(c)


def build_hub_graph(runner: Investigation) -> StateGraph:
    """Build the deprecated hub topology only for an explicit legacy selection."""
    graph = StateGraph(GraphState)
    graph.add_node("controller", partial(controller, runner))
    for role in PAYLOADS:
        graph.add_node(role, runner.execute)
        graph.add_edge(role, "controller")
    graph.add_node("tool", runner.execute)
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

    # Send destinations need explicit metadata for draw_mermaid()/Studio.
    # This does not change dispatch or the hub graph's execution order.
    graph.add_conditional_edges("controller", route, [*PAYLOADS, "tool", END])
    return graph
