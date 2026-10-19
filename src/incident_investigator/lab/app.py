"""Small deterministic FastAPI incident laboratory."""

from __future__ import annotations

import random
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from incident_investigator.telemetry.models import SourceObservation

FAULTS = (
    "bad_configuration",
    "deployment_regression",
    "database_contention",
    "resource_exhaustion",
    "traffic_overload",
)
FaultName = str


class TelemetrySink(Protocol):
    def emit(self, observation: SourceObservation) -> None: ...


class JsonlTelemetrySink:
    """Append-only JSONL sink suitable for later snapshotting."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, observation: SourceObservation) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(observation.model_dump_json() + "\n")


class MemoryTelemetrySink:
    def __init__(self) -> None:
        self.observations: list[SourceObservation] = []

    def emit(self, observation: SourceObservation) -> None:
        self.observations.append(observation)


class FaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    family: FaultName


def create_app(
    clock: Callable[[], datetime] | None = None,
    seed: int = 1,
    telemetry_sink: TelemetrySink | None = None,
) -> FastAPI:
    """Create an in-process three-service lab with injectable time and telemetry."""
    current_time = clock or (lambda: datetime.now(UTC))
    sink = telemetry_sink or MemoryTelemetrySink()
    rng = random.Random(seed)
    app = FastAPI(title="Incident Investigator Lab")
    app.state.faults: set[str] = set()
    app.state.sequence = 0
    app.state.sink = sink
    database = sqlite3.connect(":memory:", check_same_thread=False)
    database.execute("CREATE TABLE inventory (sku TEXT PRIMARY KEY, available INTEGER NOT NULL)")
    database.execute("INSERT INTO inventory VALUES ('widget', 100)")
    database.commit()
    counters: dict[str, list[float]] = defaultdict(list)

    def now() -> datetime:
        value = current_time()
        if value.tzinfo is None:
            raise ValueError("lab clock must return an aware datetime")
        return value.astimezone(UTC)

    def emit(source: str, data: dict[str, Any], *, event_time: datetime | None = None) -> None:
        app.state.sequence += 1
        point = event_time or now()
        sink.emit(
            SourceObservation(
                source=source,
                source_record_id=f"lab-{app.state.sequence:06d}",
                event_time=point,
                available_at=point,
                data=data,
            )
        )

    def log(
        service: str, severity: str, body: str, request_id: str | None, **attributes: Any
    ) -> None:
        emit(
            "logs",
            {
                "service": service,
                "severity": severity,
                "body": body,
                "version": "v2"
                if "deployment_regression" in app.state.faults and service == "inventory"
                else "v1",
                "operation": "reserve" if service != "database" else "query",
                "request_id": request_id,
                "trace_id": request_id,
                "transaction_id": None,
                "attributes": attributes,
            },
        )

    def metric(
        service: str, name: str, value: float, unit: str, aggregation: str, **attributes: Any
    ) -> None:
        point = now()
        emit(
            "metrics",
            {
                "service": service,
                "name": name,
                "value": value,
                "unit": unit,
                "aggregation": aggregation,
                "window_start": (point - timedelta(seconds=10)).isoformat(),
                "window_end": point.isoformat(),
                "version": "v2"
                if "deployment_regression" in app.state.faults and service == "inventory"
                else "v1",
                "operation": "reserve",
                "sample_count": 1,
                "attributes": attributes,
            },
            event_time=point,
        )

    def metrics(service: str, latency_ms: float, error: bool, db_ms: float = 1.0) -> None:
        counters[service].append(latency_ms)
        metric(service, "request_count", 1, "requests", "count")
        metric(service, "error_count", float(error), "errors", "count")
        metric(service, "request_latency", latency_ms, "ms", "p95")
        if service == "inventory":
            metric(service, "db_query_latency", db_ms, "ms", "p95")
            pool = (
                1.0
                if "bad_configuration" in app.state.faults
                else (10.0 if "resource_exhaustion" in app.state.faults else 2.0)
            )
            metric(service, "pool_in_use", pool, "connections", "gauge", pool_size=pool)
            # Worker saturation is visible as a gauge, not only in WARN log attributes.
            active = 64.0 if "traffic_overload" in app.state.faults else 1.0
            metric(service, "active_requests", active, "requests", "gauge", worker_limit=64)

    def inventory_reserve(sku: str, request_id: str) -> tuple[bool, int, float]:
        fault = app.state.faults
        if "resource_exhaustion" in fault:
            log(
                "inventory",
                "ERROR",
                "connection pool exhausted",
                request_id,
                pool_in_use=10,
                pool_size=10,
            )
            metrics("inventory", 120.0, True, 100.0)
            return False, 503, 120.0
        if "traffic_overload" in fault:
            log("inventory", "WARN", "worker concurrency saturated", request_id, active_requests=64)
            metrics("inventory", 180.0, True, 2.0)
            return False, 503, 180.0
        db_ms = 2.0
        if "database_contention" in fault:
            db_ms = 250.0
            log(
                "database",
                "WARN",
                "lock wait while reserving inventory",
                request_id,
                lock_wait_ms=db_ms,
            )
        if "deployment_regression" in fault:
            db_ms += 100.0
            log(
                "inventory",
                "WARN",
                "v2 reservation query expanded scan",
                request_id,
                query_plan="full_scan",
            )
        if "bad_configuration" in fault:
            log(
                "inventory",
                "ERROR",
                "database query timed out at configured deadline",
                request_id,
                db_timeout_ms=50,
            )
            metrics("inventory", 55.0, True, 55.0)
            return False, 504, 55.0
        if db_ms > 100:
            time.sleep(0.001)
        row = database.execute("SELECT available FROM inventory WHERE sku = ?", (sku,)).fetchone()
        if row is None or row[0] < 1:
            log("inventory", "ERROR", "sku unavailable", request_id)
            metrics("inventory", db_ms + 2, True, db_ms)
            return False, 409, db_ms + 2
        database.execute("UPDATE inventory SET available = available - 1 WHERE sku = ?", (sku,))
        database.commit()
        log(
            "database",
            "INFO",
            "inventory reservation query completed",
            request_id,
            query_latency_ms=db_ms,
        )
        log("inventory", "INFO", "inventory reserved", request_id)
        latency = db_ms + rng.random()
        metrics("inventory", latency, False, db_ms)
        return True, 200, latency

    @app.on_event("startup")
    def startup() -> None:
        for service, targets in (
            ("checkout", ["inventory"]),
            ("inventory", ["database"]),
            ("database", []),
        ):
            emit("dependencies", {"service": service, "targets": targets})

    @app.post("/checkout")
    def checkout(sku: str = "widget") -> dict[str, Any]:
        request_id = f"req-{app.state.sequence + 1:06d}"
        log("checkout", "INFO", "checkout request received", request_id, sku=sku)
        ok, status, downstream_ms = inventory_reserve(sku, request_id)
        # Checkout latency includes the downstream reservation it waited for.
        latency = downstream_ms + 3.0
        log(
            "checkout",
            "INFO" if ok else "ERROR",
            "checkout completed" if ok else "checkout failed",
            request_id,
            status=status,
        )
        metrics("checkout", latency, not ok)
        if not ok:
            raise HTTPException(status_code=status, detail="reservation failed")
        return {"status": "reserved", "sku": sku}

    @app.get("/inventory/{sku}")
    def inventory(sku: str) -> dict[str, Any]:
        row = database.execute("SELECT available FROM inventory WHERE sku = ?", (sku,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="unknown sku")
        return {"sku": sku, "available": row[0]}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/admin/faults")
    def set_fault(request: FaultRequest) -> dict[str, list[str]]:
        if request.family not in FAULTS:
            raise HTTPException(status_code=422, detail="unknown fault family")
        app.state.faults.add(request.family)
        if request.family == "deployment_regression":
            emit(
                "changes",
                {
                    "service": "inventory",
                    "change_kind": "deployment",
                    "version": "v2",
                    "description": "inventory v2 deployed",
                    "changes": [{"key": "version", "before": "v1", "after": "v2"}],
                    "attributes": {},
                },
            )
        if request.family == "bad_configuration":
            emit(
                "changes",
                {
                    "service": "inventory",
                    "change_kind": "configuration",
                    "version": None,
                    "description": "database deadline reduced",
                    "changes": [{"key": "db_timeout_ms", "before": 500, "after": 50}],
                    "attributes": {},
                },
            )
        return {"faults": sorted(app.state.faults)}

    @app.delete("/admin/faults")
    def clear_faults() -> dict[str, list[str]]:
        app.state.faults.clear()
        return {"faults": []}

    return app
