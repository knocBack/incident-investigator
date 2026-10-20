"""Storage-agnostic presentation helpers for the slim console."""

from __future__ import annotations

import html
import json
from collections import Counter
from datetime import datetime

from markdown_it import MarkdownIt
from markupsafe import Markup

from incident_investigator.grading.suite import _pass_metrics, reliability_metrics
from incident_investigator.graph_config import recorded_system
from incident_investigator.workspace import total


def pretty(value):
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def markdown(text, run_id="", references=()):
    renderer = MarkdownIt("commonmark", {"html": False}).enable("table").disable("image")
    rendered = renderer.render(text)
    from urllib.parse import quote

    for ident in references:
        rendered = rendered.replace(
            f"<code>{html.escape(ident)}</code>",
            f'<a class="ref" href="/runs/{quote(run_id, safe="")}/evidence/{quote(ident, safe="")}">{html.escape(ident)}</a>',
        )
    return Markup(rendered)


def observations(rows, params):
    def accepted(row):
        if params.get("source") and row.get("source") != params["source"]:
            return False
        if params.get("service") and row.get("data", {}).get("service") != params["service"]:
            return False
        for field, before in (("start", False), ("end", True)):
            if params.get(field):
                try:
                    point = datetime.fromisoformat(row["event_time"].replace("Z", "+00:00"))
                    boundary = datetime.fromisoformat(params[field].replace("Z", "+00:00"))
                    if point > boundary if before else point < boundary:
                        return False
                except (KeyError, ValueError, TypeError):
                    return False
        return True

    selected = [r for r in rows if accepted(r)]
    return {
        "rows": selected[:1000],
        "count": len(selected),
        "sources": sorted({r.get("source", "") for r in rows}),
        "services": sorted({r.get("data", {}).get("service", "") for r in rows}),
    }


def _failures(runs):
    return sum(r.get("status") in {"failed", "resource_exhausted", "interrupted"} for r in runs)


def suite_metrics(runs, k):
    groups = {}
    for run in runs:
        groups.setdefault(
            (
                run["fault"],
                run["model"],
                run["graph"],
                recorded_system(run.get("system"), run["graph"]),
                run.get("case_set", "primary"),
            ),
            [],
        ).append(run)
    rows = []
    for (fault, model, graph, system, case_set), items in sorted(groups.items()):
        n = len(items)
        correct = sum(r["verdict"] == "correct" for r in items)
        effective_k = min(k, n)
        at, power = _pass_metrics(n, correct, effective_k)
        grounded = [
            value
            for run in items
            if (value := run.get("grade", {}).get("grounding", {}).get("hypothesis_claim_fraction"))
            is not None
        ]
        rows.append(
            {
                "fault": fault,
                "model": model,
                "graph": graph,
                "system": system,
                "case_set": case_set,
                "verdicts": dict(Counter(r["verdict"] for r in items)),
                "provisional": any(r["verdict"] == "ungraded" for r in items),
                "reliability": reliability_metrics([r.get("grade", {}) for r in items]),
                "resources": {
                    field: total(items, field)
                    for field in (
                        "cost",
                        "known_cost",
                        "input_tokens",
                        "output_tokens",
                        "known_input_tokens",
                        "known_output_tokens",
                        "model_calls",
                        "tool_calls",
                        "wall_seconds",
                    )
                },
                "runs": items,
                "n": n,
                "correct": correct,
                "grounding": sum(grounded) / len(grounded) if grounded else None,
                "grounding_measured": len(grounded),
                "execution_failures": _failures(items),
                "pass1": correct / n,
                "passk": at,
                "powerk": power,
                "k": effective_k,
            }
        )
    return rows


def system_metrics(board, runs, k):
    """Use recorded leaderboard headlines; fall back to observed attempts while live."""
    rows = board.get("models", [])
    if not rows:
        groups = {}
        for run in runs:
            groups.setdefault((run["system"], run["model"], run["case_set"]), []).append(run)
        rows = []
        for (system, model, case_set), items in sorted(groups.items()):
            metrics = suite_metrics(items, k)
            count = sum(row["grounding_measured"] for row in metrics)
            rows.append(
                {
                    "system": system,
                    "models": {"all": model},
                    "suite_kind": "primary" if case_set == "primary" else "reliability",
                    "runs": len(items),
                    "accuracy": sum(r["correct"] for r in metrics) / len(items),
                    "execution_failures": _failures(items),
                    "mean_grounding": sum(
                        r["grounding"] * r["grounding_measured"]
                        for r in metrics
                        if r["grounding_measured"]
                    )
                    / count
                    if count
                    else None,
                    "grounding_measured": count,
                    "grounding_total": len(items),
                    **{
                        "mean_" + field: value / len(items) if value is not None else None
                        for field in (
                            "model_calls",
                            "tool_calls",
                            "cost",
                            "known_cost",
                            "wall_seconds",
                        )
                        for value in [total(items, field)]
                    },
                    "faults": {r["fault"]: {"n": r["n"], "correct": r["correct"]} for r in metrics},
                }
            )
    result = []
    for row in rows:
        passes = [
            _pass_metrics(f["n"], f["correct"], min(k, f["n"]))
            for f in row.get("faults", {}).values()
            if f["n"]
        ]
        result.append(
            {
                **row,
                "passk": sum(p[0] for p in passes) / len(passes) if passes else None,
                "powerk": sum(p[1] for p in passes) / len(passes) if passes else None,
            }
        )
    return result


def summary(runs):
    from incident_investigator.workspace import total

    return {
        "runs": len(runs),
        "correct": sum(r["verdict"] == "correct" for r in runs),
        "failures": _failures(runs),
        "cost": total(runs, "cost"),
        "known_cost": total(runs, "known_cost"),
        "calls": sum(r["calls"] for r in runs),
        "events": sum(r["events"] for r in runs),
        "wall_seconds": total(runs, "wall_seconds"),
    }


def paired_metrics(metrics):
    pairs = {}
    for row in metrics:
        key = (row["fault"], row["model"], row["case_set"])
        pair = pairs.setdefault(key, {"fault": key[0], "model": key[1], "case_set": key[2]})
        pair[row["system"]] = row
    return [pairs[key] for key in sorted(pairs)]
