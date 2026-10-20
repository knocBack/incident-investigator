"""Storage-agnostic presentation helpers for the slim console."""

from __future__ import annotations

import html
import json
import re
from collections import Counter
from contextlib import suppress
from datetime import datetime
from decimal import InvalidOperation

from markdown_it import MarkdownIt
from markupsafe import Markup

from incident_investigator.grading.suite import _pass_metrics, reliability_metrics
from incident_investigator.graph_config import recorded_system
from incident_investigator.workspace import total


def pretty(value):
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def failure_label(reason):
    if "output_truncated" in reason:
        return "output truncated"
    if match := re.search(r"HTTP (\d{3})", reason):
        return f"provider HTTP {match[1]}"
    if "timeout" in reason.lower():
        return "request timed out"
    return reason[:160] or "execution failed"


def failure_explanation(reason, call_seconds=None):
    if "output_truncated" in reason:
        return "The model hit its output-token limit before finishing the report. Try another model or a larger output limit where supported."
    if "timeout" in reason.lower():
        limit = f" ({call_seconds:g} s per call)" if isinstance(call_seconds, int | float) else ""
        return f"The request timed out{limit}. Try another model or raise Per-call timeout within the run wall limit."
    return ""


def check_reasons(result, grade):
    reasons = {
        "culprit_service": "The graded top hypothesis did not name the expected culprit service.",
        "mechanism": "The top hypothesis did not describe all required parts of the expected failure mechanism.",
        "key_evidence_cited": "The top hypothesis did not cite a retrieved observation matching the key evidence.",
        "required_evidence_groups": "The top hypothesis did not cite observations from every required evidence group.",
        "support_references_valid": "A supporting reference is missing, belongs to another dataset, or falls after the cutoff.",
        "justified_uncertainty": "The report did not meet all requirements for justified uncertainty, including supporting observations, explicit gaps and follow-up evidence.",
    }
    if result.get("diagnosis") == "inconclusive":
        reasons["culprit_service"] = (
            "The final diagnosis is inconclusive; separately, the graded top hypothesis did not match the expected culprit service."
        )
    if not result.get("hypotheses"):
        for key, subject in (
            ("culprit_service", "culprit-service check"),
            ("mechanism", "mechanism check"),
            ("key_evidence_cited", "key-evidence check"),
            ("required_evidence_groups", "required evidence-group check"),
        ):
            reasons[key] = f"No ranked hypothesis was available for the {subject}."
    return {
        key: reasons.get(
            key, "The saved evaluator check did not pass; inspect the full grade below."
        )
        for key, passed in grade.get("diagnosis_checks", {}).items()
        if not passed
    }


def grade_summary(result, grade):
    verdict = grade.get("verdict", "ungraded")
    if verdict == "ungraded":
        return "Not graded yet."
    if verdict == "correct":
        return "Counted correct: the saved grade passed its required diagnosis and evidence checks."
    checks = grade.get("diagnosis_checks", {})
    prefix = "No final report was completed. " if verdict == "no_report" else ""
    if checks.get("culprit_service") and checks.get("mechanism"):
        if checks.get("key_evidence_cited") is False:
            return (
                prefix
                + "Cause named, but key evidence was not cited, so this attempt is not counted correct."
            )
        if (
            checks.get("required_evidence_groups") is False
            or checks.get("support_references_valid") is False
        ):
            return (
                prefix
                + "Cause named, but required supporting evidence is missing or references are invalid; not counted correct."
            )
    if prefix:
        return prefix + "Retained hypothesis checks do not replace a completed final report."
    if checks.get("culprit_service") is False or checks.get("mechanism") is False:
        return "The retained hypothesis did not pass the cause and mechanism checks; this attempt is not counted correct."
    if result.get("diagnosis") == "inconclusive":
        return "No final cause was committed; the saved grade did not count the uncertainty as a correct diagnosis."
    return f"Saved verdict: {verdict}. The checks below explain why this attempt was not counted correct."


