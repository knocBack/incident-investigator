"""Validated local CLI launches; no shell, user supplied environment, or credentials."""

from __future__ import annotations

import asyncio
import math
import os
import re
import shlex
import sys
from contextlib import suppress
from pathlib import Path
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from incident_investigator.console_preflight import LaunchPreflight
from incident_investigator.console_views import (
    failure_explanation,
    failure_label,
    present_run,
    suite_title,
)
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
            raise ValueError(
                "Tick 'I explicitly authorize OpenRouter calls' in section 02 before launching (explicit confirmation required for all OpenRouter models)."
            )
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


TERMINATION_GRACE_SECONDS = 120


def job_plan(request: RunRequest, repo: Repositories) -> dict:
    cases = request.faults or [
        f["id"]
        for f in repo.catalog.faults()
        if f["injectable"]
        and f["evaluable"]
        and (request.case_set == "all" or f.get("case_set", "primary") == request.case_set)
    ]
    attempts = (
        len(cases) * request.repeats * (2 if request.system == "both" else 1)
        if request.command == "eval-suite"
        else 1
    )
    concurrency = min(request.concurrency, attempts) if request.command == "eval-suite" else 1
    wall = request.wall_seconds or PROFILES[request.limits_profile].wall_seconds
    duration = math.ceil(attempts / max(concurrency, 1)) * wall
    return {
        "attempts": attempts,
        "concurrency": concurrency,
        "wall_seconds": wall,
        "call_seconds": request.call_seconds or PROFILES[request.limits_profile].call_seconds,
        "timeout_seconds": math.ceil(duration + max(60, duration * 0.1)),
        "termination_grace_seconds": TERMINATION_GRACE_SECONDS,
    }


