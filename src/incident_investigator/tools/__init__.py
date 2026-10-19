"""Frozen-snapshot tools exposed to investigation agents."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.tools import StructuredTool

from .contracts import ChangesQuery, DependenciesQuery, HistoryQuery, LogsQuery, MetricsQuery
from .core import changes, dependencies, history, logs, metrics

TOOL_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "logs": logs,
    "metrics": metrics,
    "changes": changes,
    "dependencies": dependencies,
    "history": history,
}
_SCHEMAS = {
    "logs": LogsQuery,
    "metrics": MetricsQuery,
    "changes": ChangesQuery,
    "dependencies": DependenciesQuery,
    "history": HistoryQuery,
}


def build_registry(snapshot: Any) -> dict[str, StructuredTool]:
    """Create native schemas without exposing snapshots/cutoffs as model arguments."""
    registry: dict[str, StructuredTool] = {}
    for name, function in TOOL_FUNCTIONS.items():

        async def invoke(_function: Callable[..., Any] = function, **kwargs: Any) -> dict[str, Any]:
            return (await _function(snapshot, **kwargs)).model_dump(mode="json")

        registry[name] = StructuredTool.from_function(
            coroutine=invoke,
            name=name,
            description=f"Retrieve frozen {name} observations; returns evidence, never a diagnosis.",
            args_schema=_SCHEMAS[name],
        )
    return registry


__all__ = [
    "TOOL_FUNCTIONS",
    "build_registry",
    "changes",
    "dependencies",
    "history",
    "logs",
    "metrics",
]