def present_run(run, store, suite_prefix=None):
    """Explain persisted outcomes without changing execution status or grading."""
    if not run.get("prefix"):
        return {
            **run,
            "outcome": "Retained grade only; investigation artifact unavailable.",
            "failure_explanation": "",
            "has_hypothesis": False,
            "free_no_charge": False,
            "progress": "unavailable",
            "model_call_errors": None,
            "provider_issue": "",
            "scripted_no_charge": False,
        }
    result = store.json(run["prefix"] + "/result.json")
    saved = store.json(run["prefix"] + "/run.json")
    state = result.get("state", {})
    events = store.lines(run["prefix"] + "/trace.jsonl")
    transitions = [e for e in events if e.get("event") == "state_transition"]
    latest = transitions[-1] if transitions else {}
    model_calls = max(run.get("model_calls") or 0, latest.get("model_calls", 0))
    tool_calls = max(run.get("tool_calls") or 0, latest.get("tool_calls", 0))
    # Older live traces lack admission counts; completed calls are a lower bound.
    model_calls = max(model_calls, sum(e.get("event") == "model_call" for e in events))
    tool_calls = max(tool_calls, sum(e.get("event") == "tool_call" for e in events))
    stage = state.get("stage") or latest.get("stage") or "starting"
    seconds = (state.get("limits") or saved.get("limits") or {}).get("call_seconds")
    reason = (
        result.get("stop_reason")
        or run.get("grade", {}).get("process", {}).get("stop_reason")
        or ""
    )
    errors = [
        e
        for e in events
        if e.get("event") == "model_call" and e.get("outcome") not in {None, "received", "ok"}
    ]
    if not errors:
        errors = [
            {
                "outcome": receipt["error"],
                "role": state.get("calls", {}).get(key, {}).get("role", "unknown"),
            }
            for key, receipt in result.get("receipts", {}).items()
            if receipt.get("error") and state.get("calls", {}).get(key, {}).get("kind") == "model"
        ]
    provider_errors = [
        e
        for e in errors
        if re.search(r"HTTP [45]\d\d|timeout", str(e.get("outcome", "")), re.IGNORECASE)
    ]
    provider_labels = list(dict.fromkeys(failure_label(str(e["outcome"])) for e in provider_errors))
    provider_roles = {
        e["role"] for e in provider_errors if e.get("role") and e["role"] != "unknown"
    }
    provider_issue = "; ".join(provider_labels)
    if len(provider_roles) > 1:
        provider_issue += f" on {len(provider_roles)} roles"
    task_errors = [
        f"{t.get('role', 'role')}: {failure_label(t['failure'])}"
        for t in state.get("tasks", {}).values()
        if t.get("failure")
    ]
    hypotheses = result.get("hypotheses", [])
    hypothesis_note = (
        "retained a high-confidence hypothesis"
        if any(h.get("confidence") == "high" for h in hypotheses)
        else "retained a ranked hypothesis"
        if hypotheses
        else "no ranked hypothesis"
    )
    active = run["status"] in {"running", "unfinished"}
    if active:
        outcome = f"{stage}; {model_calls} model / {tool_calls} tool calls admitted"
    else:
        failed = run["status"] in {"failed", "resource_exhausted", "interrupted"}
        outcome = (
            f"stopped during {stage}"
            if failed
            else (
                "partial report completed"
                if result.get("completeness") == "partial"
                else "report completed"
            )
        )
        if reason:
            outcome += "; " + failure_label(reason)
        elif task_errors:
            outcome += "; " + "; ".join(dict.fromkeys(task_errors))
        timeout_count = sum("timeout" in str(e.get("outcome", "")).lower() for e in errors)
        if timeout_count:
            outcome += f"; {timeout_count} timed-out model calls"
            if isinstance(seconds, int | float):
                outcome += f" at {seconds:g} s per call"
        elif errors and not reason and not task_errors:
            outcome += f"; {len(errors)} model-call errors retained"
        outcome += "; " + hypothesis_note
    explanation = failure_explanation(reason, seconds)
    if not active and run["status"] == "completed" and result.get("completeness") == "partial":
        context = (
            "An earlier role failed, then the reporting role produced a final partial report. "
            if task_errors
            else "A final partial report was delivered; its recorded closure reason appears above. "
        )
        explanation = (
            context
            + "Completed describes report delivery, not a proven diagnosis. Execution failures count runs that stopped without completing reporting. "
            + explanation
        )
    free = False
    if suite_prefix:
        from incident_investigator.grading.budget import amount, price_bound

        prices = store.json(suite_prefix + "/prices.json")
        ledger = store.json(run["prefix"] + "/budget.json")
        with suppress(KeyError, TypeError, ValueError, InvalidOperation):
            free = (
                bool(run.get("models"))
                and all(price_bound(prices[model]) == 0 for model in set(run["models"].values()))
                and bool(ledger)
                and all(
                    value is None or amount(value) == 0
                    for value in (run.get("cost"), run.get("known_cost"))
                )
                and all(
                    amount(ledger["state"][key]) == 0
                    for key in ("charged", "active", "unknown_reserve")
                )
                and all(
                    amount(c["reservation"]) == 0
                    and (c.get("cost") in {None, "unbilled_failure"} or amount(c["cost"]) == 0)
                    for c in ledger["calls"]
                )
            )
    return {
        **run,
        "outcome": outcome,
        "failure_explanation": explanation,
        "has_hypothesis": bool(hypotheses),
        "free_no_charge": free,
        "scripted_no_charge": run.get("model") == "scripted"
        and all(value is None or value == 0 for value in (run.get("cost"), run.get("known_cost"))),
        "model_call_errors": len(errors)
        if events or "receipts" in result or "calls" in state
        else None,
        "provider_issue": provider_issue,
        "progress": f"{stage} · {model_calls} model / {tool_calls} tool calls admitted",
    }


