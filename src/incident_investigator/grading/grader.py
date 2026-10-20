"""Versioned, conservative lexical grading with a separate human semantic worksheet."""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from incident_investigator.graph_config import recorded_graph

VERSION = "grader.v3"
_SOURCE_EXPECTED = Path(__file__).resolve().parents[3] / "evals" / "expected"
EXPECTED = Path(
    os.environ.get(
        "II_EVALS",
        _SOURCE_EXPECTED if _SOURCE_EXPECTED.is_dir() else Path(__file__).with_name("expected"),
    )
).expanduser()
FAMILIES = tuple(sorted(p.stem for p in (EXPECTED / "faults").glob("*.json")))

CASES = ("case-02", "case-04", "case-06", "case-08", "case-10", "reliability-01", "reliability-02")


def expectation(family):
    if family not in FAMILIES and family not in CASES:
        raise ValueError("unknown expectation")
    group = "faults" if family in FAMILIES else "cases"
    return json.loads((EXPECTED / group / f"{family}.json").read_text())


def matches(row, matcher):
    if (
        row.get("source") != matcher["source"]
        or row.get("data", {}).get("service") != matcher["service"]
    ):
        return False
    for field, predicate in matcher["predicates"].items():
        value = row.get("data", {})
        for part in field.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        for op, expected in predicate.items():
            if op == "eq":
                ok = value == expected
            elif op == "gte":
                ok = isinstance(value, int | float) and value >= expected
            elif op == "regex":
                ok = isinstance(value, str) and re.search(expected, value, re.I) is not None
            elif op == "contains":
                ok = isinstance(value, list) and any(
                    all(v.get(k) == x for k, x in expected.items())
                    for v in value
                    if isinstance(v, dict)
                )
            else:
                raise ValueError(f"unknown predicate {op}")
            if not ok:
                return False
    return True


def references(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"supporting_evidence", "contradicting_evidence", "evidence_ids"}:
                yield from item
            elif key == "references":
                yield from (r["id"] for r in item)
            else:
                yield from references(item)
    elif isinstance(value, list):
        for item in value:
            yield from references(item)


def measured_sum(values):
    values = list(values)
    return sum(values) if values and all(isinstance(v, int | float) for v in values) else None


def reliability_result(result, expected, verdict, justified):
    """Observe the actual failed call and its bounded successful retry, not counters."""
    if expected.get("expected_diagnosis") == "inconclusive":
        return {"kind": "uncertainty", "justified_uncertainty": justified, "success": justified}
    rule = expected.get("reliability_rule")
    if not rule:
        return None
    state = result.get("state", {})
    calls = state.get("calls", {})
    receipts = result.get("receipts", {})
    selected = {
        key: call
        for key, call in calls.items()
        if call.get("kind") == "tool" and (call.get("fault") or {}).get("rule_id") == rule
    }
    failed = {
        key: call
        for key, call in selected.items()
        if receipts.get(key, {}).get("error")
        or (receipts.get(key, {}).get("payload") or {}).get("source_status") == "failed"
    }
    successors = {
        key: call
        for key, call in calls.items()
        if call.get("retry_of") in failed
        and 0 < call.get("retry", 0) <= state.get("limits", {}).get("call_retries", 0)
        and call.get("request") == failed[call["retry_of"]].get("request")
    }
    successful = [
        key
        for key in successors
        if not receipts.get(key, {}).get("error")
        and (receipts.get(key, {}).get("payload") or {}).get("source_status") == "complete"
        and bool((receipts.get(key, {}).get("payload") or {}).get("observations"))
    ]
    exercised, recovered = bool(failed), bool(successful)
    return {
        "kind": "recovery",
        "rule_id": rule,
        "fault_exercised": exercised,
        "failed_retrieval": bool(failed),
        "bounded_successor": bool(successors),
        "successful_retrieval": bool(successful),
        "recovered": recovered,
        "status": "recovered" if recovered else "failed" if exercised else "not_exercised",
        "diagnostic_correct": verdict == "correct",
        "success": recovered and verdict == "correct",
    }


