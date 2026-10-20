"""Validated local CLI launches; no shell, user supplied environment, or credentials."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import sys
from pathlib import Path
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from incident_investigator.contracts import PROFILES
from incident_investigator.graph_config import DEFAULT_GRAPH
from incident_investigator.storage import FileEvalCatalog, Repositories
from incident_investigator.workspace import home


def discover_faults(expected: Path | None = None) -> list[dict]:
    return FileEvalCatalog(expected).faults()


def graph_variants() -> list[str]:
    # Inspect the actual CLI choices so a graph extension can become available without UI edits.
    from incident_investigator.cli import build_parser

    parser = build_parser()
    for action in parser._actions:
        for sub in (
            getattr(action, "choices", {}).values() if isinstance(action.choices, dict) else ()
        ):
            if sub.prog.endswith(" demo"):
                for option in sub._actions:
                    if option.dest == "graph" and option.choices:
                        return list(option.choices)
    return [DEFAULT_GRAPH]


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: Literal["demo", "eval-suite", "eval"] = "eval-suite"
    faults: list[str] = Field(default_factory=list, min_length=0, max_length=50)
    provider: Literal["scripted", "openrouter"] = "scripted"
    model_id: str = Field(default="", max_length=200)
    graph: str = DEFAULT_GRAPH
    system: Literal["multi", "single", "both"] = "multi"
    case_set: Literal["primary", "variants", "all"] = "primary"
    limits_profile: str = "demo"
    call_seconds: float | None = Field(default=None, gt=0, le=600, allow_inf_nan=False)
    wall_seconds: float | None = Field(default=None, gt=0, le=3600, allow_inf_nan=False)
    max_input_tokens: int = Field(default=64000, ge=1, le=2_000_000)
    repeats: int = Field(default=1, ge=1, le=20)
    concurrency: int = Field(default=2, ge=1, le=8)
    budget_usd: str = "0"
    run_budget_usd: str | None = None
    confirm_paid: bool = False
    run: str | None = None
    trace: bool = False

    @field_validator("budget_usd", "run_budget_usd")
    @classmethod
    def budget(cls, value):
        if value is None:
            return None
        from decimal import InvalidOperation

        from incident_investigator.grading.budget import amount

        try:
            parsed = amount(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("invalid budget") from exc
        if not parsed.is_finite() or parsed < 0 or parsed > 100:
            raise ValueError("budget must be between 0 and 100 USD")
        return str(parsed)


def build_command(
    request: RunRequest,
    repo: Repositories,
    job_id: str,
    *,
    root: Path | None = None,
    validate_launch: bool = True,
) -> tuple[list[str], Path]:
    root = root or home()
    argv = [sys.executable, "-u", "-m", "incident_investigator.cli", request.command]
    if request.limits_profile not in PROFILES or request.graph not in graph_variants():
        raise ValueError("unknown limits profile or unavailable graph")
    if request.command == "eval":
        run = repo.runs.get(request.run)
        if not run:
            raise ValueError("select an indexed run to grade")
        output = root / "suites" / f"eval-{job_id}"
        argv += [str(root / "jobs" / job_id / "input"), "--out", str(output)]
        if request.faults:
            valid = {f["id"] for f in repo.catalog.faults() if f["evaluable"]}
            if len(request.faults) != 1 or request.faults[0] not in valid:
                raise ValueError("grading requires one known expectation")
            argv += ["--fault", request.faults[0]]
        return argv, output
    valid = {
        f["id"]
        for f in repo.catalog.faults()
        if f["injectable"]
        and f["evaluable"]
        and (request.case_set == "all" or f.get("case_set", "primary") == request.case_set)
    }
    if (
        (not request.faults and request.command == "demo")
        or set(request.faults) - valid
        or len(set(request.faults)) != len(request.faults)
    ):
        raise ValueError("select distinct injectable cases within the selected set")
    if request.provider == "openrouter":
        if validate_launch and not request.confirm_paid:
            raise ValueError("explicit paid-run confirmation required")
        if validate_launch and not os.environ.get("OPENROUTER_API_KEY"):
            raise ValueError("OPENROUTER_API_KEY is not set in the server environment")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.:+-]+", request.model_id):
            raise ValueError("use a complete organization/model id")
        if request.command != "eval-suite":
            raise ValueError("use eval-suite for OpenRouter so the budget guard applies")
    if request.command == "demo":
        if len(request.faults) != 1 or request.repeats != 1:
            raise ValueError("demo requires one fault and one repeat; use eval-suite for batches")
        output = root / "runs"
        argv += ["--fault", request.faults[0]]
    else:
        output = root / "suites" / job_id
        argv += [
            "--set",
            request.case_set,
            "--faults",
            ",".join(request.faults) if request.faults else "all",
            "--models",
            request.model_id if request.provider == "openrouter" else "scripted",
            "--repeats",
            str(request.repeats),
            "--concurrency",
            str(request.concurrency),
            "--budget-usd",
            request.budget_usd,
        ]
    argv += [
        "--model",
        request.provider,
        "--out",
        str(output),
        "--limits-profile",
        request.limits_profile,
    ]
    if request.command == "demo" and request.system == "both":
        raise ValueError("use eval-suite to compare both systems")
    argv += ["--graph", request.graph, "--system", request.system]
    for name in ("call_seconds", "wall_seconds"):
        if getattr(request, name) is not None:
            argv += ["--" + name.replace("_", "-"), str(getattr(request, name))]
    if request.command == "eval-suite" and request.run_budget_usd is not None:
        argv += ["--run-budget-usd", request.run_budget_usd]
    if request.command == "eval-suite":
        argv += ["--max-input-tokens", str(request.max_input_tokens)]
    if request.trace:
        argv += ["--trace", "phoenix"]
    if request.provider == "scripted":
        replay = Path(__file__).with_name("scripted-paired.json")
        if not replay.is_file():
            replay = Path(__file__).resolve().parents[2] / "tests" / "scripted-paired.json"
        if not replay.is_file():
            raise ValueError("the scripted replay is missing; reinstall the package")
        argv += ["--script", str(replay)]
    return argv, output


def display_command(argv: list[str]) -> str:
    return shlex.join(["incident-investigator", *argv[4:]])


class Jobs:
    """Local process adapter. UI only sees persisted job records and artifact keys."""

    def __init__(self, repo: Repositories, root: Path | None = None):
        self.repo, self.root = repo, root or home()
        self.tasks: dict[str, asyncio.Task] = {}
        self._refreshed = float("-inf")

    def preview(self, request: RunRequest, ident: str):
        return build_command(request, self.repo, ident, root=self.root, validate_launch=False)

    def start(self, request: RunRequest, ident: str):
        if any(not task.done() for task in self.tasks.values()):
            raise ValueError("a console job is already active; wait for it to finish")
        argv, output = build_command(request, self.repo, ident, root=self.root)
        if request.command == "eval":
            run = self.repo.runs.get(request.run)
            self.repo.artifacts.materialize(run["prefix"], self.root / "jobs" / ident / "input")
        self.repo.artifacts.save_json(
            f"jobs/{ident}/job.json",
            {
                "id": ident,
                "status": "running",
                "command": display_command(argv),
            },
        )
        self.tasks[ident] = asyncio.create_task(self.execute(ident, argv, output))

    def get(self, ident):
        metadata = self.repo.artifacts.json(f"jobs/{ident}/job.json")
        if metadata:
            now = monotonic()
            if now - self._refreshed >= 2:
                self.repo.runs.refresh()
                self._refreshed = now
            metadata["output"] = (
                self.repo.artifacts.read(f"jobs/{ident}/output.log") or b""
            ).decode(errors="replace")
            metadata["runs"] = [r for r in self.repo.runs.runs() if r.get("launch_id") == ident]
            from incident_investigator.telemetry.tracing import local_url

            for run in metadata["runs"]:
                try:
                    run["phoenix_url"] = (
                        local_url(run["phoenix_url"]) if run.get("phoenix_url") else None
                    )
                except (ValueError, TypeError):
                    run["phoenix_url"] = None
            metadata["suites"] = [
                s for s in self.repo.runs.suites() if s["batch"].get("launch_id") == ident
            ]
            if metadata["status"] == "running" and ident not in self.tasks:
                metadata["status"] = "interrupted"
        return metadata

    async def execute(self, ident: str, argv: list[str], output: Path):
        store = self.repo.artifacts
        metadata = store.json(f"jobs/{ident}/job.json")
        process = None
        captured = bytearray()
        try:
            env = {
                **os.environ,
                "II_HOME": str(self.root),
                "PYTHONUNBUFFERED": "1",
                "II_LAUNCH_ID": ident,
            }
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            async with asyncio.timeout(3600):
                while chunk := await process.stdout.read(8192):
                    if len(captured) < 2_000_000:
                        captured.extend(chunk[: 2_000_000 - len(captured)])
                        store.write(f"jobs/{ident}/output.log", captured)
                metadata["exit_code"] = await process.wait()
            metadata["status"] = "completed" if metadata["exit_code"] == 0 else "failed"
        except asyncio.CancelledError:
            metadata["status"] = "interrupted"
            raise
        except Exception as exc:
            metadata["status"] = "failed"
            metadata["error"] = type(exc).__name__
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            store.save_json(f"jobs/{ident}/job.json", metadata)
            store.ingest_tree(output, output.relative_to(self.root).as_posix())
            self.repo.runs.refresh()
            self._refreshed = monotonic()

    async def close(self):
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
