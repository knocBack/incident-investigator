"""LLD-02 source observation envelopes and deterministic dataset hashing."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

Service = Literal["checkout", "inventory", "database"]
Severity = Literal["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"]
JSONValue = dict[str, Any] | list[Any] | str | int | float | bool | None
JSONScalar = str | int | float | bool | None


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LogPayload(StrictModel):
    service: Service
    severity: Severity
    body: str
    version: str | None
    operation: str | None
    request_id: str | None
    trace_id: str | None
    transaction_id: str | None
    attributes: dict[str, JSONScalar]


class MetricPayload(StrictModel):
    service: Service
    name: str
    value: float
    unit: str
    aggregation: Literal["gauge", "count", "sum", "rate", "mean", "p50", "p95", "p99"]
    window_start: datetime | None
    window_end: datetime | None
    version: str | None
    operation: str | None
    sample_count: int | None
    attributes: dict[str, JSONScalar]

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: float) -> float:
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("metric value must be finite")
        return value

    @model_validator(mode="after")
    def valid_window(self) -> MetricPayload:
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("metric windows must provide both endpoints or neither")
        if self.window_start and self.window_end and self.window_start >= self.window_end:
            raise ValueError("metric window_start must precede window_end")
        if self.sample_count is not None and self.sample_count < 0:
            raise ValueError("sample_count must be nonnegative")
        return self


class Change(StrictModel):
    key: str
    before: JSONValue
    after: JSONValue


class ChangePayload(StrictModel):
    service: Service
    change_kind: Literal["deployment", "configuration"]
    version: str | None
    description: str
    changes: list[Change]
    attributes: dict[str, JSONScalar]


class DependencyPayload(StrictModel):
    service: Service
    targets: list[Service]

    @field_validator("targets")
    @classmethod
    def sorted_unique(cls, targets: list[Service]) -> list[Service]:
        if targets != sorted(set(targets)):
            raise ValueError("dependency targets must be sorted and unique")
        return targets


class HistoryPayload(StrictModel):
    incident_id: str
    services: list[Service]
    title: str
    summary: str
    symptoms: list[str]
    root_cause: str | None
    resolution: str | None

    @field_validator("services")
    @classmethod
    def sorted_services(cls, services: list[Service]) -> list[Service]:
        if not services or services != sorted(set(services)):
            raise ValueError("history services must be nonempty, sorted, and unique")
        return services


class SourceObservation(StrictModel):
    source: Literal["logs", "metrics", "changes", "dependencies", "history"]
    source_record_id: str
    event_time: datetime
    available_at: datetime
    data: dict[str, Any]

    @field_validator("event_time", "available_at")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_payload(self) -> SourceObservation:
        payload_type = {
            "logs": LogPayload,
            "metrics": MetricPayload,
            "changes": ChangePayload,
            "dependencies": DependencyPayload,
            "history": HistoryPayload,
        }[self.source]
        payload = payload_type.model_validate(self.data)
        if isinstance(payload, MetricPayload) and payload.window_end is not None:
            if payload.window_end.astimezone(UTC) != self.event_time:
                raise ValueError("window metric event_time must equal window_end")
            if self.available_at < self.event_time:
                raise ValueError("window metric cannot be available before its end")
        self.data = payload.model_dump(mode="json")
        return self


def _normalize(value: Any) -> Any:
    if isinstance(value, datetime):
        utc = value.astimezone(UTC)
        return utc.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_normalize(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Canonical JSON required by the frozen-source contract."""
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def dataset_digest(
    alert: dict[str, Any], observations: list[SourceObservation], fault_plan: dict[str, Any]
) -> str:
    ordered = sorted(observations, key=lambda item: (item.source, item.source_record_id))
    rules = sorted(fault_plan.get("rules", []), key=lambda rule: rule["rule_id"])
    payload = {
        "schema_version": "tool-contract.v1",
        "alert": alert,
        "observations": [item.model_dump(mode="python") for item in ordered],
        "fault_plan": {**fault_plan, "rules": rules},
    }
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()