def grade(result, expected, events=()):
    state = result.get("state", {})
    evidence = state.get("evidence", {})
    # A receipt may justify a retrieval gap, never an application-cause claim.
    cutoff = state.get("cutoff")
    for call in state.get("calls", {}).values():
        for message in call.get("request", {}).get("messages", []):
            try:
                context = json.loads(message.get("content", ""))
            except (ValueError, TypeError):
                continue
            if isinstance(context, dict) and context.get("alert", {}).get("cutoff"):
                cutoff = context["alert"]["cutoff"]
                break

    def before_cutoff(row):
        if not cutoff:
            return True
        return all(
            datetime.fromisoformat(row[k].replace("Z", "+00:00"))
            <= datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
            for k in ("event_time", "available_at")
            if row.get(k)
        )

    digest = result.get("dataset_digest")

    def same_dataset(row):
        return not digest or not row.get("dataset_digest") or row["dataset_digest"] == digest

    valid = {k for k, row in evidence.items() if before_cutoff(row) and same_dataset(row)}
    receipts = {
        k
        for k, receipt in state.get("tool_receipts", {}).items()
        if same_dataset(receipt.get("page") or {})
        and before_cutoff({"event_time": (receipt.get("page") or {}).get("cutoff")})
    }
    valid_refs = valid | receipts
    key_ids = {
        k for k in valid if any(matches(evidence[k], m) for m in expected["key_evidence_matchers"])
    }
    hypotheses = result.get("hypotheses") or []
    report = result.get("report") or {}
    top = hypotheses[0] if hypotheses else {}
    statement = top.get("statement", "").lower()
    service = bool(re.search(r"\b" + re.escape(expected["culprit_service"]) + r"\b", statement))
    mechanism = bool(statement) and all(
        re.search(pattern, statement, re.I) for pattern in expected["mechanism_keywords"]
    )
    key_cited = bool(set(top.get("supporting_evidence", [])) & key_ids)
    cited = set(top.get("supporting_evidence", [])) & valid
    required = all(
        any(any(matches(evidence[key], matcher) for matcher in group) for key in cited)
        for group in expected.get("required_evidence_groups", [])
    )
    support_valid = set(top.get("supporting_evidence", [])) <= valid_refs
    if expected.get("expected_diagnosis") == "inconclusive":
        text = json.dumps({"hypotheses": hypotheses, "report": report}).lower()
        justified = (
            result.get("diagnosis") == "inconclusive"
            and result.get("status") == "completed"
            and service
            and required
            and support_valid
            and bool(cited)
            and all(re.search(pattern, text, re.I) for pattern in expected["uncertainty_keywords"])
            and not any(re.search(pattern, text, re.I) for pattern in expected["forbidden_claims"])
        )
    else:
        justified = False
    if not report or not report.get("summary", "").strip():
        verdict = "no_report"
    elif expected.get("expected_diagnosis") == "inconclusive":
        verdict = "correct" if justified else "wrong"
    elif not hypotheses or result.get("diagnosis") == "inconclusive":
        verdict = "inconclusive"
    elif service and mechanism and key_cited and required and support_valid:
        verdict = "correct"
    elif service or key_cited:
        verdict = "partial"
    else:
        verdict = "wrong"
    refs = list(references({"hypotheses": hypotheses, "report": report}))
    claim_supported = [
        bool(set(h.get("supporting_evidence", [])) & valid)
        and set(h["supporting_evidence"]) <= valid_refs
        for h in hypotheses
    ]
    queries = [
        json.dumps({"request": c.get("request")}, sort_keys=True)
        for c in state.get("calls", {}).values()
        if c.get("kind") == "tool"
    ]
    usage = result.get("usage", {})
    totals = {
        field: measured_sum(u.get(field) for u in usage.values())
        for field in (
            "cost",
            "input_tokens",
            "output_tokens",
            "known_cost",
            "known_input_tokens",
            "known_output_tokens",
            "model_calls",
            "tool_calls",
        )
    }
    times = [e["at"] for e in events if isinstance(e.get("at"), int | float)]
    alternatives = []
    for decoy in expected["wrong_alternatives"]:
        ranked = [i for i, h in enumerate(hypotheses) if decoy in h.get("statement", "").lower()]
        alternatives.append(
            {
                "alternative": decoy,
                "ranked_first": 0 in ranked,
                "mentioned_in_report": decoy in json.dumps(report).lower(),
                "explicitly_ruled_out_in_report": bool(
                    re.search(
                        r"(?:not|unlikely|ruled out|no evidence for)\s+(?:\w+\s+){0,3}"
                        + re.escape(decoy),
                        report.get("summary", ""),
                        re.I,
                    )
                ),
                "ranked_down": any(i > 0 for i in ranked),
                "contradicted": any(
                    h.get("contradicting_evidence") for i, h in enumerate(hypotheses) if i in ranked
                ),
            }
        )
    return {
        "grader_version": VERSION,
        "expectation_version": expected["version"],
        "family": expected["family"],
        "run_id": result.get("run_id"),
        "graph": recorded_graph(result.get("graph")),
        "system": "single" if result.get("graph") == "single" else "multi",
        "case_id": result.get("case_id"),
        "suite_kind": "reliability"
        if expected.get("expected_diagnosis") or expected.get("reliability")
        else "primary",
        "verdict": verdict,
        "reliability": reliability_result(result, expected, verdict, justified),
        "diagnosis_checks": {
            "culprit_service": service,
            "mechanism": bool(mechanism),
            "key_evidence_cited": key_cited,
            "required_evidence_groups": required,
            "support_references_valid": support_valid,
            "justified_uncertainty": justified,
        },
        "grounding": {
            "hypothesis_claim_fraction": sum(claim_supported) / len(claim_supported)
            if claim_supported
            else None,
            "key_evidence_retrieved": bool(key_ids),
            "key_evidence_cited": key_cited,
            "key_evidence_ids": sorted(key_ids),
            "semantic_support": "human review required",
        },
        "citations": {
            "count": len(refs),
            "observation_fraction": sum(r in valid for r in refs) / len(refs) if refs else None,
            "unknown": sorted(set(refs) - valid - receipts),
            "receipt_only": sorted(set(refs) & receipts),
        },
        "alternatives": alternatives,
        "report_fields_present": {
            k: k in report
            for k in (
                "summary",
                "facts",
                "assumptions",
                "recommendations",
                "unresolved_objection_ids",
            )
        },
        "report_completeness": {
            k: bool(report.get(k))
            for k in (
                "summary",
                "facts",
                "assumptions",
                "recommendations",
                "unresolved_objection_ids",
            )
        },
        "process": {
            "repeated_queries": sum(v - 1 for v in Counter(queries).values()),
            "invalid_outputs": sum(
                e.get("event") == "validation" and e.get("outcome") == "rejected" for e in events
            ),
            "repaired_outputs": sum(
                e.get("event") == "validation" and e.get("outcome") in {"warning", "repaired"}
                for e in events
            ),
            "failed_roles": sorted(
                {t["role"] for t in state.get("tasks", {}).values() if t.get("status") == "failed"}
            ),
            "unresolved_objections": list(state.get("objections", {})),
            "stop_reason": result.get("stop_reason"),
            "budget_exhausted": result.get("status") == "resource_exhausted",
            "execution_status": result.get("status"),
        },
        "resources": {
            "per_role": usage,
            "totals": totals,
            "wall_seconds": max(times) - min(times) if len(times) > 1 else None,
            "latency_note": "trace span; per-role latency is summed call duration",
        },
        "models": result.get("models", {}),
    }


