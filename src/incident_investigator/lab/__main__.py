"""Command line entrypoint for serving, loading, faulting, and freezing lab telemetry."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import uvicorn

from incident_investigator.lab.app import JsonlTelemetrySink, create_app
from incident_investigator.lab.load import generate_load
from incident_investigator.telemetry.models import SourceObservation, dataset_digest


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include UTC offset")
    return parsed.astimezone(UTC)


def snapshot(args: argparse.Namespace) -> None:
    cutoff = args.cutoff
    observations = [
        SourceObservation.model_validate_json(line)
        for line in Path(args.telemetry).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    selected = [item for item in observations if item.available_at <= cutoff]
    if not selected:
        print("warning: snapshot contains no observations at or before the cutoff", file=sys.stderr)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    alert = {
        "case_id": args.case,
        "affected_service": args.service,
        "observed_symptoms": args.symptoms,
        "time_window": {"start": args.start.isoformat(), "end": cutoff.isoformat()},
        "cutoff": cutoff.isoformat(),
    }
    fault_plan = {"rules": []}
    (out / "alert.json").write_text(
        json.dumps(alert, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out / "observations.jsonl").write_text(
        "".join(item.model_dump_json() + "\n" for item in selected), encoding="utf-8"
    )
    (out / "fault-plan.json").write_text(json.dumps(fault_plan, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "cases": {
            args.case: {
                "alert": "alert.json",
                "observations": "observations.jsonl",
                "fault_plan": "fault-plan.json",
                "dataset_digest": dataset_digest(alert, selected, fault_plan),
            }
        }
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="incident-lab")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--telemetry", type=Path, default=Path("telemetry.jsonl"))
    load = sub.add_parser("load")
    load.add_argument("--target", default="http://127.0.0.1:8000")
    load.add_argument("--rps", type=float, required=True)
    load.add_argument("--duration", type=float, required=True)
    load.add_argument("--fault")
    fault = sub.add_parser("fault")
    fault.add_argument("action", choices=["set", "clear"])
    fault.add_argument("family", nargs="?")
    fault.add_argument("--target", default="http://127.0.0.1:8000")
    freeze = sub.add_parser("snapshot")
    freeze.add_argument("--case", required=True)
    freeze.add_argument("--cutoff", type=_utc, required=True)
    freeze.add_argument("--start", type=_utc, default=datetime(2026, 1, 1, tzinfo=UTC))
    freeze.add_argument("--service", choices=["checkout", "inventory", "database"], required=True)
    freeze.add_argument("--symptoms", nargs="+", required=True)
    freeze.add_argument("--out", type=Path, required=True)
    freeze.add_argument("--telemetry", type=Path, default=Path("telemetry.jsonl"))
    args = parser.parse_args()
    if args.command == "serve":
        uvicorn.run(
            create_app(telemetry_sink=JsonlTelemetrySink(args.telemetry)),
            host=args.host,
            port=args.port,
        )
    elif args.command == "load":
        print(
            json.dumps(asyncio.run(generate_load(args.target, args.rps, args.duration, args.fault)))
        )
    elif args.command == "fault":
        if args.action == "set" and not args.family:
            parser.error("fault set requires a family")
        response = httpx.request(
            "POST" if args.action == "set" else "DELETE",
            f"{args.target}/admin/faults",
            json={"family": args.family} if args.action == "set" else None,
        )
        response.raise_for_status()
        print(response.text)
    else:
        snapshot(args)


if __name__ == "__main__":
    main()
