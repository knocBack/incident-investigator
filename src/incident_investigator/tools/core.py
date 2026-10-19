"""Pure snapshot queries with stable paging and provenance."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, get_args

from pydantic import ValidationError

from incident_investigator.sources.loader import CaseSnapshot
from incident_investigator.telemetry.models import Service, canonical_json
from incident_investigator.tools.contracts import (
    QUERY_MODELS,
    Failure,
    LogCountsQuery,
    MetricStatsQuery,
    PagedQuery,
    QueryModel,
    ToolPage,
    TraceLogsQuery,
)

MAX_PAGE_BYTES = 32768
# Discovery/aggregation tools scan the whole snapshot by default, including baseline data.
UNBOUNDED_START = {"history", "log_counts", "metric_stats", "trace_logs"}
SEVERITY_RANK = {"FATAL": 5, "ERROR": 4, "WARN": 3, "INFO": 2, "DEBUG": 1, "TRACE": 0}
MAX_GROUPS = 100
MAX_EXEMPLARS = 20
_VARIABLE = re.compile(r"\b(?:[0-9a-f]{8,}|[0-9a-f-]{36}|\d+(?:\.\d+)?)\b", re.IGNORECASE)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _filters(query: QueryModel, snapshot: CaseSnapshot, tool: str) -> dict[str, Any]:
    values = query.model_dump(mode="python")
    values.pop("cursor", None)
    values.pop("limit", None)
    if tool == "dependencies":
        values["as_of"] = values.get("as_of") or snapshot.cutoff
    elif tool != "catalog":
        values["start"] = values.get("start") or (
            None if tool in UNBOUNDED_START else snapshot.alert_start
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
        elif isinstance(value, list):
            values[key] = list(dict.fromkeys(value))
        elif isinstance(value, str) and key != "cursor":
            if not value.strip():
                raise ValueError(f"{key} must be nonempty")
            values[key] = value.strip()
    return values


def _normalized(
    tool: str, raw: dict[str, Any], snapshot: CaseSnapshot
) -> tuple[QueryModel, dict[str, Any], str]:
    try:
        query = QUERY_MODELS[tool].model_validate(raw)
    except ValidationError as exc:
        raise ValueError(describe_validation(exc)) from exc
    filters = _filters(query, snapshot, tool)
    if tool == "dependencies":
        if datetime.fromisoformat(filters["as_of"].replace("Z", "+00:00")) > snapshot.cutoff:
            raise ValueError("as_of must not be after cutoff")
    elif tool != "catalog":
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


def describe_validation(exc: ValidationError, prefix: str = "") -> str:
    """Name each failing field path and its rule, e.g. `arguments.severity: Input should be ...`."""
    lines = []
    for error in exc.errors():
        path = ".".join(str(part) for part in (prefix, *error["loc"]) if part != "")
        lines.append(f"{path or '<root>'}: {error['msg']} (got {error.get('input')!r:.80})")
    return "; ".join(lines)


def query_identity(snapshot: CaseSnapshot, tool: str, arguments: dict[str, Any]) -> str:
    """Stable identity of one normalized page request: query key plus page position."""
    query, _, key = _normalized(tool, arguments, snapshot)
    position = {"limit": getattr(query, "limit", None), "cursor": getattr(query, "cursor", None)}
    return hashlib.sha256(canonical_json([key, position]).encode()).hexdigest()


def _point(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _within(
    snapshot: CaseSnapshot, moment: datetime, start: datetime | None, end: datetime
) -> bool:
    # The interval is half-open except at the cutoff, so cutoff-instant records stay retrievable.
    return (start is None or start <= moment) and (moment < end or moment == end == snapshot.cutoff)


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
    start = datetime.fromisoformat(filters["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(filters["end"].replace("Z", "+00:00"))

    def match(item: Any) -> bool:
        data = item.data
        return (
            item.source == "logs"
            and _available(snapshot, item)
            and data["service"] == filters["service"]
            and _within(snapshot, item.event_time, start, end)
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


def _metric_match(
    snapshot: CaseSnapshot,
    item: Any,
    filters: dict[str, Any],
    start: datetime | None,
    end: datetime,
) -> bool:
    data = item.data
    if (
        item.source != "metrics"
        or not _available(snapshot, item)
        or (filters.get("service") is not None and data["service"] != filters["service"])
        or (filters.get("metric_name") is not None and data["name"] != filters["metric_name"])
    ):
        return False
    window_start, window_end = _point(data["window_start"]), _point(data["window_end"])
    if window_start is None:
        if not _within(snapshot, item.event_time, start, end):
            return False
    elif not ((start is None or window_start >= start) and window_end <= end):
        return False
    return filters.get("window_seconds") is None or (
        window_start is not None
        and (window_end - window_start).total_seconds() == filters["window_seconds"]
    )


async def metrics(snapshot: CaseSnapshot, **arguments: Any) -> ToolPage:
    query, filters, key = _normalized("metrics", arguments, snapshot)
    start = datetime.fromisoformat(filters["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(filters["end"].replace("Z", "+00:00"))

    def match(item: Any) -> bool:
        return _metric_match(snapshot, item, filters, start, end) and all(
            item.data.get(name) == filters[name]
            for name in ("version", "operation")
            if filters.get(name) is not None
        )

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
    start = datetime.fromisoformat(filters["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(filters["end"].replace("Z", "+00:00"))
    matches = [
        item
        for item in snapshot.observations
        if item.source == "changes"
        and _available(snapshot, item)
        and item.data["service"] == filters["service"]
        and _within(snapshot, item.event_time, start, end)
        and all(
            item.data.get(name) == filters[name]
            for name in ("change_kind", "version")
            if filters.get(name) is not None
        )
    ]
    return _page("changes", snapshot, query, filters, key, matches)


async def dependencies(snapshot: CaseSnapshot, **arguments: Any) -> ToolPage:
    _, filters, key = _normalized("dependencies", arguments, snapshot)
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
    start, end = _point(filters["start"]), _point(filters["end"])

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
            and _within(snapshot, item.event_time, start, end)
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


def _template(body: str) -> str:
    """Collapse variable tokens (numbers, hex IDs) so repeated messages group together."""
    return _VARIABLE.sub("<n>", body)


def _eligible(snapshot: CaseSnapshot, source: str) -> list[Any]:
    rows = [
        item
        for item in snapshot.observations
        if item.source == source
        and _available(snapshot, item)
        and not (source == "history" and item.data["incident_id"] == snapshot.case_id)
    ]
    return sorted(rows, key=lambda item: (item.event_time, item.source_record_id))


def _summary_page(
    tool: str,
    snapshot: CaseSnapshot,
    filters: dict[str, Any],
    key: str,
    summary: dict[str, Any],
    matched: int,
    exemplars: list[Any] = (),
) -> ToolPage:
    """Bounded derived page; exemplar source records are attached as citable observations."""
    attached = list(exemplars)
    while True:
        page = ToolPage(
            tool=tool,
            dataset_digest=snapshot.dataset_digest,
            query_key=key,
            query=filters,
            cutoff=snapshot.cutoff,
            source_status="complete",
            observations=[_observation(snapshot, item) for item in attached],
            available_match_count=matched,
            next_cursor=None,
            error=None,
            summary=summary,
        )
        if len(page.model_dump_json().encode()) <= MAX_PAGE_BYTES:
            return page
        if not attached:
            return _failed(
                tool,
                snapshot,
                filters,
                key,
                "summary exceeds the response bound; narrow service, metric_name or time range",
            )
        dropped = _observation(snapshot, attached.pop())["evidence_id"]
        _forget_exemplar(summary, dropped)


def _forget_exemplar(value: Any, evidence_id: str) -> None:
    if isinstance(value, dict):
        for name, item in value.items():
            if item == evidence_id:
                value[name] = None
            else:
                _forget_exemplar(item, evidence_id)
    elif isinstance(value, list):
        for item in value:
            _forget_exemplar(item, evidence_id)


async def catalog(snapshot: CaseSnapshot, **arguments: Any) -> ToolPage:
    """What exists up to the cutoff: names, units, templates, keys and ranges, not findings."""
    _, filters, key = _normalized("catalog", arguments, snapshot)
    service = filters.get("service")

    def owned(item: Any) -> bool:
        if service is None:
            return True
        if item.source == "history":
            return service in item.data["services"]
        return item.data["service"] == service

    rows = {
        source: [item for item in _eligible(snapshot, source) if owned(item)]
        for source in ("logs", "metrics", "changes", "dependencies", "history")
    }
    services: set[str] = set()
    for source_rows in rows.values():
        for item in source_rows:
            services.update(
                item.data["services"] if item.source == "history" else [item.data["service"]]
            )
            services.update(item.data.get("targets", []))
    series: dict[tuple, dict[str, Any]] = {}
    for item in rows["metrics"]:
        data = item.data
        entry = series.setdefault(
            (data["service"], data["name"], data["unit"]),
            {
                "service": data["service"],
                "name": data["name"],
                "unit": data["unit"],
                "aggregations": set(),
                "window_seconds": set(),
                "samples": 0,
                "first_event": _timestamp(item.event_time),
            },
        )
        entry["aggregations"].add(data["aggregation"])
        if data["window_start"]:
            entry["window_seconds"].add(
                int((_point(data["window_end"]) - _point(data["window_start"])).total_seconds())
            )
        entry["samples"] += 1
        entry["last_event"] = _timestamp(item.event_time)
    templates: dict[tuple, dict[str, Any]] = {}
    severities: dict[str, int] = {}
    attribute_keys: dict[str, int] = {}
    for item in rows["logs"]:
        data = item.data
        severities[data["severity"]] = severities.get(data["severity"], 0) + 1
        for name in data["attributes"]:
            attribute_keys[name] = attribute_keys.get(name, 0) + 1
        group = templates.setdefault(
            (data["service"], data["severity"], _template(data["body"])),
            {
                "service": data["service"],
                "severity": data["severity"],
                "template": _template(data["body"]),
                "count": 0,
                "attribute_keys": set(),
                "first_event": _timestamp(item.event_time),
            },
        )
        group["count"] += 1
        group["attribute_keys"].update(data["attributes"])
        group["last_event"] = _timestamp(item.event_time)
    ranked = sorted(
        templates.values(),
        key=lambda group: (-SEVERITY_RANK[group["severity"]], -group["count"]),
    )
    latest_dependencies: dict[str, Any] = {}
    for item in rows["dependencies"]:
        latest_dependencies[item.data["service"]] = item.data["targets"]

    def plain(value: Any) -> Any:
        if isinstance(value, set):
            return sorted(value)
        if isinstance(value, dict):
            return {name: plain(item) for name, item in value.items()}
        return value

    summary = {
        "alert_window": {
            "start": _timestamp(snapshot.alert_start),
            "cutoff": _timestamp(snapshot.cutoff),
        },
        "services": sorted(services),
        "sources": {
            source: {
                "records": len(source_rows),
                "first_event": _timestamp(source_rows[0].event_time) if source_rows else None,
                "last_event": _timestamp(source_rows[-1].event_time) if source_rows else None,
            }
            for source, source_rows in rows.items()
        },
        "metrics": [plain(entry) for entry in series.values()][:60],
        "logs": {
            "severities": severities,
            "templates": [plain(group) for group in ranked[:40]],
            "templates_omitted": max(0, len(ranked) - 40),
            "attribute_keys": attribute_keys,
            "operations": sorted({item.data["operation"] for item in rows["logs"]} - {None}),
            "versions": sorted({item.data["version"] for item in rows["logs"]} - {None}),
        },
        "changes": [
            {
                "service": item.data["service"],
                "change_kind": item.data["change_kind"],
                "version": item.data["version"],
                "config_keys": [change["key"] for change in item.data["changes"]],
                "description": item.data["description"],
                "event_time": _timestamp(item.event_time),
            }
            for item in rows["changes"][-30:]
        ],
        "dependencies": latest_dependencies,
        "history": [
            {
                "incident_id": item.data["incident_id"],
                "title": item.data["title"],
                "services": item.data["services"],
                "event_time": _timestamp(item.event_time),
            }
            for item in rows["history"][-30:]
        ],
        "valid_values": {
            "service": list(get_args(Service)),
            "severity": list(SEVERITY_RANK)[::-1],
            "change_kind": ["deployment", "configuration"],
            "aggregation": ["gauge", "count", "sum", "rate", "mean", "p50", "p95", "p99"],
        },
    }
    return _summary_page(
        "catalog", snapshot, filters, key, summary, sum(len(r) for r in rows.values())
    )


async def log_counts(snapshot: CaseSnapshot, **arguments: Any) -> ToolPage:
    """Grouped log counts per optional time bucket, with one citable exemplar per group key."""
    query, filters, key = _normalized("log_counts", arguments, snapshot)
    assert isinstance(query, LogCountsQuery)
    start, end = _point(filters["start"]), _point(filters["end"])
    rows = [
        item
        for item in _eligible(snapshot, "logs")
        if (filters["service"] is None or item.data["service"] == filters["service"])
        and _within(snapshot, item.event_time, start, end)
        and (filters["severity"] is None or item.data["severity"] == filters["severity"])
        and (
            filters["text_contains"] is None
            or filters["text_contains"] in item.data["body"].casefold()
        )
    ]
    dimensions = filters["group_by"]
    attribute = filters["attribute"]
    bucket = filters["bucket_seconds"]
    groups: dict[str, dict[str, Any]] = {}
    exemplars: dict[str, tuple[Any, dict[str, Any]]] = {}
    for item in rows:
        data = item.data
        values = {
            name: _template(data["body"]) if name == "template" else data[name]
            for name in dimensions
        }
        if attribute:
            values["attribute:" + attribute] = data["attributes"].get(attribute)
        identity = canonical_json(values)
        exemplars.setdefault(identity, (item, values))
        moment = item.event_time.timestamp()
        bucket_start = (
            _timestamp(datetime.fromtimestamp(moment - moment % bucket, UTC)) if bucket else None
        )
        group = groups.setdefault(
            canonical_json([identity, bucket_start]),
            {
                "key": values,
                "bucket_start": bucket_start,
                "count": 0,
                "first_event": _timestamp(item.event_time),
                "identity": identity,
                "rank": SEVERITY_RANK[data["severity"]],
            },
        )
        group["count"] += 1
        group["last_event"] = _timestamp(item.event_time)
    totals: dict[str, int] = {}
    for group in groups.values():
        totals[group["identity"]] = totals.get(group["identity"], 0) + group["count"]
    priority = sorted(
        exemplars,
        key=lambda identity: (
            -SEVERITY_RANK[exemplars[identity][0].data["severity"]],
            -totals[identity],
        ),
    )
    chosen = priority[:MAX_EXEMPLARS]
    exemplar_ids = {
        identity: _observation(snapshot, exemplars[identity][0])["evidence_id"]
        for identity in chosen
    }
    kept = sorted(groups.values(), key=lambda group: (-group["rank"], -group["count"]))[:MAX_GROUPS]
    kept.sort(key=lambda group: (group["bucket_start"] or "", -group["rank"], -group["count"]))
    summary = {
        "group_by": dimensions,
        "attribute": attribute,
        "bucket_seconds": bucket,
        "total_records": len(rows),
        "groups": [
            {
                "key": group["key"],
                "bucket_start": group["bucket_start"],
                "count": group["count"],
                "first_event": group["first_event"],
                "last_event": group["last_event"],
                "exemplar_evidence_id": exemplar_ids.get(group["identity"]),
            }
            for group in kept
        ],
        "groups_omitted": len(groups) - len(kept),
    }
    return _summary_page(
        "log_counts",
        snapshot,
        filters,
        key,
        summary,
        len(rows),
        [exemplars[identity][0] for identity in chosen],
    )


def _stats(values: list[tuple[datetime, float]]) -> dict[str, Any] | None:
    if not values:
        return None
    numbers = [value for _, value in values]
    return {
        "count": len(numbers),
        "min": min(numbers),
        "max": max(numbers),
        "mean": round(sum(numbers) / len(numbers), 6),
        "last": numbers[-1],
    }


async def metric_stats(snapshot: CaseSnapshot, **arguments: Any) -> ToolPage:
    """Per-series statistics and optional before/after split, with the peak sample attached."""
    query, filters, key = _normalized("metric_stats", arguments, snapshot)
    assert isinstance(query, MetricStatsQuery)
    start, end = _point(filters["start"]), _point(filters["end"])
    split = _point(filters["split_at"])
    rows = [
        item
        for item in _eligible(snapshot, "metrics")
        if _metric_match(snapshot, item, filters, start, end)
    ]
    series: dict[tuple, list[Any]] = {}
    for item in rows:
        data = item.data
        window = (
            int((_point(data["window_end"]) - _point(data["window_start"])).total_seconds())
            if data["window_start"]
            else None
        )
        series.setdefault(
            (data["service"], data["name"], data["unit"], data["aggregation"], window), []
        ).append(item)
    entries, peaks = [], []
    for (service, name, unit, aggregation, window), items in list(series.items())[:50]:
        points = [(item.event_time, item.data["value"]) for item in items]
        peak = max(items, key=lambda item: (item.data["value"], item.event_time))
        entry = {
            "service": service,
            "name": name,
            "unit": unit,
            "aggregation": aggregation,
            "window_seconds": window,
            **(_stats(points) or {}),
            "first": {"value": points[0][1], "event_time": _timestamp(points[0][0])},
            "last_event": _timestamp(points[-1][0]),
            "peak_evidence_id": _observation(snapshot, peak)["evidence_id"]
            if len(peaks) < MAX_EXEMPLARS
            else None,
        }
        if split is not None:
            entry["before_split"] = _stats([p for p in points if p[0] < split])
            entry["after_split"] = _stats([p for p in points if p[0] >= split])
        if entry["peak_evidence_id"]:
            peaks.append(peak)
        entries.append(entry)
    summary = {
        "split_at": filters["split_at"],
        "series": entries,
        "series_omitted": max(0, len(series) - 50),
    }
    return _summary_page("metric_stats", snapshot, filters, key, summary, len(rows), peaks)


async def trace_logs(snapshot: CaseSnapshot, **arguments: Any) -> ToolPage:
    """Every log row sharing a trace_id/request_id across services, in time order."""
    query, filters, key = _normalized("trace_logs", arguments, snapshot)
    assert isinstance(query, TraceLogsQuery)
    start, end = _point(filters["start"]), _point(filters["end"])
    matches = [
        item
        for item in _eligible(snapshot, "logs")
        if _within(snapshot, item.event_time, start, end)
        and all(
            item.data[name] == filters[name]
            for name in ("trace_id", "request_id")
            if filters[name] is not None
        )
    ]
    return _page("trace_logs", snapshot, query, filters, key, matches)
