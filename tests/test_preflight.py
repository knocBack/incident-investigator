from __future__ import annotations

import aiosqlite
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import START, StateGraph


@pytest.mark.asyncio
async def test_sqlite_checkpoint_can_be_read_only_after_sync_write(tmp_path):
    checkpoint_path = tmp_path / "state.sqlite"

    async def step(state: dict[str, int]) -> dict[str, int]:
        return {"count": state["count"] + 1}

    graph = StateGraph(dict)
    graph.add_node("step", step)
    graph.add_edge(START, "step")

    async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
        app = graph.compile(checkpointer=saver)
        result = await app.ainvoke(
            {"count": 0},
            {"configurable": {"thread_id": "preflight"}},
            durability="sync",
        )

    assert result["count"] == 1
    uri = f"file:{checkpoint_path}?mode=ro"
    async with aiosqlite.connect(uri, uri=True) as connection:
        row = await (await connection.execute("SELECT COUNT(*) FROM checkpoints")).fetchone()

    assert row is not None and row[0] > 0
