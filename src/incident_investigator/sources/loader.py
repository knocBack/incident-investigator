"""Manifest-only loader for immutable local incident snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from incident_investigator.telemetry.models import SourceObservation, dataset_digest


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alert: str
    observations: str
    fault_plan: str
    dataset_digest: str | None = None


class _Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cases: dict[str, _Entry] = Field(min_length=1)


@dataclass(frozen=True)
class CaseSnapshot:
    case_id: str
    alert: MappingProxyType
    observations: tuple[SourceObservation, ...]
    fault_plan: MappingProxyType
    dataset_digest: str
    cutoff: datetime
    alert_start: datetime


def _read_registered(root: Path, relative: str) -> bytes:
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError(f"source path escapes root: {relative}") from exc
    if candidate.is_symlink() or resolved.is_symlink() or not resolved.is_file():
        raise ValueError(f"source path must be a regular non-symlink file: {relative}")
    return resolved.read_bytes()


def _json_object(raw: bytes, name: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid {name} JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _utc(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an aware timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must be an aware timestamp")
    return parsed.astimezone(UTC)


def load_snapshot(
    case_id: str,
    source_root: Path = Path("fixtures/dev-00"),
    expected_digest: str | None = None,
    *,
    max_record_bytes: int = 8192,
    max_snapshot_bytes: int = 8 * 1024 * 1024,
) -> CaseSnapshot:
    """Load exactly one manifest-registered case without walking the source tree."""
    root = source_root.resolve(strict=True)
    if root.is_symlink() or "evals" in root.parts:
        raise ValueError("unsafe source root")
    manifest = _Manifest.model_validate(
        _json_object(_read_registered(root, "manifest.json"), "manifest")
    )
    if case_id not in manifest.cases:
        raise ValueError(f"unknown case ID: {case_id}")
    entry = manifest.cases[case_id]
    alert = _json_object(_read_registered(root, entry.alert), "alert")
    fault_plan = _json_object(_read_registered(root, entry.fault_plan), "fault plan")
    if set(fault_plan) != {"rules"} or not isinstance(fault_plan["rules"], list):
        raise ValueError("fault plan must contain only rules")
    raw_rows = _read_registered(root, entry.observations).splitlines()
    if (
        sum(len(row) + 1 for row in raw_rows) + len(json.dumps(alert)) + len(json.dumps(fault_plan))
        > max_snapshot_bytes
    ):
        raise ValueError("snapshot exceeds byte bound")
    observations: list[SourceObservation] = []
    identities: set[tuple[str, str]] = set()
    for number, row in enumerate(raw_rows, 1):
        if not row:
            continue
        if len(row) > max_record_bytes:
            raise ValueError(f"observation {number} exceeds byte bound")
        try:
            observation = SourceObservation.model_validate_json(row)
        except ValueError as exc:
            raise ValueError(f"invalid observation {number}") from exc
        identity = (observation.source, observation.source_record_id)
        if identity in identities:
            raise ValueError(f"duplicate source observation: {identity}")
        identities.add(identity)
        observations.append(observation)
    if alert.get("case_id") != case_id:
        raise ValueError("alert case ID does not match manifest")
    cutoff = _utc(alert.get("cutoff"), "alert cutoff")
    window = alert.get("time_window")
    if not isinstance(window, dict):
        raise ValueError("alert time_window is required")
    start = _utc(window.get("start"), "alert time_window.start")
    if start >= cutoff:
        raise ValueError("alert window must precede cutoff")
    digest = dataset_digest(alert, observations, fault_plan)
    if entry.dataset_digest and entry.dataset_digest != digest:
        raise ValueError("manifest dataset digest mismatch")
    if expected_digest and expected_digest != digest:
        raise ValueError("expected dataset digest mismatch")
    # JSON round-tripping breaks aliases to both source file data and returned page data.
    frozen_alert = MappingProxyType(json.loads(json.dumps(alert)))
    frozen_faults = MappingProxyType(json.loads(json.dumps(fault_plan)))
    return CaseSnapshot(
        case_id, frozen_alert, tuple(observations), frozen_faults, digest, cutoff, start
    )
