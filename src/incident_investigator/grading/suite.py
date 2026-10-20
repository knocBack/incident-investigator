"""Sequential reproducible lab batches; no judge and no hidden runtime expectations."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

import httpx

from incident_investigator.contracts import PROFILES
from incident_investigator.grading.budget import (
    BoundedModel,
    RunSpendGuard,
    SpendGuard,
    amount,
    price_bound,
)
from incident_investigator.grading.grader import (
    CASES,
    FAMILIES,
    VERSION,
    expectation,
    grade_run,
    measured_sum,
)
from incident_investigator.graph_config import DEFAULT_GRAPH, add_graph_argument, recorded_graph
from incident_investigator.lab.cases import SECOND_CASES, VARIANTS
from incident_investigator.workspace import batch_output


def save(path, value):
    path.write_text(json.dumps(value, indent=2, default=str) + "\n")


def _pass_metrics(n, correct, k):
    k = min(k, n)
    if not n or not k:
        return None, None
    denominator = math.comb(n, k)
    return 1 - math.comb(n - correct, k) / denominator, math.comb(correct, k) / denominator


def _fault_metrics(items, k):
    def fault_of(g):
        # Standalone `eval` grades have a family but no suite job metadata.
        return g.get("suite_attempt", {}).get("fault", g.get("family", "unknown"))

    metrics = {}
    for fault in sorted({fault_of(g) for g in items}):
        group = [g for g in items if fault_of(g) == fault]
        correct = sum(g["verdict"] == "correct" for g in group)
        n = len(group)
        pass_at_k, pass_to_k = _pass_metrics(n, correct, min(k, n))
        metrics[fault] = {
            "n": n,
            "correct": correct,
            "pass_at_1": correct / n,
            "grounding": (
                measured_sum(g["grounding"]["hypothesis_claim_fraction"] for g in group) / n
                if measured_sum(g["grounding"]["hypothesis_claim_fraction"] for g in group)
                is not None
                else None
            ),
            "verdicts": dict(Counter(g["verdict"] for g in group)),
            "resources": {
                field: measured_sum(g["resources"]["totals"].get(field) for g in group)
                for field in (
                    "model_calls",
                    "tool_calls",
                    "input_tokens",
                    "output_tokens",
                    "cost",
                    "known_input_tokens",
                    "known_output_tokens",
                    "known_cost",
                )
            },
            "wall_seconds": measured_sum(g["resources"]["wall_seconds"] for g in group),
            "pass_at_k": pass_at_k,
            "pass_to_k": pass_to_k,
            "k": min(k, n),
        }
    return metrics


def leaderboard(grades, out, skipped=(), k=4):
    out.mkdir(parents=True, exist_ok=True)
    groups = {}
    for g in grades:
        models = g.get("models", {})
        name = (
            recorded_graph(g.get("graph")),
            g.get("suite_kind", "primary"),
            json.dumps(models, sort_keys=True),
        )
        groups.setdefault(name, []).append(g)
    rows = []
    for (graph, suite_kind, name), items in groups.items():

        def mean(field, items=items):
            values = [g["resources"]["totals"].get(field) for g in items]
            total = measured_sum(values)
            return total / len(items) if total is not None else None

        ground = [g["grounding"]["hypothesis_claim_fraction"] for g in items]
        walls = [g["resources"]["wall_seconds"] for g in items]
        rows.append(
            {
                "models": json.loads(name),
                "graph": graph,
                "system": "single" if graph == "single" else "multi",
                "suite_kind": suite_kind,
                "runs": len(items),
                "accuracy": sum(g["verdict"] == "correct" for g in items) / len(items),
                "verdicts": {
                    v: sum(g["verdict"] == v for g in items)
                    for v in ("correct", "partial", "wrong", "inconclusive", "no_report")
                },
                "mean_grounding": measured_sum(ground) / len(items)
                if measured_sum(ground) is not None
                else None,
                "mean_cost": mean("cost"),
                "mean_known_cost": mean("known_cost"),
                "mean_known_input_tokens": mean("known_input_tokens"),
                "mean_known_output_tokens": mean("known_output_tokens"),
                "mean_input_tokens": mean("input_tokens"),
                "mean_output_tokens": mean("output_tokens"),
                "mean_model_calls": mean("model_calls"),
                "mean_tool_calls": mean("tool_calls"),
                "mean_wall_seconds": measured_sum(walls) / len(items)
                if measured_sum(walls) is not None
                else None,
                "execution_failures": sum(
                    g["process"]["execution_status"] in {"failed", "resource_exhausted"}
                    for g in items
                ),
                "faults": _fault_metrics(items, k),
            }
        )
    data = {
        "classification": "exploratory",
        "acceptance_threshold": None,
        "grader_version": VERSION,
        "models": rows,
        "runs": grades,
        "skipped": list(skipped),
    }
    save(out / "leaderboard.json", data)

    def display(value):
        return (
            "unknown"
            if value is None
            else f"{value:.6g}"
            if isinstance(value, float)
            else str(value)
        )

    def usage_display(total, known):
        return display(total) if total is not None else f"unknown (known {display(known)})"

    lines = [
        "# Exploratory evaluation",
        "",
        "No acceptance threshold. Primary diagnosis and reliability success are separate. Missing measurements remain unknown. Grounding measures provenance; semantic review remains required.",
        "",
        "| System | Graph | Set | Model(s) | Runs | Accuracy / success | Grounding | Mean model/tool calls | Mean tokens in/out | Mean USD | Mean wall s |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | --- | --- | ---: | ---: |",
    ]
    for r in rows:
        models = ", ".join(sorted(set(r["models"].values()))) or "unknown"
        lines.append(
            f"| {r['system']} | {r['graph']} | {r['suite_kind']} | {models} | {r['runs']} | {display(r['accuracy'])} | {display(r['mean_grounding'])} | {display(r['mean_model_calls'])}/{display(r['mean_tool_calls'])} | {usage_display(r['mean_input_tokens'], r['mean_known_input_tokens'])}/{usage_display(r['mean_output_tokens'], r['mean_known_output_tokens'])} | {usage_display(r['mean_cost'], r['mean_known_cost'])} | {display(r['mean_wall_seconds'])} |"
        )
    lines.extend(
        [
            "",
            "## Per incident and system",
            "",
            "Resource columns below are totals over all retained repeats. k is capped by the number of attempts.",
            "",
            "| Incident | Set | System | Model(s) | n | Verdicts | pass@1 | k | pass@k | pass^k | Grounding | Model/tool calls | Tokens in/out | USD | Wall s |",
            "| --- | --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | ---: |",
        ]
    )
    for r in rows:
        models = ", ".join(sorted(set(r["models"].values()))) or "unknown"
        for incident, metric in r["faults"].items():
            resource = metric["resources"]
            lines.append(
                f"| {incident} | {r['suite_kind']} | {r['system']} | {models} | {metric['n']} | {metric['verdicts']} | {display(metric['pass_at_1'])} | {metric['k']} | {display(metric['pass_at_k'])} | {display(metric['pass_to_k'])} | {display(metric['grounding'])} | {display(resource['model_calls'])}/{display(resource['tool_calls'])} | {usage_display(resource['input_tokens'], resource['known_input_tokens'])}/{usage_display(resource['output_tokens'], resource['known_output_tokens'])} | {usage_display(resource['cost'], resource['known_cost'])} | {display(metric['wall_seconds'])} |"
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

    selection = getattr(args, "set", None)
    if selection:
        faults = (
            list(FAMILIES) + list(SECOND_CASES)
            if selection == "primary"
            else list(VARIANTS)
            if selection == "variants"
            else list(FAMILIES) + list(CASES)
        )
        if args.faults != "all":
            faults = args.faults.split(",")
    else:
        faults = list(FAMILIES) if args.faults == "all" else args.faults.split(",")
    selected_system = getattr(args, "system", "multi")
    systems = ["multi", "single"] if selected_system == "both" else [selected_system]
    repeats = getattr(args, "repeats", None) or 1
    concurrency = getattr(args, "concurrency", None) or 1
    if not faults or set(faults) - set(FAMILIES + CASES) or repeats < 1 or concurrency < 1:
        raise ValueError("known faults and repeats >= 1 required")
    amount(args.budget_usd)
    run_budget = getattr(args, "run_budget_usd", None) or args.budget_usd
    amount(run_budget)
    max_input_tokens = getattr(args, "max_input_tokens", 64000)
    if max_input_tokens < 1:
        raise ValueError("max input tokens must be positive")
    out = args.out
    out.mkdir(parents=True, exist_ok=False)
    batch = {
        "version": "suite.v2",
        "systems": systems,
        "suite_set": selection or "legacy",
        "limits": PROFILES["demo-live" if args.model == "openrouter" else "demo"].model_dump(
            mode="json"
        ),
        "graph": getattr(args, "graph", DEFAULT_GRAPH),
        "exposure": "development lab; exploratory",
        "faults": faults,
        "models": args.models.split(","),
        "repeats": repeats,
        "concurrency": concurrency,
        "budget_usd": args.budget_usd,
        "run_budget_usd": run_budget,
        "max_input_tokens": max_input_tokens,
        "run_order": "model, repeat, fault, system",
        "settings": "runtime defaults; one selected model for all roles",
        "limits_profile": getattr(args, "limits_profile", None)
        or ("demo-live" if args.model == "openrouter" else "demo"),
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
    # Materialize each snapshot once. Both systems and every repeat see identical data.
    snapshots = {}
    if demo_fn is None:
        for fault in faults:
            snapshots[fault] = await cli.demo(
                argparse.Namespace(fault=fault, _freeze_only=True, _fixed_clock=True)
            )
            frozen = snapshots[fault]
            source_dir = out / "snapshots" / fault
            source_dir.mkdir(parents=True)
            save(source_dir / "alert.json", dict(frozen.alert))
            save(source_dir / "fault-plan.json", dict(frozen.fault_plan))
            (source_dir / "observations.jsonl").write_text(
                "".join(row.model_dump_json() + "\n" for row in frozen.observations)
            )
            save(
                source_dir / "manifest.json",
                {
                    "cases": {
                        frozen.case_id: {
                            "alert": "alert.json",
                            "observations": "observations.jsonl",
                            "fault_plan": "fault-plan.json",
                            "dataset_digest": frozen.dataset_digest,
                        }
                    }
                },
            )
    batch["dataset_digests"] = {
        fault: snapshot.dataset_digest for fault, snapshot in snapshots.items()
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
            if guard:
                guard.observe(await key_usage(client))
            candidates = [
                (model_id, repeat, fault, system)
                for model_id in args.models.split(",")
                for repeat in range(repeats)
                for fault in faults
                for system in systems
            ]
            jobs = []
            for model_id, repeat, fault, system in candidates:
                if guard:
                    try:
                        # Only preflight the output floor here. The actual prompt is
                        # bounded and reserved atomically immediately before each call.
                        bound = price_bound(catalog[model_id], max_input_tokens=0)
                        reason = (
                            None
                            if guard.launch_allowed(bound) and bound <= amount(run_budget)
                            else "suite budget reservation cannot fit remaining budget"
                        )
                    except (KeyError, ValueError) as exc:
                        reason = str(exc)
                    if reason:
                        batch["skipped"].append(
                            {
                                "model": model_id,
                                "fault": fault,
                                "system": system,
                                "repeat": repeat,
                                "reason": reason,
                            }
                        )
                        continue
                jobs.append((model_id, repeat, fault, system))
            save(out / "batch.json", batch)
            if not jobs and batch["skipped"]:
                save(out / "budget-stop.json", {**batch["skipped"][0], **guard.snapshot()})
            semaphore = asyncio.Semaphore(concurrency)

            async def run_job(model_id, repeat, fault, system):
                async with semaphore:
                    run_guard = RunSpendGuard(guard, run_budget) if guard else None
                    if model_factory:
                        adapter = model_factory(model_id)
                    else:
                        adapter = OpenRouterModel()
                        adapter._models = dict.fromkeys(adapter._models, model_id)
                        adapter = BoundedModel(
                            adapter, catalog, run_guard, max_input_tokens=max_input_tokens
                        )
                    print(f"Starting {fault} | model {model_id} | repeat {repeat + 1}", flush=True)
                    started = time.monotonic()
                    run_id = str(uuid4())
                    try:
                        directory = await (demo_fn or cli.demo)(
                            argparse.Namespace(
                                model=args.model,
                                script=None,
                                out=out,
                                fault=fault,
                                _model_adapter=adapter,
                                graph=batch["graph"],
                                limits_profile=getattr(args, "limits_profile", None),
                                system=system,
                                _snapshot=snapshots.get(fault),
                                _run_id=run_id,
                            )
                        )
                    except (Exception, asyncio.CancelledError) as exc:
                        directory = out / run_id
                        directory.mkdir(exist_ok=True)
                        save(
                            directory / "attempt-error.json",
                            {
                                "exception_type": type(exc).__name__,
                                "launch_id": os.environ.get("II_LAUNCH_ID"),
                                "model": model_id,
                                "fault": fault,
                                "repeat": repeat,
                            },
                        )
                    if run_guard:
                        save(
                            directory / "budget.json",
                            {"state": run_guard.snapshot(), "calls": run_guard.records},
                        )
                    g = grade_run(directory, expectation(fault))
                    g["graph"] = "single" if system == "single" else batch["graph"]
                    g["system"] = system
                    g["suite_kind"] = "reliability" if fault in VARIANTS else "primary"
                    g["case_id"] = fault
                    g["fault_exercised"] = (
                        bool(
                            json.loads((directory / "result.json").read_text())
                            .get("state", {})
                            .get("fault_counters")
                        )
                        if (directory / "result.json").exists()
                        else False
                    )
                    g["resources"]["wall_seconds"] = time.monotonic() - started
                    g["suite_attempt"] = {
                        "model": model_id,
                        "repeat": repeat,
                        "fault": fault,
                        "system": system,
                    }
                    if (directory / "attempt-error.json").exists():
                        g["process"]["execution_status"] = "failed"
                    save(directory / "grade.json", g)
                    print(f"{g['verdict']} | {directory / 'report.md'}", flush=True)
                    return g

            tasks = [asyncio.create_task(run_job(*job)) for job in jobs]
            try:
                grades.extend(await asyncio.gather(*tasks))
            except asyncio.CancelledError:
                for task in tasks:
                    if not task.done() and not task.cancelling():
                        task.cancel()
                settled = await asyncio.gather(*tasks, return_exceptions=True)
                grades.extend(value for value in settled if isinstance(value, dict))
                for job, value in zip(jobs, settled, strict=True):
                    if not isinstance(value, dict):
                        model_id, repeat, fault, system = job
                        batch["skipped"].append(
                            {
                                "model": model_id,
                                "repeat": repeat,
                                "fault": fault,
                                "system": system,
                                "reason": "suite cancelled before admission",
                            }
                        )
                save(out / "batch.json", batch)
                leaderboard(grades, out, batch["skipped"], getattr(args, "k", 4))
                raise
            if guard:
                guard.observe(await key_usage(client))
                save(out / "budget.json", {"state": guard.snapshot(), "calls": guard.records})
        finally:
            if guard:
                guard.observe(await key_usage(client))
                save(out / "spend-after.json", {"usage_delta": str(guard.key_delta)})
                save(out / "budget.json", {"state": guard.snapshot(), "calls": guard.records})
    return leaderboard(grades, out, batch["skipped"], getattr(args, "k", 4))


def add_commands(sub):
    command = sub.add_parser("eval")
    command.add_argument("run_dirs", nargs="+", type=Path)
    command.add_argument("--fault", choices=FAMILIES + CASES)
    command.add_argument("--out", type=Path, default=batch_output("eval"))
    command = sub.add_parser("eval-suite")
    add_graph_argument(command, allow_both=True)
    command.add_argument("--set", choices=["primary", "variants", "all"], default="primary")
    command.add_argument("--faults", default="all")
    command.add_argument("--model", choices=["openrouter", "scripted"], default="openrouter")
    command.add_argument("--models", required=True)
    command.add_argument("--repeats", type=int)
    command.add_argument("--concurrency", type=int)
    command.add_argument(
        "--quick", action="store_true", help="selected set once, with concurrency 5"
    )
    command.add_argument(
        "--k", type=int, default=4, help="pass@k/pass^k reporting cap (default: 4)"
    )
    command.add_argument("--budget-usd", default="2.00")
    command.add_argument(
        "--run-budget-usd", help="per-run cap, identical for both systems (default: suite cap)"
    )
    command.add_argument("--max-input-tokens", type=int, default=64000)
    command.add_argument("--out", type=Path, default=batch_output())
    command.add_argument("--trace", choices=["phoenix"])
    command.add_argument("--limits-profile", choices=PROFILES)
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
        if args.quick:
            args.repeats = args.repeats or 1
            args.concurrency = args.concurrency or 5
        else:
            args.repeats = args.repeats or 1
            args.concurrency = args.concurrency or 1
        if args.k < 1:
            raise ValueError("k must be positive")
        await run_suite(args, model_factory=factory)
