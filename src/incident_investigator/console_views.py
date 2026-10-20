"""Storage-agnostic presentation helpers for the slim console."""

from __future__ import annotations

import html
import json
from datetime import datetime

from markdown_it import MarkdownIt
from markupsafe import Markup

from incident_investigator.grading.suite import _pass_metrics


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
        groups.setdefault((run["fault"], run["model"], run["graph"]), []).append(run)
    rows = []
    for (fault, model, graph), items in sorted(groups.items()):
        n = len(items)
        correct = sum(r["verdict"] == "correct" for r in items)
        effective_k = min(k, n)
        at, power = _pass_metrics(n, correct, effective_k)
        rows.append(
            {
                "fault": fault,
                "model": model,
                "graph": graph,
                "system": "single" if graph == "single" else "multi",
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
        "calls": sum(r["calls"] for r in runs),
        "events": sum(r["events"] for r in runs),
        "wall_seconds": total(runs, "wall_seconds"),
    }
