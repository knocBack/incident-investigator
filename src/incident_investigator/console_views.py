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
                "pass1": correct / n,
                "passk": at,
                "powerk": power,
                "k": effective_k,
            }
        )
    return rows


def summary(runs):
    from incident_investigator.workspace import total

    return {
        "runs": len(runs),
        "correct": sum(r["verdict"] == "correct" for r in runs),
        "failures": sum(r["status"] in {"failed", "resource_exhausted"} for r in runs),
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