def comparison_outcome(metrics):
    multi, single = metrics.get("multi"), metrics.get("single")
    if len(metrics.get("systems", ())) == 1:
        return "Single-system run: no comparison."
    if not multi or not single:
        provisional = metrics.get(
            "provisional", any(row.get("provisional", True) for row in (multi, single) if row)
        )
        if provisional:
            return "Awaiting attempts from both systems."
        missing = []
        for system, row in (("multi", multi), ("single", single)):
            if row:
                continue
            reasons = list(
                dict.fromkeys(
                    item.get("reason", "skip reason unavailable")
                    for item in metrics.get("skipped", [])
                    if recorded_system(item.get("system"), item.get("graph")) == system
                )
            )
            reason = (
                "skipped: " + "; ".join(reasons)
                if reasons
                else "not selected for this suite"
                if metrics.get("systems") and system not in metrics["systems"]
                else "no skip reason was recorded"
            )
            missing.append(f"{system.title()} has no retained attempt; {reason}.")
        return " ".join(missing)
    if multi["provisional"] or single["provisional"]:
        return "Comparison is provisional while attempts or grades are pending."
    provider_issues = [
        f"{system}: {run['provider_issue']}"
        for system, row in (("multi", multi), ("single", single))
        for run in row["runs"]
        if run.get("provider_issue") and run.get("verdict") != "correct"
    ]
    if provider_issues:
        return (
            "Provider errors affected these attempts: "
            + "; ".join(dict.fromkeys(provider_issues))
            + ". This is not a fair comparison of diagnosis quality; recorded verdicts and progress remain below."
        )
    if multi["pass1"] != single["pass1"]:
        winner = "Multi" if multi["pass1"] > single["pass1"] else "Single"
        return f"{winner} achieved a higher correct-verdict rate on these retained attempts. Compare the outcomes below for causes and execution limits."
    text = (
        "Both systems achieved the same correct-verdict rate. "
        if multi["correct"]
        else "Neither system achieved a correct final verdict. "
    )
    mh = sum(r.get("has_hypothesis", False) for r in multi["runs"])
    sh = sum(r.get("has_hypothesis", False) for r in single["runs"])
    if bool(mh) != bool(sh):
        text += f"{'Multi' if mh else 'Single'} retained a hypothesis while the other retained none; this shows diagnostic progress, not a successful investigation."
    else:
        text += "The recorded outcomes below explain progress and failures; no diagnostic winner was established."
    return text


def suite_title(suite, runs):
    cases = suite.get("batch", {}).get("faults") or sorted({r["fault"] for r in runs})
    model = ", ".join(sorted({r["model"] for r in runs if r.get("model")})) or "awaiting model"
    time = min((r["created_at"] for r in runs if r.get("created")), default="time unavailable")
    with suppress(ValueError):
        time = datetime.fromisoformat(time).strftime("%Y-%m-%d %H:%M UTC")
    label = cases[0] if len(cases) == 1 else f"{len(cases)} incidents"
    return f"{label} · {model} · {time}"


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
                "free_no_charge": all(r.get("free_no_charge") for r in items),
                "scripted_no_charge": all(r.get("scripted_no_charge") for r in items),
                "runs": items,
                "n": n,
                "correct": correct,
                "grounding": sum(grounded) / len(grounded) if grounded else None,
                "grounding_measured": len(grounded),
                "execution_failures": _failures(items),
                "model_call_errors": total(items, "model_call_errors"),
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
        system = recorded_system(row.get("system"), row.get("graph"))
        suite_kind = row.get("suite_kind", "primary")
        outcomes = [
            r
            for r in runs
            if r["system"] == system
            and (r["case_set"] == "primary") == (suite_kind == "primary")
            and r["model"] == ", ".join(sorted(set(row.get("models", {}).values())))
        ]
        passes = [
            _pass_metrics(f["n"], f["correct"], min(k, f["n"]))
            for f in row.get("faults", {}).values()
            if f.get("n") and f.get("correct") is not None
        ]
        result.append(
            {
                **row,
                "system": system,
                "suite_kind": suite_kind,
                "models": row.get("models", {}),
                "outcomes": outcomes,
                "model_call_errors": total(outcomes, "model_call_errors"),
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
        "free_no_charge": bool(runs)
        and any(r.get("free_no_charge") for r in runs)
        and all(r.get("free_no_charge") or r.get("scripted_no_charge") for r in runs),
        "scripted_no_charge": bool(runs) and all(r.get("scripted_no_charge") for r in runs),
    }


def paired_metrics(metrics, *, provisional=None, skipped=(), systems=()):
    pairs = {}
    for row in metrics:
        key = (row["fault"], row["model"], row["case_set"])
        pair = pairs.setdefault(key, {"fault": key[0], "model": key[1], "case_set": key[2]})
        pair[row["system"]] = row
    for item in skipped:
        key = (
            item.get("fault", "unknown"),
            item.get("model", "unknown"),
            item.get("case_set", "primary"),
        )
        pairs.setdefault(key, {"fault": key[0], "model": key[1], "case_set": key[2]})
        pairs[key].setdefault("skipped", []).append(item)
    result = []
    for key in sorted(pairs):
        pair = pairs[key]
        if provisional is not None:
            pair["provisional"] = provisional
        pair["systems"] = systems
        result.append({**pair, "outcome": comparison_outcome(pair)})
    return result