class Jobs:
    """Local process adapter. UI only sees persisted job records and artifact keys."""

    def __init__(self, repo: Repositories, root: Path | None = None):
        self.repo, self.root = repo, root or home()
        self.tasks: dict[str, asyncio.Task] = {}
        self._refreshed = float("-inf")
        self.preflight = LaunchPreflight()

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
                "plan": job_plan(request, self.repo),
                "provider": request.provider,
                "limits_profile": request.limits_profile,
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
                s
                for s in self.repo.runs.suites()
                if s["batch"].get("launch_id") == ident or s["name"] in {ident, f"eval-{ident}"}
            ]
            for run in metadata["runs"]:
                run["report_path"] = run["location"].rstrip("/") + "/report.md"
            prefixes = {s["id"]: s["prefix"] for s in metadata["suites"]}
            metadata["runs"] = [
                present_run(r, self.repo.artifacts, prefixes.get(r.get("suite")))
                for r in metadata["runs"]
            ]
            for suite in metadata["suites"]:
                systems = (
                    suite["batch"].get("systems")
                    or sorted(
                        {r["system"] for r in metadata["runs"] if r.get("suite") == suite["id"]}
                    )
                    or ["multi"]
                )
                suite["results_label"] = "Open results" if len(systems) == 1 else "Open comparison"
                suite["title"] = suite_title(
                    suite, [r for r in metadata["runs"] if r.get("suite") == suite["id"]]
                )
            metadata["replay"] = metadata.get("provider") == "scripted" or (
                bool(metadata["runs"]) and all(r["model"] == "scripted" for r in metadata["runs"])
            )
            if metadata["status"] == "running" and ident not in self.tasks:
                metadata["status"] = "interrupted"
            self.outcome(metadata)
        return metadata

    def outcome(self, metadata):
        failed = [
            r
            for r in metadata["runs"]
            if r["status"] in {"failed", "resource_exhausted", "interrupted"}
        ]
        failures = []
        for run in failed:
            reason = run.get("grade", {}).get("process", {}).get("stop_reason")
            if not reason:
                reason = self.repo.artifacts.json(run["prefix"] + "/result.json").get("stop_reason")
            failures.append(f"{run['system']}: {failure_label(reason or '')}")
        skipped, budget_stops, zero_spend = [], [], True
        for suite in metadata["suites"]:
            prefix = suite["prefix"]
            board = self.repo.artifacts.json(prefix + "/leaderboard.json")
            skipped.extend(board.get("skipped", suite["batch"].get("skipped", [])))
            stop = self.repo.artifacts.json(prefix + "/budget-stop.json")
            if stop and "budget" in stop.get("reason", "").lower():
                budget_stops.append(stop)
            ledger = self.repo.artifacts.json(prefix + "/budget.json")
            after = self.repo.artifacts.json(prefix + "/spend-after.json")
            zero_spend &= not ledger.get("calls") and after.get("usage_delta") in {"0", 0}
        budget_stops.extend(
            reason
            for run in metadata["runs"]
            if "budget"
            in (
                reason := (run.get("grade", {}).get("process", {}).get("stop_reason") or "").lower()
            )
            and "refus" in reason
        )
        finished = metadata["status"] in {"completed", "stopped"}
        target = (
            "results"
            if metadata["suites"]
            and all(s["results_label"] == "Open results" for s in metadata["suites"])
            else "comparison"
        )
        if skipped and not metadata["output"].strip():
            metadata["output"] = "\n".join(
                f"Skipped {s.get('fault', 'attempt')} | {s.get('system', 'unknown')}: {s['reason']}"
                for s in skipped
            )
        if finished and ((skipped and not metadata["runs"]) or budget_stops):
            metadata["status"] = "stopped"
            if skipped and not metadata["runs"] and budget_stops:
                cost = "USD 0 spent" if zero_spend else "spend reconciliation unavailable"
                metadata["outcome"] = (
                    f"Stopped: budget caps refused all {len(skipped)} attempts before sending; {cost}. Raise Suite cap USD or Per-run cap USD and launch again."
                )
            else:
                reason = "Budget cap prevented further calls. " if budget_stops else ""
                metadata["outcome"] = (
                    f"Stopped: {reason}{len(skipped)} attempts skipped; {len(metadata['runs'])} run reports retained. Open the {target} for reasons and recorded usage."
                )
        elif metadata["status"] == "running":
            metadata["outcome"] = (
                f"Watch progress below, then open the {target} or a run report as it appears."
            )
        elif metadata["runs"]:
            metadata["outcome"] = (
                f"{len(metadata['runs'])} run reports available. Open the {target}, or choose a report below."
            )
        else:
            metadata["outcome"] = (
                f"No new reports retained. Open the {target} and job log for the outcome."
            )
        if failures:
            metadata["outcome"] = (
                f"{len(failed)} of {len(metadata['runs'])} runs failed: {'; '.join(failures)}. "
                + metadata["outcome"]
            )
            if any("output truncated" in failure for failure in failures):
                metadata["outcome"] += " " + failure_explanation("output_truncated")
        if metadata["replay"]:
            metadata["outcome"] += (
                " Canned pipeline replay for testing the console; inconclusive is expected."
            )
        metadata["outcome_tone"] = (
            "warning"
            if failed or metadata["status"] in {"stopped", "failed", "interrupted"}
            else "info"
        )
        metadata["heading"] = (
            f"finished, {len(failed)} of {len(metadata['runs'])} runs failed"
            if metadata["status"] == "completed" and failed
            else metadata["status"]
        )

    async def execute(self, ident: str, argv: list[str], output: Path):
        store = self.repo.artifacts
        metadata = store.json(f"jobs/{ident}/job.json")
        process = None
        reader = None
        captured = bytearray()

        async def capture():
            while chunk := await process.stdout.read(8192):
                if len(captured) < 2_000_000:
                    captured.extend(chunk[: 2_000_000 - len(captured)])
                    store.write(f"jobs/{ident}/output.log", captured)

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
            reader = asyncio.create_task(capture())
            async with asyncio.timeout(metadata["plan"]["timeout_seconds"]):
                await asyncio.shield(reader)
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
                with suppress(ProcessLookupError):
                    process.terminate()
                metadata["termination"] = "SIGTERM"
                try:
                    async with asyncio.timeout(metadata["plan"]["termination_grace_seconds"]):
                        await process.wait()
                        if reader:
                            await asyncio.shield(reader)
                except TimeoutError:
                    if process.returncode is None:
                        process.kill()
                        metadata["termination"] = "SIGKILL"
                        await process.wait()
            if reader:
                if not reader.done():
                    reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            if process:
                metadata["exit_code"] = process.returncode
            store.save_json(f"jobs/{ident}/job.json", metadata)
            store.ingest_tree(output, output.relative_to(self.root).as_posix())
            self.repo.runs.refresh()
            self._refreshed = monotonic()
            if metadata["status"] == "completed":
                updated = self.get(ident)
                metadata.update(status=updated["status"], outcome=updated["outcome"])
                store.save_json(f"jobs/{ident}/job.json", metadata)

    async def close(self):
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
