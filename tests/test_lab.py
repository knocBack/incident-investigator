from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from incident_investigator.lab.app import MemoryTelemetrySink, create_app
from incident_investigator.telemetry.models import SourceObservation, dataset_digest


@pytest.fixture
def lab() -> tuple[TestClient, MemoryTelemetrySink]:
    sink = MemoryTelemetrySink()
    client = TestClient(
        create_app(lambda: datetime(2026, 1, 1, tzinfo=UTC), seed=4, telemetry_sink=sink)
    )
    with client:
        yield client, sink


def bodies(sink: MemoryTelemetrySink) -> list[str]:
    return [row.data.get("body", "") for row in sink.observations if row.source == "logs"]


def test_baseline_has_no_fault_signature(lab: tuple[TestClient, MemoryTelemetrySink]) -> None:
    client, sink = lab
    assert client.post("/checkout").status_code == 200
    assert not any("timed out" in body or "exhausted" in body for body in bodies(sink))


@pytest.mark.parametrize(
    ("family", "signature", "status"),
    [
        ("deployment_regression", "expanded scan", 200),
        ("bad_configuration", "configured deadline", 504),
        ("database_contention", "lock wait", 200),
        ("resource_exhaustion", "pool exhausted", 503),
        ("traffic_overload", "concurrency saturated", 503),
    ],
)
def test_each_fault_has_a_distinct_telemetry_signature(
    lab: tuple[TestClient, MemoryTelemetrySink], family: str, signature: str, status: int
) -> None:
    client, sink = lab
    assert client.post("/admin/faults", json={"family": family}).status_code == 200
    assert client.post("/checkout").status_code == status
    assert any(signature in body for body in bodies(sink))


def test_dev_fixture_validates_and_digest_ignores_line_order() -> None:
    root = Path("fixtures/dev-00")
    alert = json.loads((root / "alert.json").read_text())
    fault_plan = json.loads((root / "fault-plan.json").read_text())
    rows = [
        SourceObservation.model_validate_json(line)
        for line in (root / "observations.jsonl").read_text().splitlines()
    ]
    manifest = json.loads((root / "manifest.json").read_text())
    assert dataset_digest(alert, rows, fault_plan) == manifest["cases"]["dev-00"]["dataset_digest"]
    assert dataset_digest(alert, list(reversed(rows)), fault_plan) == dataset_digest(
        alert, rows, fault_plan
    )
    assert alert["affected_service"] == "checkout"
    assert len([row for row in rows if row.source == "history"]) == 3
    assert len({row.event_time for row in rows if row.source == "metrics"}) == 10


def test_runtime_package_does_not_import_evaluator_files() -> None:
    source = "\n".join(path.read_text() for path in Path("src/incident_investigator").rglob("*.py"))
    assert "evals/" not in source
    assert "evals." not in source


def test_real_clock_metric_event_time_matches_window_end() -> None:
    sink = MemoryTelemetrySink()
    with TestClient(create_app(telemetry_sink=sink)) as client:
        assert client.post("/checkout").status_code == 200
    metrics = [row for row in sink.observations if row.source == "metrics"]
    assert metrics
    assert all(row.event_time == datetime.fromisoformat(row.data["window_end"]) for row in metrics)


def test_empty_snapshot_warns(tmp_path, capsys):
    from argparse import Namespace
    from datetime import timedelta

    from incident_investigator.lab.__main__ import snapshot

    telemetry = tmp_path / "telemetry.jsonl"
    telemetry.write_text("")
    cutoff = datetime.now(UTC)
    snapshot(
        Namespace(
            cutoff=cutoff,
            start=cutoff - timedelta(seconds=10),
            telemetry=telemetry,
            out=tmp_path / "case",
            case="empty",
            service="inventory",
            symptoms=["timeouts"],
        )
    )
    assert "snapshot contains no observations" in capsys.readouterr().err
    assert (tmp_path / "case" / "manifest.json").is_file()