def grade_run(directory: Path, expected=None, *, output=None):
    directory = Path(directory)
    result_path = directory / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
    if expected is None:
        pointer = directory / "expectation.json"
        if pointer.exists():
            expected = expectation(json.loads(pointer.read_text(encoding="utf-8"))["family"])
        elif result.get("case_id") == "dev-00":
            legacy = json.loads((EXPECTED / "dev-00.json").read_text(encoding="utf-8"))
            expected = expectation(legacy["root_cause"])
        else:
            raise ValueError(f"{directory}: expectation.json or explicit expectation required")
    trace = directory / "trace.jsonl"
    events = (
        [
            json.loads(line)
            for line in trace.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if trace.exists()
        else []
    )
    graded = grade(result, expected, events)
    graded["graded_at"] = time.time()
    out = Path(output or directory)
    out.mkdir(parents=True, exist_ok=True)
    (out / "grade.json").write_text(json.dumps(graded, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Human rubric worksheet",
        "",
        f"Automatic verdict: **{graded['verdict']}** ({VERSION}).",
        "",
        "Lexical checks and valid citations do not prove semantic support. Complete each dimension.",
        "",
        "| Dimension | pass / partial / fail | Reason |",
        "| --- | --- | --- |",
    ]
    lines += [
        f"| {d} | ___ | ___ |"
        for d in ("Diagnosis", "Support", "Contradictions", "Uncertainty", "Actions")
    ]
    lines += ["", "## Auto-filled facts", "", "```json", json.dumps(graded, indent=2), "```", ""]
    (out / "grade.md").write_text("\n".join(lines), encoding="utf-8")
    return graded
