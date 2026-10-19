"""Regenerate the committed dev-00 fixture from the local lab deterministically."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from incident_investigator.lab.app import MemoryTelemetrySink, create_app
from incident_investigator.telemetry.models import SourceObservation, dataset_digest

ROOT = Path(__file__).parent
START = datetime(2026, 1, 1, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = START

    def __call__(self) -> datetime:
        return self.value


def observation(source: str, record_id: str, event_time: datetime, data: dict) -> SourceObservation:
    return SourceObservation(
        source=source,
        source_record_id=record_id,
        event_time=event_time,
        available_at=event_time,
        data=data,
    )


def metric(record_id: str, service: str, name: str, value: float, minute: int) -> SourceObservation:
    end = START + timedelta(minutes=minute + 1)
    unit = "ms" if "latency" in name else "connections" if name == "pool_in_use" else "requests"
    aggregation = "p95" if "latency" in name else "gauge" if name == "pool_in_use" else "count"
    return observation(
        "metrics",
        record_id,
        end,
        {
            "service": service,
            "name": name,
            "value": value,
            "unit": unit,
            "aggregation": aggregation,
            "window_start": end - timedelta(minutes=1),
            "window_end": end,
            "version": "v1",
            "operation": "reserve",
            "sample_count": 60,
            "attributes": {},
        },
    )


def main() -> None:
    clock = Clock()
    sink = MemoryTelemetrySink()
    with TestClient(create_app(clock, seed=11, telemetry_sink=sink)) as client:
        for minute in range(10):
            clock.value = START + timedelta(minutes=minute)
            if minute == 3:
                sink.emit(
                    observation(
                        "changes",
                        "dev-00-checkout-v1.1",
                        START + timedelta(minutes=3),
                        {
                            "service": "checkout",
                            "change_kind": "deployment",
                            "version": "v1.1",
                            "description": "checkout deployment: v1.0 -> v1.1",
                            "changes": [{"key": "version", "before": "v1.0", "after": "v1.1"}],
                            "attributes": {"rollout": "complete"},
                        },
                    )
                )
            if minute == 5:
                client.post("/admin/faults", json={"family": "bad_configuration"})
            for second in range(60):
                clock.value = START + timedelta(minutes=minute, seconds=second)
                client.post("/checkout")
    retained = [
        row
        for row in sink.observations
        if row.source in {"dependencies", "changes"}
        or (row.source == "logs" and row.data["severity"] == "ERROR" and row.event_time.second == 0)
        or (
            row.source == "logs"
            and row.data["severity"] == "INFO"
            and row.event_time.minute in {1, 4}
        )
        and row.event_time.second == 0
    ]
    summary: list[SourceObservation] = []
    sequence = 0
    for minute in range(10):
        failing = minute >= 5
        values = {
            "checkout": [
                ("request_count", 60),
                ("error_count", 60 if failing else 0),
                ("request_latency", 80 if failing else 5),
            ],
            "inventory": [
                ("request_count", 60),
                ("error_count", 60 if failing else 0),
                ("request_latency", 55 if failing else 3),
                ("db_query_latency", 55 if failing else 2),
                ("pool_in_use", 1 if failing else 2),
            ],
            "database": [("request_count", 60), ("error_count", 0), ("db_query_latency", 2)],
        }
        for service, measures in values.items():
            for name, value in measures:
                sequence += 1
                summary.append(
                    metric(f"dev-00-metric-{sequence:03d}", service, name, value, minute)
                )
    history_time = START - timedelta(days=30)
    histories = [
        (
            "history-001",
            ["checkout"],
            "Checkout retry storm",
            "Errors followed a retry rollout.",
            "retry policy",
            "rollback retry policy",
        ),
        (
            "history-002",
            ["database", "inventory"],
            "Inventory lock contention",
            "Reservations slowed during a bulk update.",
            "long transaction",
            "reschedule bulk update",
        ),
        (
            "history-003",
            ["checkout", "inventory"],
            "Inventory query deadline",
            "Checkout errors followed a reduced query deadline.",
            "deadline too low",
            "restore deadline",
        ),
    ]
    for record_id, services, title, summary_text, root_cause, resolution in histories:
        retained.append(
            observation(
                "history",
                record_id,
                history_time,
                {
                    "incident_id": record_id,
                    "services": services,
                    "title": title,
                    "summary": summary_text,
                    "symptoms": ["latency", "errors"],
                    "root_cause": root_cause,
                    "resolution": resolution,
                },
            )
        )
    rows = retained + summary
    alert = {
        "case_id": "dev-00",
        "affected_service": "checkout",
        "observed_symptoms": ["checkout latency and error rate up"],
        "time_window": {
            "start": (START + timedelta(minutes=4)).isoformat(),
            "end": (START + timedelta(minutes=10)).isoformat(),
        },
        "cutoff": (START + timedelta(minutes=10)).isoformat(),
    }
    fault_plan = {"rules": []}
    (ROOT / "alert.json").write_text(json.dumps(alert, indent=2, sort_keys=True) + "\n")
    (ROOT / "fault-plan.json").write_text(json.dumps(fault_plan) + "\n")
    (ROOT / "observations.jsonl").write_text("".join(row.model_dump_json() + "\n" for row in rows))
    manifest = {
        "cases": {
            "dev-00": {
                "alert": "alert.json",
                "observations": "observations.jsonl",
                "fault_plan": "fault-plan.json",
                "dataset_digest": dataset_digest(alert, rows, fault_plan),
            }
        }
    }
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
