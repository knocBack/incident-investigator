"""Typed retrieval queries and response envelopes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from incident_investigator.telemetry.models import Service

ToolName = Literal["logs", "metrics", "changes", "dependencies", "history"]


class QueryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("service", mode="before", check_fields=False)
    @classmethod
    def normalize_service(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("start", "end", "as_of", mode="before", check_fields=False)
    @classmethod
    def aware_time(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @field_validator("start", "end", "as_of", check_fields=False)
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
    severity: Literal["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"] | None = None
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


QUERY_MODELS: dict[str, type[QueryModel]] = {
    "logs": LogsQuery,
    "metrics": MetricsQuery,
    "changes": ChangesQuery,
    "dependencies": DependenciesQuery,
    "history": HistoryQuery,
}
