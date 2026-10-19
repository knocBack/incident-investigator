"""Frozen-snapshot tools exposed to investigation agents."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.tools import StructuredTool

from .contracts import QUERY_MODELS
from .core import (
    catalog,
    changes,
    dependencies,
    history,
    log_counts,
    logs,
    metric_stats,
    metrics,
    query_identity,
    trace_logs,
)

TOOL_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "catalog": catalog,
    "logs": logs,
    "log_counts": log_counts,
    "trace_logs": trace_logs,
    "metrics": metrics,
    "metric_stats": metric_stats,
    "changes": changes,
    "dependencies": dependencies,
    "history": history,
}
_ENUMS = (
    "Valid service: checkout|inventory|database. Valid severity: "
    "TRACE|DEBUG|INFO|WARN|ERROR|FATAL. Valid change_kind: deployment|configuration. "
    "Metric aggregations: gauge|count|sum|rate|mean|p50|p95|p99. Timestamps are ISO-8601 "
    "with offset and must not be after the cutoff."
)
DESCRIPTIONS = {
    "catalog": "Discover what exists up to the cutoff before guessing names: services; metric "
    "names with units, aggregations, window sizes and time ranges; log severities, top message "
    "templates with counts, attribute keys, operations and versions; change kinds, versions and "
    "config keys; dependency targets; earlier incident ids and titles. Optional service filter.",
    "logs": "Raw log records for one service (default window: alert start to cutoff), filtered "
    "by severity, version, operation, request_id, trace_id, transaction_id or body substring. "
    "Paged: pass next_cursor to continue.",
    "log_counts": "Grouped log counts (no raw paging): group_by any of service, severity, "
    "template, operation, version; optional attribute (e.g. status) to split by its value; "
    "optional bucket_seconds for a time series. Defaults to all services and all data up to "
    "the cutoff. One exemplar record per group is attached as citable evidence.",
    "trace_logs": "Every log row for one trace_id or request_id across all services, in time "
    "order. Paged.",
    "metrics": "Raw metric samples for one service (default window: alert start to cutoff), "
    "optionally one metric_name or window_seconds. Paged.",
    "metric_stats": "Per-series statistics (count, min, max, mean, first, last) for metric "
    "names per service, and before/after values around split_at. Defaults to all services and "
    "all data up to the cutoff. The peak sample per series is attached as citable evidence.",
    "changes": "Deployment and configuration change records for one service with before/after "
    "values. Paged.",
    "dependencies": "Latest observed downstream targets of one service at as_of (default "
    "cutoff). One hop; query the next service separately.",
    "history": "Earlier incidents involving one service, optionally matching text. Paged.",
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
            description=f"{DESCRIPTIONS[name]} {_ENUMS} Returns observations, never a diagnosis.",
            args_schema=QUERY_MODELS[name],
        )
    return registry


__all__ = [
    "DESCRIPTIONS",
    "TOOL_FUNCTIONS",
    "build_registry",
    "catalog",
    "changes",
    "dependencies",
    "history",
    "log_counts",
    "logs",
    "metric_stats",
    "metrics",
    "query_identity",
    "trace_logs",
]
