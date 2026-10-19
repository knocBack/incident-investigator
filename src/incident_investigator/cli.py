"""Minimal local investigation, inspection and live-lab snapshot demo CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from incident_investigator.contracts import PROFILES, Limits
from incident_investigator.graph_config import DEFAULT_GRAPH, add_graph_argument
from incident_investigator.lab.__main__ import snapshot
from incident_investigator.lab.app import FAULTS, JsonlTelemetrySink, create_app
from incident_investigator.models import CodexExecModel, OpenRouterModel, ScriptedModel
from incident_investigator.runtime import investigate
from incident_investigator.sources.loader import load_snapshot
from incident_investigator.storage import repositories
from incident_investigator.workspace import home


def profile(args) -> Limits:
    """Live providers get the demo-live profile; its per-call timeout also bounds adapters."""
    selected = getattr(args, "limits_profile", None)
    return PROFILES[selected or ("demo-live" if args.model in {"openrouter", "codex"} else "demo")]


def model(args):
    injected = getattr(args, "_model_adapter", None)
    if injected is not None:
        return injected
    if args.model == "openrouter":
        adapter = OpenRouterModel(timeout_seconds=profile(args).call_seconds)
        if getattr(args, "model_id", None):
            adapter._models = dict.fromkeys(adapter._models, args.model_id)
        return adapter
    if args.model == "codex":
        return CodexExecModel(timeout_seconds=profile(args).call_seconds)
    actions = json.loads(args.script.read_text()) if args.script else []
    return ScriptedModel(actions)


async def demo(args) -> Path:
    """Exercise the real FastAPI routes in-process; investigators see only a snapshot."""
    with tempfile.TemporaryDirectory(prefix="ii-demo-") as directory:
        root = Path(directory)
        telemetry = root / "telemetry.jsonl"
        # A simulated clock spaces baseline, fault and failing load seconds apart (ending
        # before now), so before/after comparisons and change times are meaningful.
        moment = [datetime.now(UTC) - timedelta(seconds=40)]
        app = create_app(clock=lambda: moment[0], telemetry_sink=JsonlTelemetrySink(telemetry))
        # M1 metrics cover ten-second windows; include the whole baseline window.
        start = moment[0] - timedelta(seconds=11)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://lab"
            ) as client:
                for _ in range(3):
                    moment[0] += timedelta(seconds=2)
                    await client.post("/checkout")
                moment[0] += timedelta(seconds=5)
                response = await client.post("/admin/faults", json={"family": args.fault})
                response.raise_for_status()
                for _ in range(5):
                    moment[0] += timedelta(seconds=2)
                    await client.post("/checkout")
            cutoff = moment[0]
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
            directory = await investigate(
                load_snapshot("lab-demo", source_root),
                getattr(args, "_model_adapter", None) or model(args),
                args.out,
                limits=profile(args),
                graph=getattr(args, "graph", DEFAULT_GRAPH),
            )
            # Evaluator metadata stays outside the temporary agent-visible source root.
            (directory / "expectation.json").write_text(
                json.dumps({"family": args.fault, "version": "lab.v1"}) + "\n"
            )
            return directory


async def run(args) -> int:
    if getattr(args, "resume", None):
        if args.case or args.source:
            raise ValueError("--resume uses the saved source; omit --case and --source")
        runs = [await investigate(None, model(args), resume=args.resume)]
    elif args.command == "demo":
        runs = [await demo(args)]
    else:
        semaphore = asyncio.Semaphore(4)
        roots = args.source or Path("fixtures/dev-00")
        if not args.case:
            raise ValueError("--case or --resume is required")
        snapshots = [load_snapshot(case, roots) for case in args.case]
        runs = await asyncio.gather(
            *[
                investigate(
                    case,
                    model(args),
                    args.out,
                    semaphore=semaphore,
                    limits=profile(args),
                    graph=getattr(args, "graph", DEFAULT_GRAPH),
                )
                for case in snapshots
            ]
        )
    failed = False
    for directory in runs:
        print(directory / "report.md")
        result = json.loads((directory / "result.json").read_text())
        failed |= result["status"] in {"failed", "resource_exhausted"}
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="incident-investigator")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("run_dir", type=Path)
    for name in ("investigate", "demo"):
        command = sub.add_parser(name)
        add_graph_argument(command)
        command.add_argument(
            "--model", choices=["scripted", "codex", "openrouter"], default="scripted"
        )
        command.add_argument(
            "--script",
            type=Path,
            help="explicit ScriptedModel action JSON; no runtime default script",
        )
        command.add_argument("--out", type=Path, default=home() / "runs")
        command.add_argument("--trace", choices=["phoenix"])
        command.add_argument("--limits-profile", choices=PROFILES)
        command.add_argument("--model-id", help="OpenRouter model id for all roles")
        if name == "investigate":
            command.add_argument("--case", action="append")
            command.add_argument(
                "--resume",
                type=Path,
                help="continue a saved run with its original source and limits",
            )
            command.add_argument(
                "--source", type=Path, help="manifest root (default fixtures/dev-00)"
            )
        else:
            command.add_argument(
                "--fault",
                default="bad_configuration",
                choices=FAULTS,
            )
    from incident_investigator.grading.suite import add_commands

    add_commands(sub)
    command = sub.add_parser(
        "import", help="index existing artifacts in place; never modify sources"
    )
    command.add_argument("directories", nargs="+", type=Path)
    command = sub.add_parser("console", help="serve the local artifact console")
    command.add_argument("--port", type=int, default=8765)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command in {"demo", "investigate"} and args.model == "scripted" and not args.script:
        parser.error("the scripted model requires --script; supply it or select another --model")
    if getattr(args, "trace", None) == "phoenix":
        os.environ["II_PHOENIX"] = "1"
    if args.command == "import":
        repo = repositories()
        repo.runs.import_roots(args.directories)
        print(
            f"Indexed {len(repo.runs.runs())} runs and {len(repo.runs.suites())} suites in {home()}"
        )
        return
    if args.command == "console":
        import uvicorn

        from incident_investigator.console import create_console

        uvicorn.run(create_console(), host="127.0.0.1", port=args.port)
        return
    if args.command in {"eval", "eval-suite"}:
        from incident_investigator.grading.suite import execute

        asyncio.run(execute(args))
        return
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
