"""Topology, durable call boundaries, and graph-aware evaluation artifacts."""

import asyncio
import json

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import NodeCancelledError
from test_m3_agents import Scenario

from incident_investigator.contracts import Limits
from incident_investigator.grading.grader import expectation, grade_run
from incident_investigator.grading.suite import leaderboard
from incident_investigator.models import ScriptedModel
from incident_investigator.runtime import Control, Investigation, investigate
from incident_investigator.sources.loader import load_snapshot


def runner(tmp_path, variant, model=None):
    return Investigation(
        load_snapshot("dev-00"),
        model or ScriptedModel(),
        tmp_path,
        Limits(),
        asyncio.Semaphore(4),
        graph=variant,
    )


def test_explicit_topology_exposes_stage_order_and_role_call_loops(tmp_path):
    compiled = runner(tmp_path, "explicit").graph().compile()
    top = compiled.get_graph()
    assert "controller" not in top.nodes
    assert {
        "orchestrator_plan",
        "gathering",
        "reasoning",
        "critic",
        "orchestrator_decide",
        "reporting",
    } <= top.nodes.keys()
    edges = {(edge.source, edge.target) for edge in top.edges}
    assert {
        ("__start__", "orchestrator_plan"),
        ("orchestrator_plan", "gathering"),
        ("gathering", "reasoning"),
        ("reasoning", "critic"),
        ("critic", "orchestrator_decide"),
        ("orchestrator_decide", "gathering"),
        ("orchestrator_decide", "reasoning"),
        ("orchestrator_decide", "reporting"),
        ("reporting", "__end__"),
    } <= edges
    expanded = compiled.get_graph(xray=True)
    edges = {(edge.source, edge.target) for edge in expanded.edges}
    for role in ("observability", "dependency"):
        assert f"gathering:{role}" in expanded.nodes
        assert ("gathering:commit", f"gathering:{role}") in edges
        assert (f"gathering:{role}", "gathering:commit") in edges
    assert ("gathering:commit", "gathering:tool") in edges
    assert ("gathering:tool", "gathering:commit") in edges


@pytest.mark.parametrize("variant", ["current", "explicit"])
async def test_resume_does_not_repeat_completed_model_or_tool_calls(tmp_path, variant):
    class Crash(Scenario):
        crashed = False

        async def complete(self, messages, schema):
            role = json.loads(messages[1]["content"])["role"]
            if role == "observability" and self.role_calls.get(role) == 1 and not self.crashed:
                # Let the sibling's final model call finish in this superstep.
                await asyncio.sleep(0.02)
                self.crashed = True
                raise asyncio.CancelledError("simulated process interruption")
            return await super().complete(messages, schema)

    model = Crash(followup=False)
    investigation = runner(tmp_path, variant, model)
    initial = Control(run_id="resume", case_id="dev-00", limits=Limits(), graph=variant)
    config = {"configurable": {"thread_id": "resume"}, "recursion_limit": 1000}
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "state.sqlite")) as saver:
        compiled = investigation.graph().compile(checkpointer=saver)
        with pytest.raises(NodeCancelledError):
            await compiled.ainvoke(
                {"control": initial.model_dump(mode="json"), "inbox": {}},
                config,
                durability="sync",
            )
        before = (tmp_path / "trace.jsonl").read_text().splitlines()
        assert sum(json.loads(row)["event"] == "tool_call" for row in before) == 5
        assert model.role_calls["dependency"] == 2
        # Rebuild the graph/runner as a fresh process would; only the adapter script
        # is retained here so it supplies the remaining local responses.
        resumed = runner(tmp_path, variant, model).graph().compile(checkpointer=saver)
        result = await resumed.ainvoke(None, config, durability="sync")
    assert result["control"]["status"] == "completed"
    after = (tmp_path / "trace.jsonl").read_text().splitlines()
    assert sum(json.loads(row)["event"] == "tool_call" for row in after) == 5
    assert model.role_calls["orchestrator"] == 1
    assert model.role_calls["observability"] == model.role_calls["dependency"] == 2


async def test_graph_labels_flow_through_results_trace_grades_and_leaderboard(tmp_path):
    grades = []
    for variant in ("current", "explicit"):
        directory = await investigate(
            load_snapshot("dev-00"),
            Scenario(followup=False),
            tmp_path,
            graph=variant,
        )
        result = json.loads((directory / "result.json").read_text())
        assert result["graph"] == result["state"]["graph"] == variant
        assert all(
            json.loads(row)["graph"] == variant
            for row in (directory / "trace.jsonl").read_text().splitlines()
        )
        grades.append(grade_run(directory, expectation("bad_configuration")))
    board = leaderboard(grades, tmp_path / "board")
    # Regression after concurrency/pass-metrics integration: standalone grades
    # have no suite_attempt; aggregating them must still retain both graph groups.
    assert {row["graph"] for row in board["models"]} == {"current", "explicit"}
    assert all(row["runs"] == 1 for row in board["models"])


@pytest.mark.parametrize("variant", ["current", "explicit"])
@pytest.mark.parametrize("command", ["investigate", "demo"])
def test_cli_selects_graph(tmp_path, monkeypatch, capsys, variant, command):
    from incident_investigator.cli import main

    args = [
        "incident-investigator",
        command,
        "--graph",
        variant,
        "--model",
        "scripted",
        "--script",
        "tests/scripted-demo.json",
        "--out",
        str(tmp_path),
    ]
    if command == "investigate":
        args += ["--case", "dev-00"]
    monkeypatch.setattr("sys.argv", args)
    main()
    from pathlib import Path

    directory = Path(capsys.readouterr().out.strip()).parent
    result = json.loads((directory / "result.json").read_text())
    assert result["graph"] == variant
    assert result["status"] == "completed"
