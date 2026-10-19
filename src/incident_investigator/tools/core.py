"""Pure snapshot queries with stable paging and provenance."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from incident_investigator.sources.loader import CaseSnapshot
from incident_investigator.telemetry.models import canonical_json
from incident_investigator.tools.contracts import (
    ChangesQuery,
    DependenciesQuery,
    Failure,
    HistoryQuery,
    LogsQuery,
    MetricsQuery,
    PagedQuery,
    QueryModel,
    ToolPage,
)

MAX_PAGE_BYTES = 32768


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _filters(query: QueryModel, snapshot: CaseSnapshot, tool: str) -> dict[str, Any]:
    values = query.model_dump(mode="python")
    values.pop("cursor", None)
    values.pop("limit", None)
    if tool == "dependencies":
        values["as_of"] = values.get("as_of") or snapshot.cutoff
    else:
        values["start"] = values.get("start") or (
            None if tool == "history" else snapshot.alert_start
        )
        values["end"] = values.get("end") or snapshot.cutoff
    for key, value in list(values.items()):
        if isinstance(value, datetime):
            values[key] = _timestamp(value)
        elif key == "text_contains" and value is not None:
            normalized = value.strip().casefold()
            if not normalized:
                raise ValueError("text_contains must be nonempty")
            values[key] = normalized
        elif isinstance(value, str) and key != "cursor":
            if not value.strip():
                raise ValueError(f"{key} must be nonempty")
            values[key] = value.strip()
    return values


def _normalized(
    tool: str, raw: dict[str, Any], snapshot: CaseSnapshot
) -> tuple[QueryModel, dict[str, Any], str]:
    try:
        query = {
            "logs": LogsQuery,
            "metrics": MetricsQuery,
            "changes": ChangesQuery,
            "dependencies": DependenciesQuery,
            "history": HistoryQuery,
        }[tool].model_validate(raw)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc
    filters = _filters(query, snapshot, tool)
    if tool == "dependencies":
        if datetime.fromisoformat(filters["as_of"].replace("Z", "+00:00")) > snapshot.cutoff:
            raise ValueError("as_of must not be after cutoff")
    else:
        start, end = filters["start"], filters["end"]
        parsed_end = datetime.fromisoformat(end.replace("Z", "+00:00"))
        if start:
            parsed_start = datetime.fromisoformat(start.replace("Z", "+00:00"))
            if parsed_start >= parsed_end:
                raise ValueError("start must precede end")
        if parsed_end > snapshot.cutoff:
            raise ValueError("end must not be after cutoff")
    key = hashlib.sha256(
        canonical_json(
            {
                "tool_contract_version": "tool-contract.v1",
                "tool": tool,
                "resolved_filters": filters,
                "dataset_digest": snapshot.dataset_digest,
                "cutoff": _timestamp(snapshot.cutoff),
            }
        ).encode()
    ).hexdigest()
    return query, filters, key


def _observation(snapshot: CaseSnapshot, item: Any) -> dict[str, Any]:
    payload = item.model_dump(mode="json")
    payload["dataset_digest"] = snapshot.dataset_digest
    payload["evidence_id"] = hashlib.sha256(
        canonical_json(
            {
                "dataset_digest": snapshot.dataset_digest,
                "source": item.source,
                "source_record_id": item.source_record_id,
            }
        ).encode()
    ).hexdigest()
    return payload


def _failed(
    tool: str, snapshot: CaseSnapshot, query: dict[str, Any], key: str, message: str
) -> ToolPage:
    return ToolPage(
        tool=tool,
        dataset_digest=snapshot.dataset_digest,
        query_key=key,
        query=query,
        cutoff=snapshot.cutoff,
        source_status="failed",
        observations=[],
        available_match_count=None,
        next_cursor=None,
        error=Failure(code="invalid_query", message=message),
    )


def _cursor(key: str, snapshot: CaseSnapshot, item: Any) -> str:
    raw = canonical_json(
        {
            "version": 1,
            "dataset_digest": snapshot.dataset_digest,
            "query_key": key,
            "last": [_timestamp(item.event_time), item.source_record_id],
        }
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_index(cursor: str | None, matches: list[Any], key: str, snapshot: CaseSnapshot) -> int:
    if not cursor:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
        if (
            value["version"] != 1
            or value["dataset_digest"] != snapshot.dataset_digest
            or value["query_key"] != key
        ):
            raise ValueError("cursor belongs to another query")
        last = tuple(value["last"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    for index, item in enumerate(matches):
        if (_timestamp(item.event_time), item.source_record_id) == last:
            return index + 1
    raise ValueError("cursor position is absent from this query")


def _page(
    tool: str,
    snapshot: CaseSnapshot,
    query: PagedQuery,
    filters: dict[str, Any],
    key: str,
    matches: list[Any],
) -> ToolPage:
    matches.sort(key=lambda item: (item.event_time, item.source_record_id))
    try:
        start_index = _cursor_index(query.cursor, matches, key, snapshot)
    except ValueError as exc:
        return _failed(tool, snapshot, filters, key, str(exc))
    selected: list[dict[str, Any]] = []
    for item in matches[start_index : start_index + query.limit]:
        candidate = selected + [_observation(snapshot, item)]
        # Conservatively include a possible continuation cursor in the byte test.
        continuation = (
            _cursor(key, snapshot, item) if start_index + len(candidate) < len(matches) else None
        )
        page = ToolPage(
            tool=tool,
            dataset_digest=snapshot.dataset_digest,
            query_key=key,
            query=filters,
            cutoff=snapshot.cutoff,
            source_status="complete",
            observations=candidate,
            available_match_count=len(matches),
            next_cursor=continuation,
            error=None,
        )
        if len(page.model_dump_json().encode()) > MAX_PAGE_BYTES:
            if not selected:
                return _failed(tool, snapshot, filters, key, "one record cannot fit response bound")
            break
        selected = candidate
    end_index = start_index + len(selected)
    next_cursor = (
        _cursor(key, snapshot, matches[end_index - 1])
        if end_index < len(matches) and selected
        else None
    )
    return ToolPage(
        tool=tool,
        dataset_digest=snapshot.dataset_digest,
        query_key=key,
        query=filters,
        cutoff=snapshot.cutoff,
        source_status="complete",
        observations=selected,
        available_match_count=len(matches),
        next_cursor=next_cursor,
        error=None,
    )


def _available(snapshot: CaseSnapshot, item: Any) -> bool:
    return item.available_at <= snapshot.cutoff


async def logs(snapshot: CaseSnapshot, **arguments: Any) -> ToolPage:
    query, filters, key = _normalized("logs", arguments, snapshot)
    assert isinstance(query, LogsQuery)
    start = datetime.fromisoformat(filters["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(filters["end"].replace("Z", "+00:00"))

    def match(item: Any) -> bool:
        data = item.data
        return (
            item.source == "logs"
            and _available(snapshot, item)
            and data["service"] == filters["service"]
            and start <= item.event_time < end
            and all(
                data.get(name) == filters[name]
                for name in (
                    "severity",
                    "version",
                    "operation",
                    "request_id",
                    "trace_id",
                    "transaction_id",
                )
                if filters.get(name) is not None
            )
            and (
                filters.get("text_contains") is None
                or filters["text_contains"] in data["body"].casefold()
            )
        )

    return _page(
        "logs",
        snapshot,
        query,
        filters,
        key,
        [item for item in snapshot.observations if match(item)],
    )


async def metrics(snapshot: CaseSnapshot, **arguments: Any) -> ToolPage:
    query, filters, key = _normalized("metrics", arguments, snapshot)
    assert isinstance(query, MetricsQuery)
    start = datetime.fromisoformat(filters["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(filters["end"].replace("Z", "+00:00"))

    def match(item: Any) -> bool:
        data = item.data
        if (
            item.source != "metrics"
            or not _available(snapshot, item)
            or data["service"] != filters["service"]
        ):
            return False
        ws, we = data["window_start"], data["window_end"]
        window_start = datetime.fromisoformat(ws.replace("Z", "+00:00")) if ws else None
        window_end = datetime.fromisoformat(we.replace("Z", "+00:00")) if we else None
        if (ws is None and not start <= item.event_time < end) or (
            ws is not None and not (window_start >= start and window_end <= end)
        ):
            return False
        if filters.get("window_seconds") is not None and (
            ws is None or (window_end - window_start).total_seconds() != filters["window_seconds"]
        ):
            return False
        return all(
            data.get(name) == filters[name]
            for name in ("version", "operation")
            if filters.get(name) is not None
        ) and (filters.get("metric_name") is None or data["name"] == filters["metric_name"])

    return _page(
        "metrics",
        snapshot,
        query,
        filters,
        key,
        [item for item in snapshot.observations if match(item)],
    )


async def changes(snapshot: CaseSnapshot, **arguments: Any) -> ToolPage:
    query, filters, key = _normalized("changes", arguments, snapshot)
    assert isinstance(query, ChangesQuery)
    start = datetime.fromisoformat(filters["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(filters["end"].replace("Z", "+00:00"))
    matches = [
        item
        for item in snapshot.observations
        if item.source == "changes"
        and _available(snapshot, item)
        and item.data["service"] == filters["service"]
        and start <= item.event_time < end
        and all(
            item.data.get(name) == filters[name]
            for name in ("change_kind", "version")
            if filters.get(name) is not None
        )
    ]
    return _page("changes", snapshot, query, filters, key, matches)


async def dependencies(snapshot: CaseSnapshot, **arguments: Any) -> ToolPage:
    query, filters, key = _normalized("dependencies", arguments, snapshot)
    assert isinstance(query, DependenciesQuery)
    as_of = datetime.fromisoformat(filters["as_of"].replace("Z", "+00:00"))
    matches = [
        item
        for item in snapshot.observations
        if item.source == "dependencies"
        and item.data["service"] == filters["service"]
        and item.event_time <= as_of
        and item.available_at <= as_of
    ]
    latest = max(matches, key=lambda item: item.event_time, default=None)
    return ToolPage(
        tool="dependencies",
        dataset_digest=snapshot.dataset_digest,
        query_key=key,
        query=filters,
        cutoff=snapshot.cutoff,
        source_status="complete",
        observations=[] if latest is None else [_observation(snapshot, latest)],
        available_match_count=0 if latest is None else 1,
        next_cursor=None,
        error=None,
    )


async def history(snapshot: CaseSnapshot, **arguments: Any) -> ToolPage:
    query, filters, key = _normalized("history", arguments, snapshot)
    assert isinstance(query, HistoryQuery)
    start = (
        datetime.fromisoformat(filters["start"].replace("Z", "+00:00"))
        if filters["start"]
        else None
    )
    end = datetime.fromisoformat(filters["end"].replace("Z", "+00:00"))

    def match(item: Any) -> bool:
        if item.source != "history" or not _available(snapshot, item):
            return False
        data = item.data
        values = [
            data["title"],
            data["summary"],
            *data["symptoms"],
            data["root_cause"],
            data["resolution"],
        ]
        return (
            data["incident_id"] != snapshot.case_id
            and filters["service"] in data["services"]
            and (start is None or start <= item.event_time)
            and item.event_time < end
            and (
                filters.get("text_contains") is None
                or any(filters["text_contains"] in value.casefold() for value in values if value)
            )
        )

    return _page(
        "history",
        snapshot,
        query,
        filters,
        key,
        [item for item in snapshot.observations if match(item)],
    )
