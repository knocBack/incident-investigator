"""Freeze repeatable lab observations for named development cases.

Case metadata maps switches to opaque IDs; no answer keys are loaded here.
"""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from incident_investigator.lab.__main__ import snapshot
from incident_investigator.lab.app import JsonlTelemetrySink, create_app
from incident_investigator.telemetry.models import SourceObservation, dataset_digest

SECOND_CASES = {
    "case-02": "caller_deadline",
    "case-04": "response_handling",
    "case-06": "lock_order",
    "case-08": "memory_growth",
    "case-10": "retry_amplification",
}
VARIANTS = {"reliability-01": "pool_leak", "reliability-02": "endpoint_address"}


async def freeze_case(case_id: str, root: Path):
    fault = {**SECOND_CASES, **VARIANTS}[case_id]
    moment = [datetime(2026, 1, 1, 13, 55, tzinfo=UTC)]
    telemetry = root / "telemetry.jsonl"
    app = create_app(
        clock=lambda: moment[0], telemetry_sink=JsonlTelemetrySink(telemetry), scenario=fault
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://lab") as client,
    ):
        for _ in range(3):
            await client.post("/checkout")
            moment[0] += timedelta(minutes=1)
        moment[0] = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
        (await client.post("/admin/faults", json={"family": fault})).raise_for_status()
        for minute in range(3, 10):
            moment[0] = datetime(2026, 1, 1, 14, minute, tzinfo=UTC)
            await client.post("/checkout")
    cutoff = datetime(2026, 1, 1, 14, 10, tzinfo=UTC)
    snapshot(
        argparse.Namespace(
            cutoff=cutoff,
            telemetry=telemetry,
            out=root,
            case=case_id,
            service="checkout",
            symptoms=["Checkout latency or errors increased after 14:02"],
            start=datetime(2026, 1, 1, 13, 50, tzinfo=UTC),
        )
    )
    observations = [
        SourceObservation.model_validate_json(line)
        for line in (root / "observations.jsonl").read_text().splitlines()
    ]
    # A prior incident is a tempting analogy, not evidence of today's cause.
    observations.append(
        SourceObservation(
            source="history",
            source_record_id="prior-01",
            event_time=datetime(2025, 12, 1, tzinfo=UTC),
            available_at=datetime(2025, 12, 1, tzinfo=UTC),
            data=dict(
                incident_id="prior-01",
                services=["checkout", "database", "inventory"],
                title="Checkout timeout incident",
                summary="Earlier requests waited on database locks.",
                symptoms=["Checkout latency and errors"],
                root_cause="database lock contention",
                resolution="Operator completed the blocking maintenance transaction.",
            ),
        )
    )
    rules = []
    if case_id == "case-08":
        rules = [
            dict(
                rule_id="source-08",
                tool="metrics",
                service="inventory",
                window_start="2026-01-01T14:04:00Z",
                window_end="2026-01-01T14:07:00Z",
                steps=[{"outcome": "partial"}] * 300,
            )
        ]
    if case_id == "reliability-01":
        rules = [
            dict(
                rule_id="source-01",
                tool="logs",
                service="inventory",
                steps=[{"outcome": "timeout"}],
            )
        ]
    elif case_id == "reliability-02":
        # Remove decisive records from every retrieval path, including aggregation.
        observations = [
            r
            for r in observations
            if not (r.source == "logs" and r.data.get("service") == "checkout")
        ]
        for row in observations:
            if row.source == "changes":
                row.data["changes"] = []
                row.data["attributes"] = {"configuration_diff": "unavailable"}
        rules = [
            dict(
                rule_id="source-02",
                tool="logs",
                service="checkout",
                steps=[{"outcome": "error"}] * 300,
            )
        ]
    plan = {"rules": rules}
    alert = json.loads((root / "alert.json").read_text())
    (root / "observations.jsonl").write_text(
        "".join(r.model_dump_json() + "\n" for r in observations)
    )
    (root / "fault-plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "cases": {
                    case_id: dict(
                        alert="alert.json",
                        observations="observations.jsonl",
                        fault_plan="fault-plan.json",
                        dataset_digest=dataset_digest(alert, observations, plan),
                    )
                }
            },
            indent=2,
        )
        + "\n"
    )
    telemetry.unlink()
