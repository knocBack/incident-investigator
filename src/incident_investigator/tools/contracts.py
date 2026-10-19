"""Typed retrieval queries and response envelopes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from incident_investigator.telemetry.models import Service, Severity

ToolName = Literal[
    "catalog",
    "logs",
    "log_counts",
    "trace_logs",
    "metrics",
    "metric_stats",
    "changes",
    "dependencies",
    "history",
]


class QueryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("service", mode="before", check_fields=False)
    @classmethod
    def normalize_service(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("start", "end", "as_of", "split_at", mode="before", check_fields=False)
    @classmethod
    def aware_time(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @field_validator("start", "end", "as_of", "split_at", check_fields=False)
    @classmethod
    def utc_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamp must include an offset")
        return value.astimezone(UTC) if value else value

    @field_validator("cursor", check_fields=False)
    @classmethod
    def bounded_cursor(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode()) > 2048:
            raise ValueError("cursor exceeds 2048 bytes")
        return value


class PagedQuery(QueryModel):
    service: Service
    start: datetime | None = None
    end: datetime | None = None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = None


class LogsQuery(PagedQuery):
    severity: Severity | None = None
    version: str | None = Field(default=None, max_length=256)
    operation: str | None = Field(default=None, max_length=256)
    request_id: str | None = Field(default=None, max_length=256)
    trace_id: str | None = Field(default=None, max_length=256)
    transaction_id: str | None = Field(default=None, max_length=256)
    text_contains: str | None = Field(default=None, max_length=256)


class MetricsQuery(PagedQuery):
    metric_name: str | None = Field(default=None, max_length=256)
    window_seconds: int | None = Field(default=None, gt=0)
    version: str | None = Field(default=None, max_length=256)
    operation: str | None = Field(default=None, max_length=256)


class ChangesQuery(PagedQuery):
    change_kind: Literal["deployment", "configuration"] | None = None
    version: str | None = Field(default=None, max_length=256)


class DependenciesQuery(QueryModel):
    service: Service
    as_of: datetime | None = None


class HistoryQuery(PagedQuery):
    text_contains: str | None = Field(default=None, max_length=256)


class CatalogQuery(QueryModel):
    service: Service | None = None


class LogCountsQuery(QueryModel):
    service: Service | None = None
    start: datetime | None = None
    end: datetime | None = None
    severity: Severity | None = None
    text_contains: str | None = Field(default=None, max_length=256)
    group_by: list[Literal["service", "severity", "template", "operation", "version"]] = Field(
        default_factory=lambda: ["service", "severity", "template"], max_length=5
    )
    attribute: str | None = Field(default=None, max_length=256)
    bucket_seconds: int | None = Field(default=None, ge=1, le=86400)


class MetricStatsQuery(QueryModel):
    service: Service | None = None
    start: datetime | None = None
    end: datetime | None = None
    metric_name: str | None = Field(default=None, max_length=256)
    window_seconds: int | None = Field(default=None, gt=0)
    split_at: datetime | None = None


class TraceLogsQuery(QueryModel):
    trace_id: str | None = Field(default=None, max_length=256)
    request_id: str | None = Field(default=None, max_length=256)
    start: datetime | None = None
    end: datetime | None = None
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = None

    @model_validator(mode="after")
    def correlation_id(self) -> TraceLogsQuery:
        if not (self.trace_id or self.request_id):
            raise ValueError("trace_id or request_id is required")
        return self


class Failure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    message: str
    retryable: bool = False


class ToolPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: ToolName
    dataset_digest: str
    query_key: str
    query: dict[str, Any]
    cutoff: datetime
    source_status: Literal["complete", "partial", "failed"]
    observations: list[dict[str, Any]]
    available_match_count: int | None
    next_cursor: str | None
    error: Failure | None
    # Derived counts/statistics for discovery and aggregation tools; never a diagnosis.
    summary: dict[str, Any] | None = None


QUERY_MODELS: dict[str, type[QueryModel]] = {
    "catalog": CatalogQuery,
    "logs": LogsQuery,
    "log_counts": LogCountsQuery,
    "trace_logs": TraceLogsQuery,
    "metrics": MetricsQuery,
    "metric_stats": MetricStatsQuery,
    "changes": ChangesQuery,
    "dependencies": DependenciesQuery,
    "history": HistoryQuery,
}
