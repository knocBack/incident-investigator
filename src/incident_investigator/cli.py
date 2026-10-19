"""Minimal local investigation, inspection and live-lab snapshot demo CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from incident_investigator.lab.__main__ import snapshot
from incident_investigator.lab.app import JsonlTelemetrySink, create_app
from incident_investigator.models import CodexExecModel, OpenRouterModel, ScriptedModel
from incident_investigator.runtime import investigate
from incident_investigator.sources.loader import load_snapshot


def model(args):
    if args.model == "openrouter":
        return OpenRouterModel()
    if args.model == "codex":
        return CodexExecModel()
    actions = json.loads(args.script.read_text()) if args.script else []
    return ScriptedModel(actions)


async def demo(args) -> Path:
    """Exercise the real FastAPI routes in-process; investigators see only a snapshot."""
    with tempfile.TemporaryDirectory(prefix="ii-demo-") as directory:
        root = Path(directory)
        telemetry = root / "telemetry.jsonl"
        app = create_app(telemetry_sink=JsonlTelemetrySink(telemetry))
        # M1 metrics cover ten-second windows; include the whole baseline window.
        start = datetime.now(UTC) - timedelta(seconds=11)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://lab"
            ) as client:
                for _ in range(3):
                    await client.post("/checkout")
                response = await client.post("/admin/faults", json={"family": args.fault})
                response.raise_for_status()
                for _ in range(5):
                    await client.post("/checkout")
            cutoff = datetime.now(UTC)
            source_root = root / "case"
            snapshot(
                argparse.Namespace(
                    cutoff=cutoff,
                    telemetry=telemetry,
                    out=source_root,
                    case="lab-demo",
                    service="checkout",
                    symptoms=["checkout latency or errors increased during load"],
                    start=start,
                )
            )
            return await investigate(load_snapshot("lab-demo", source_root), model(args), args.out)


async def run(args) -> int:
    if args.command == "demo":
        runs = [await demo(args)]
    else:
        semaphore = asyncio.Semaphore(4)
        roots = args.source or Path("fixtures/dev-00")
        snapshots = [load_snapshot(case, roots) for case in args.case]
        runs = await asyncio.gather(
            *[investigate(case, model(args), args.out, semaphore=semaphore) for case in snapshots]
        )
    failed = False
    for directory in runs:
        print(directory / "report.md")
        result = json.loads((directory / "result.json").read_text())
        failed |= result["status"] in {"failed", "resource_exhausted"}
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="incident-investigator")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("run_dir", type=Path)
    for name in ("investigate", "demo"):
        command = sub.add_parser(name)
        command.add_argument(
            "--model", choices=["scripted", "codex", "openrouter"], default="scripted"
        )
        command.add_argument(
            "--script",
            type=Path,
            help="explicit ScriptedModel action JSON; no runtime default script",
        )
        command.add_argument("--out", type=Path, default=Path("runs"))
        if name == "investigate":
            command.add_argument("--case", action="append", required=True)
            command.add_argument(
                "--source", type=Path, help="manifest root (default fixtures/dev-00)"
            )
        else:
            command.add_argument(
                "--fault",
                default="bad_configuration",
                choices=[
                    "bad_configuration",
                    "deployment_regression",
                    "database_contention",
                    "resource_exhaustion",
                    "traffic_overload",
                ],
            )
    args = parser.parse_args()
    if args.command == "inspect":
        result = json.loads((args.run_dir / "result.json").read_text())
        print(
            json.dumps(
                {
                    key: result[key]
                    for key in (
                        "run_id",
                        "case_id",
                        "revision",
                        "status",
                        "completeness",
                        "stop_reason",
                        "usage",
                    )
                },
                indent=2,
            )
        )
        print((args.run_dir / "report.md").read_text())
    else:
        try:
            status = asyncio.run(run(args))
        except (ValueError, OSError) as exc:
            parser.exit(2, f"incident-investigator: {exc}\n")
        if status:
            raise SystemExit(status)


if __name__ == "__main__":
    main()
