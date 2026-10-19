"""Local human/JSON exports from accepted results; no model calls on closure."""

from __future__ import annotations

import json
from pathlib import Path

from incident_investigator.contracts import PAYLOADS
from incident_investigator.sources.loader import CaseSnapshot


def export_report(state: dict, snapshot: CaseSnapshot, out: Path) -> None:
    c, inbox = state["control"], state["inbox"]
    usage = {}
    for role in PAYLOADS:
        calls = [call for call in c["calls"].values() if call["role"] == role]
        rows = [inbox.get(call["call_id"], {}) for call in calls]
        model_rows = [inbox.get(call["call_id"], {}) for call in calls if call["kind"] == "model"]
        tokens = {}
        for kind in ("input_tokens", "output_tokens"):
            values = [(row.get("usage") or {}).get(kind) for row in model_rows]
            tokens[kind] = sum(values) if all(v is not None for v in values) else "unknown"
            tokens["known_" + kind] = sum(v for v in values if v is not None)
        costs = [(row.get("usage") or {}).get("cost") for row in model_rows]
        usage[role] = {
            "model": c["models"].get(role, "unknown"),
            "cost": sum(costs) if all(cost is not None for cost in costs) else "unknown",
            "known_cost": sum(cost for cost in costs if cost is not None),
            "model_calls": sum(call["kind"] == "model" for call in calls),
            "tool_calls": sum(call["kind"] == "tool" for call in calls),
            "repairs": c.get("repairs", {}).get(role, 0),
            "deduplicated_queries": c.get("deduplicated", {}).get(role, 0),
            **tokens,
            "latency_ms": round(sum(row.get("latency_ms", 0) for row in rows), 3),
        }
    reasoning = c["tasks"].get(c["hypothesis_revision"], {}).get("result") or {}
    report = c["tasks"].get(c["report_task"], {}).get("result") or {}
    result = {
        "schema_version": "m3.v1",
        "run_id": c["run_id"],
        "case_id": c["case_id"],
        "dataset_digest": snapshot.dataset_digest,
        "revision": c["revision"],
        "status": c["status"],
        "stop_reason": c["stop_reason"],
        "completeness": "complete"
        if c["status"] == "completed" and not c["partial"]
        else "partial",
        "diagnosis": reasoning.get("conclusion", "inconclusive"),
        "hypotheses": reasoning.get("hypotheses", []),
        "report": report,
        "usage": usage,
        "repairs": {role: row["repairs"] for role, row in usage.items()},
        "models": c["models"],
        "state": c,
        "receipts": inbox,
    }
    (out / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    lines = [
        f"# Incident {c['case_id']}",
        "",
        f"Run: {c['run_id']} · saved revision {c['revision']}",
        "",
        f"Execution: **{c['status']}** · Report: **{result['completeness']}** · Diagnosis: **{result['diagnosis']}**",
        "",
        "## Summary",
        "",
        report.get("summary") or "Partial summary of accepted work; no final Reporting result.",
    ]
    if c["stop_reason"]:
        lines += ["", f"Stop reason: {c['stop_reason']}"]
    lines += ["", "## Ranked hypotheses", ""]

    def citation(key):
        if key in c["evidence"]:
            record = c["evidence"][key]
            excerpt = json.dumps(record["data"], ensure_ascii=False).replace("\n", " ")[:240]
            return f"`{key}` — {record['source']}: {excerpt}"
        receipt = c["tool_receipts"][key]
        page = receipt.get("page") or {}
        return f"`{key}` — {receipt['tool']} receipt: {receipt.get('error') or page.get('error') or page.get('source_status')}"

    for index, hypothesis in enumerate(result["hypotheses"], 1):
        lines += [
            f"{index}. **{hypothesis['statement']}** — confidence: {hypothesis['confidence']}"
        ]
        for title, field in (
            ("Supporting evidence", "supporting_evidence"),
            ("Contradicting evidence", "contradicting_evidence"),
        ):
            lines += ["", f"### {title} ({len(hypothesis[field])})", ""]
            lines += [f"- {citation(key)}" for key in hypothesis[field]] or ["None recorded."]
        if hypothesis["assumptions"]:
            lines += ["", "Assumptions: " + "; ".join(hypothesis["assumptions"])]
    if not result["hypotheses"]:
        lines += [
            "No ranked hypotheses were returned.",
            "",
            "### Supporting evidence",
            "",
            "None ranked.",
            "",
            "### Contradicting evidence",
            "",
            "None ranked.",
        ]
    lines += ["", "## Facts", ""]
    for fact in report.get("facts", []):
        lines += [f"- {fact['statement']}"]
        lines += [f"  - {citation(ref['id'])}" for ref in fact["references"]]
    if not report.get("facts"):
        lines += ["None recorded."]
    lines += ["", "## Assumptions", ""]
    lines += [f"- {item}" for item in report.get("assumptions", [])] or ["None recorded."]
    lines += [
        "",
        "## Corrective recommendations and verification",
        "",
        "Proposed actions only; this investigation applies no fixes.",
        "",
    ]
    for item in report.get("recommendations", []):
        lines += [f"- {item['action']} Verification: {item['verification']}"]
        lines += [f"  - {citation(key)}" for key in item["evidence_ids"]]
    if not report.get("recommendations"):
        lines += ["None recorded."]
    lines += ["", "## Open and unresolved objections", ""]
    for key, objection in c["objections"].items():
        lines += [
            f"- {key}: {objection['question']} — {c['dispositions'].get(key, {}).get('reason', 'unresolved')}"
        ]
    if not c["objections"]:
        lines += ["None recorded."]
    lines += ["", "## Evidence gaps and task outcomes", ""]
    for task in c["tasks"].values():
        lines += [
            f"- {task['role']} `{task['task_id']}`: {task['status']}"
            + (f" — {task['failure']}" if task["failure"] else "")
        ]
        lines += [f"  - {gap}" for gap in (task.get("result") or {}).get("gaps", [])]
    for key, receipt in c["tool_receipts"].items():
        if receipt.get("error") or (receipt.get("page") or {}).get("source_status") != "complete":
            lines += [f"- Gap: {citation(key)}"]
    lines += [
        "",
        "## Per-role resource usage",
        "",
        "| Role | Model | Model calls | Tool calls | Repairs | Input tokens | Output tokens | Cost (provider) | Latency ms |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for role, row in usage.items():
        lines += [
            f"| {role} | {row['model']} | {row['model_calls']} | {row['tool_calls']} | {row['repairs']} | {row['input_tokens']} | {row['output_tokens']} | {row['cost']} | {row['latency_ms']} |"
        ]
    (out / "report.md").write_text("\n".join(lines) + "\n")
