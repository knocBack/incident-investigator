"""Sequential reproducible lab batches; no judge and no hidden runtime expectations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

import httpx

from incident_investigator.grading.budget import BoundedModel, SpendGuard, amount, price_bound
from incident_investigator.grading.grader import FAMILIES, expectation, grade_run, measured_sum


def save(path, value):
    path.write_text(json.dumps(value, indent=2, default=str) + "\n")


def leaderboard(grades, out, skipped=()):
    out.mkdir(parents=True, exist_ok=True)
    groups = {}
    for g in grades:
        models = g.get("models", {})
        name = json.dumps(models, sort_keys=True)
        groups.setdefault(name, []).append(g)
    rows = []
    for name, items in groups.items():

        def mean(field, items=items):
            values = [g["resources"]["totals"].get(field) for g in items]
            total = measured_sum(values)
            return total / len(items) if total is not None else None

        ground = [g["grounding"]["hypothesis_claim_fraction"] for g in items]
        walls = [g["resources"]["wall_seconds"] for g in items]
        rows.append(
            {
                "models": json.loads(name),
                "runs": len(items),
                "verdicts": {
                    v: sum(g["verdict"] == v for g in items)
                    for v in ("correct", "partial", "wrong", "inconclusive", "no_report")
                },
                "mean_grounding": measured_sum(ground) / len(items)
                if measured_sum(ground) is not None
                else None,
                "mean_cost": mean("cost"),
                "mean_input_tokens": mean("input_tokens"),
                "mean_output_tokens": mean("output_tokens"),
                "mean_model_calls": mean("model_calls"),
                "mean_wall_seconds": measured_sum(walls) / len(items)
                if measured_sum(walls) is not None
                else None,
                "execution_failures": sum(
                    g["process"]["execution_status"] in {"failed", "resource_exhausted"}
                    for g in items
                ),
            }
        )
    data = {
        "classification": "exploratory",
        "acceptance_threshold": None,
        "grader_version": "grader.v1",
        "models": rows,
        "runs": grades,
        "skipped": list(skipped),
    }
    save(out / "leaderboard.json", data)
    lines = [
        "# Exploratory evaluation",
        "",
        "No acceptance threshold. Missing measurements remain null. Citation grounding is a provenance proxy; human semantic review remains pending.",
        "",
        "| Models | Runs | Verdicts | Grounding | Mean USD | Mean tokens in/out | Wall s | Failures |",
        "| --- | ---: | --- | --- | --- | --- | --- | ---: |",
    ]
    for r in rows:
        lines.append(
            f"| {json.dumps(r['models'], sort_keys=True)} | {r['runs']} | {r['verdicts']} | {r['mean_grounding']} | {r['mean_cost']} | {r['mean_input_tokens']}/{r['mean_output_tokens']} | {r['mean_wall_seconds']} | {r['execution_failures']} |"
        )
    if skipped:
        lines.extend(["", "## Skipped attempts", ""])
        lines.extend(
            f"- {item['model']} ({item['fault']}, repeat {item['repeat']}): {item['reason']}"
            for item in skipped
        )
    (out / "leaderboard.md").write_text("\n".join(lines) + "\n")
    return data


async def key_usage(client):
    response = await client.get(
        "https://openrouter.ai/api/v1/key",
        headers={"Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"].strip()},
    )
    response.raise_for_status()
    return response.json()["data"]["usage"]


async def run_suite(args, *, demo_fn=None, model_factory=None):
    from incident_investigator import cli
    from incident_investigator.models import OpenRouterModel

    faults = list(FAMILIES) if args.faults == "all" else args.faults.split(",")
    if not faults or set(faults) - set(FAMILIES) or args.repeats < 1:
        raise ValueError("known faults and repeats >= 1 required")
    amount(args.budget_usd)
    max_input_tokens = getattr(args, "max_input_tokens", 64000)
    if max_input_tokens < 1:
        raise ValueError("max input tokens must be positive")
    out = args.out
    out.mkdir(parents=True, exist_ok=False)
    batch = {
        "version": "suite.v1",
        "exposure": "development lab; exploratory",
        "faults": faults,
        "models": args.models.split(","),
        "repeats": args.repeats,
        "budget_usd": args.budget_usd,
        "max_input_tokens": max_input_tokens,
        "run_order": "model, repeat, fault",
        "settings": "runtime defaults; one selected model for all roles",
        "prompt_hashes": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (Path(__file__).parents[1] / "prompts").glob("*.md")
        },
        "expectation_hashes": {
            family: hashlib.sha256(
                json.dumps(expectation(family), sort_keys=True).encode()
            ).hexdigest()
            for family in faults
        },
        "started_at": time.time(),
        "skipped": [],
    }
    save(out / "batch.json", batch)
    grades = []
    guard = None
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        catalog = {}
        if model_factory is None:
            initial = await key_usage(client)
            save(out / "spend-before.json", {"usage": initial})
            guard = SpendGuard(initial, args.budget_usd)
            response = await client.get("https://openrouter.ai/api/v1/models")
            response.raise_for_status()
            catalog = {m["id"]: m for m in response.json()["data"]}
            save(out / "prices.json", {m: catalog[m] for m in args.models.split(",")})
        try:
            for model_id in args.models.split(","):
                for repeat in range(args.repeats):
                    for fault in faults:
                        if guard:
                            guard.observe(await key_usage(client))
                            try:
                                bound = price_bound(
                                    catalog[model_id], max_input_tokens=max_input_tokens
                                )
                            except (KeyError, ValueError) as exc:
                                reason = str(exc)
                            else:
                                if bound > 0 and guard.charged() >= guard.cap:
                                    save(
                                        out / "budget-stop.json",
                                        {
                                            "model": model_id,
                                            "fault": fault,
                                            "repeat": repeat,
                                            "reason": "suite budget cap already spent",
                                            **guard.snapshot(),
                                        },
                                    )
                                    return leaderboard(grades, out, batch["skipped"])
                                reason = (
                                    None
                                    if guard.launch_allowed(bound)
                                    else ("suite budget reservation cannot fit remaining budget")
                                )
                            if reason:
                                skipped = {
                                    "model": model_id,
                                    "fault": fault,
                                    "repeat": repeat,
                                    "reason": reason,
                                }
                                batch["skipped"].append(skipped)
                                save(out / "batch.json", batch)
                                save(
                                    out / "budget-stop.json",
                                    {
                                        "model": model_id,
                                        "fault": fault,
                                        "repeat": repeat,
                                        "reason": reason,
                                        **guard.snapshot(),
                                    },
                                )
                                leaderboard(grades, out, batch["skipped"])
                                continue
                        if model_factory:
                            adapter = model_factory(model_id)
                        else:
                            adapter = OpenRouterModel()
                            # Explicit all-role selection avoids inherited per-role overrides.
                            adapter._models = dict.fromkeys(adapter._models, model_id)
                            adapter = BoundedModel(
                                adapter, catalog, guard, max_input_tokens=max_input_tokens
                            )
                        # Inject the adapter through the CLI factory without altering runtime.
                        original = cli.model
                        cli.model = lambda args, adapter=adapter: adapter
                        started = time.monotonic()
                        try:
                            directory = await (demo_fn or cli.demo)(
                                argparse.Namespace(
                                    model=args.model, script=None, out=out, fault=fault
                                )
                            )
                        except Exception as exc:
                            directory = out / str(uuid4())
                            directory.mkdir()
                            save(
                                directory / "attempt-error.json",
                                {
                                    "exception_type": type(exc).__name__,
                                    "model": model_id,
                                    "fault": fault,
                                    "repeat": repeat,
                                },
                            )
                        finally:
                            cli.model = original
                        g = grade_run(directory, expectation(fault))
                        g["resources"]["wall_seconds"] = time.monotonic() - started
                        g["suite_attempt"] = {"model": model_id, "repeat": repeat, "fault": fault}
                        if (directory / "attempt-error.json").exists():
                            g["process"]["execution_status"] = "failed"
                        save(directory / "grade.json", g)
                        grades.append(g)
                        leaderboard(grades, out, batch["skipped"])
                        if guard:
                            guard.observe(await key_usage(client))
                            save(
                                out / "budget.json",
                                {"state": guard.snapshot(), "calls": guard.records},
                            )
        finally:
            if guard:
                guard.observe(await key_usage(client))
                save(out / "spend-after.json", {"usage_delta": str(guard.key_delta)})
                save(out / "budget.json", {"state": guard.snapshot(), "calls": guard.records})
    return leaderboard(grades, out, batch["skipped"])


def add_commands(sub):
    command = sub.add_parser("eval")
    command.add_argument("run_dirs", nargs="+", type=Path)
    command.add_argument("--fault", choices=FAMILIES)
    command.add_argument("--out", type=Path)
    command = sub.add_parser("eval-suite")
    command.add_argument("--faults", default="all")
    command.add_argument("--model", choices=["openrouter", "scripted"], default="openrouter")
    command.add_argument("--models", required=True)
    command.add_argument("--repeats", type=int, default=1)
    command.add_argument("--budget-usd", required=True)
    command.add_argument("--max-input-tokens", type=int, default=64000)
    command.add_argument("--out", type=Path, required=True)
    command.add_argument("--script", type=Path)


async def execute(args):
    if args.command == "eval":
        grades = [
            grade_run(
                p,
                expectation(args.fault) if args.fault else None,
                output=args.out / p.name if args.out else None,
            )
            for p in args.run_dirs
        ]
        if args.out:
            leaderboard(grades, args.out)
        print(json.dumps(Counter(g["verdict"] for g in grades)))
    else:
        from incident_investigator.models import ScriptedModel

        factory = (
            (lambda _: ScriptedModel(json.loads(args.script.read_text()) if args.script else []))
            if args.model == "scripted"
            else None
        )
        await run_suite(args, model_factory=factory)
