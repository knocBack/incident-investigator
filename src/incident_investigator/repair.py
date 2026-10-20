"""Repair harmless model deviations before strict validation; never invent evidence.

Every repair is recorded as `(field path, rule)` so the runtime can trace and count it.
Only structurally unusable output is rejected, with an error naming the field and rule.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

from annotated_types import MaxLen
from pydantic import BaseModel

from incident_investigator.contracts import PAYLOADS, EvidenceRef
from incident_investigator.tools.contracts import QUERY_MODELS

# Near-miss enum spellings seen from models, mapped onto canonical (case-folded) values.
SYNONYMS = {
    "warning": "warn",
    "err": "error",
    "critical": "fatal",
    "moderate": "medium",
    "med": "medium",
    "receipt": "tool_receipt",
    "toolreceipt": "tool_receipt",
    "evidence": "observation",
    "likely": "likely_cause",
    "probable_cause": "likely_cause",
    "root_cause": "likely_cause",
    "config": "configuration",
    "deploy": "deployment",
    "final_result": "final",
    "result": "final",
    "tool": "tools",
    "tool_calls": "tools",
}
# Argument names models commonly use for an existing field.
ALIASES = {
    "metric": "metric_name",
    "name": "metric_name",
    "level": "severity",
    "text": "text_contains",
    "contains": "text_contains",
    "query": "text_contains",
    "kind": "change_kind",
    "from": "start",
    "since": "start",
    "to": "end",
    "until": "end",
}
_MISSING = object()


@dataclass
class Repairs:
    items: list[tuple[str, str]] = field(default_factory=list)

    def add(self, path: str, rule: str) -> None:
        self.items.append((path, rule))

    def __bool__(self) -> bool:
        return bool(self.items)


def coerce_literal(value: Any, choices: tuple, path: str, repairs: Repairs) -> Any:
    if value in choices or not isinstance(value, str):
        return value
    folded = value.strip().casefold().replace("-", "_").replace(" ", "_")
    folded = SYNONYMS.get(folded, folded)
    for choice in choices:
        if isinstance(choice, str) and choice.casefold() == folded:
            repairs.add(path, f"near-miss enum {value!r} coerced to {choice!r}")
            return choice
    return value


def _empty(annotation: Any) -> Any:
    """Explicit empty value for a required field the model omitted, if one is harmless."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, UnionType) and type(None) in args:
        return None
    if origin is list:
        return []
    if origin is dict:
        return {}
    return _MISSING


def conform_value(annotation: Any, value: Any, path: str, repairs: Repairs) -> Any:
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, UnionType):
        options = [arg for arg in args if arg is not type(None)]
        if value is None or len(options) != 1:
            return value
        return conform_value(options[0], value, path, repairs)
    if origin is Literal:
        return coerce_literal(value, args, path, repairs)
    if origin is list:
        if value is None:
            repairs.add(path, "null list replaced with []")
            return []
        if not isinstance(value, list):
            repairs.add(path, "single value wrapped in a list")
            value = [value]
        return [
            conform_value(args[0], item, f"{path}[{index}]", repairs)
            for index, item in enumerate(value)
        ]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return conform(annotation, value, path, repairs)
    return value


def conform(model: type[BaseModel], value: Any, path: str, repairs: Repairs) -> Any:
    """Drop unknown fields, rename known aliases, default omitted nullable/list fields."""
    if model is EvidenceRef and isinstance(value, str):
        repairs.add(path, "bare evidence ID wrapped as a reference")
        return {"kind": "observation", "id": value}
    if not isinstance(value, dict):
        return value
    fields = model.model_fields
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in fields:
            result[key] = item
            continue
        target = ALIASES.get(key)
        if target in fields and target not in value and target not in result:
            repairs.add(f"{path}.{key}", f"field renamed to {target!r}")
            result[target] = item
        else:
            repairs.add(f"{path}.{key}", "unknown field dropped")
    for name, info in fields.items():
        if name not in result:
            if info.is_required() and (default := _empty(info.annotation)) is not _MISSING:
                result[name] = default
                repairs.add(f"{path}.{name}", f"missing field set to {default!r}")
            continue
        result[name] = conform_value(info.annotation, result[name], f"{path}.{name}", repairs)
        for rule in info.metadata:
            if (
                isinstance(rule, MaxLen)
                and isinstance(result[name], list)
                and len(result[name]) > rule.max_length
            ):
                result[name] = result[name][: rule.max_length]
                repairs.add(f"{path}.{name}", f"truncated to {rule.max_length} items")
    return result


_FENCE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


def _embedded_json(raw: Any, repairs: Repairs) -> Any:
    """Recover one JSON object wrapped in a markdown fence or surrounding prose."""
    if not isinstance(raw, str):
        return None
    fenced = _FENCE.search(raw)
    candidate = fenced.group(1) if fenced else raw[raw.find("{") : raw.rfind("}") + 1]
    try:
        value = json.loads(candidate)
    except ValueError:
        return None
    if isinstance(value, dict):
        repairs.add("action", "JSON object extracted from markdown fence or surrounding text")
        return value
    return None


def repair_action(
    role: str, action: Any, allowed_tools: set[str], repairs: Repairs, *, final_only: bool
) -> dict[str, Any]:
    """Normalize the action envelope, tool requests and payload shape for one role."""
    if isinstance(action, dict) and action.get("kind") == "invalid":
        action = _embedded_json(action.get("raw_output"), repairs)
    if not isinstance(action, dict):
        raise ValueError("action: response was not a JSON object; return a tagged JSON action")
    if set(action) == {"action"} and isinstance(action["action"], dict):
        repairs.add("action", "redundant action wrapper removed")
        action = action["action"]
    action = dict(action)
    kind = coerce_literal(action.get("kind"), ("tools", "final"), "action.kind", repairs)
    if kind not in ("tools", "final"):
        payload_fields = set(PAYLOADS[role].model_fields)
        if "requests" in action and "result" not in action:
            kind = "tools"
        elif "result" in action:
            kind = "final"
        elif payload_fields & set(action):
            action = {"result": {k: v for k, v in action.items() if k != "kind"}}
            kind = "final"
        else:
            raise ValueError(
                f"action.kind: must be 'tools' or 'final' (got {action.get('kind')!r})"
            )
        repairs.add("action.kind", f"missing or unknown kind set to {kind!r}")
    if kind == "final":
        for key in set(action) - {"kind", "result"}:
            repairs.add(f"action.{key}", "unknown field dropped")
        result = action.get("result")
        fields = set(PAYLOADS[role].model_fields)
        if not isinstance(result, dict) or not fields & {ALIASES.get(k, k) for k in result}:
            raise ValueError(
                f"action.result: no usable content; expected an object with {sorted(fields)}"
            )
        result = conform(PAYLOADS[role], result, "action.result", repairs)
        return {"kind": "final", "result": result}
    if not allowed_tools:
        raise ValueError(f"action.kind: role {role} cannot retrieve; return kind 'final'")
    if final_only:
        raise ValueError(
            "action.kind: this is the final permitted turn; return kind 'final' with findings"
        )
    requests = action.get("requests")
    if isinstance(requests, dict):
        repairs.add("action.requests", "single request wrapped in a list")
        requests = [requests]
    if not isinstance(requests, list):
        raise ValueError("action.requests: must be a list of {name, arguments} tool requests")
    kept, reasons = [], []
    for index, request in enumerate(requests):
        path = f"action.requests[{index}]"
        if not isinstance(request, dict):
            reasons.append(f"{path}: must be an object with name and arguments")
            continue
        name = request.get("name")
        if isinstance(name, str):
            name = name.strip().casefold()
            if name not in allowed_tools and name + "s" in allowed_tools:
                repairs.add(f"{path}.name", f"tool name {name!r} corrected to {name + 's'!r}")
                name += "s"
        if name not in allowed_tools:
            reasons.append(
                f"{path}.name: {request.get('name')!r} is not one of {sorted(allowed_tools)}"
            )
            continue
        arguments = request.get("arguments") or {}
        if not isinstance(arguments, dict):
            reasons.append(f"{path}.arguments: must be an object")
            continue
        arguments = conform(QUERY_MODELS[name], arguments, f"{path}.arguments", repairs)
        kept.append({"name": name, "arguments": arguments})
    if not kept:
        raise ValueError("; ".join(reasons) or "action.requests: no usable tool request")
    for reason in reasons:
        repairs.add(
            reason.split(":", 1)[0], "unusable request dropped: " + reason.split(": ", 1)[1]
        )
    if len(kept) > 5:
        repairs.add("action.requests", "truncated to 5 requests")
        kept = kept[:5]
    return {"kind": "tools", "requests": kept}


@dataclass(frozen=True)
class Scope:
    """Runtime facts a semantic repair may rely on; nothing comes from the model."""

    observations: frozenset[str]
    receipts: frozenset[str]
    revision: str | None
    hypothesis_ids: frozenset[str]
    objection_ids: tuple[str, ...]
    alert_service: str


def _references(items: Any, scope: Scope, path: str, repairs: Repairs) -> list:
    kept = []
    for index, ref in enumerate(items if isinstance(items, list) else []):
        key = ref.get("id") if isinstance(ref, dict) else None
        kind = (
            "observation"
            if key in scope.observations
            else "tool_receipt"
            if key in scope.receipts
            else None
        )
        if kind is None:
            repairs.add(f"{path}[{index}].id", f"unknown evidence ID {key!r} dropped")
            continue
        if ref.get("kind") != kind:
            repairs.add(f"{path}[{index}].kind", f"reference kind corrected to {kind!r}")
        kept.append({"kind": kind, "id": key})
    return kept


def _ids(items: Any, scope: Scope, path: str, repairs: Repairs) -> list:
    known = scope.observations | scope.receipts
    kept = []
    for index, key in enumerate(items if isinstance(items, list) else []):
        if key in known:
            kept.append(key)
        else:
            repairs.add(f"{path}[{index}]", f"unknown evidence ID {key!r} dropped")
    return kept


def _grounded(findings: Any, scope: Scope, path: str, repairs: Repairs) -> tuple[list, list]:
    """Keep each claim whose valid support remains; drop facts left without references."""
    kept, dropped = [], []
    for index, finding in enumerate(findings if isinstance(findings, list) else []):
        if not isinstance(finding, dict):
            kept.append(finding)
            continue
        here = f"{path}[{index}]"
        cited = finding.get("references")
        references = _references(cited, scope, f"{here}.references", repairs)
        if finding.get("kind") == "fact" and not references:
            repairs.add(here, "fact dropped: no valid evidence reference remains")
            dropped.append(finding)
            continue
        kept.append({**finding, "references": references})
    return kept, dropped


DEFAULT_QUESTIONS = {
    "observability": "Which logs, metrics and request traces show what changed for {service} "
    "and its downstream services during the alert window, compared with the baseline?",
    "dependency": "Which downstream dependencies of {service}, and which recent deployments or "
    "configuration changes along that path, line up with the alert?",
}


def repair_plan(result: dict[str, Any], scope: Scope, repairs: Repairs) -> dict[str, Any]:
    """An initial plan always gathers with both specialists and carries no objection IDs."""
    path = "action.result"
    if result.get("route") != "gather":
        repairs.add(f"{path}.route", "initial plan route set to 'gather'")
    if result.get("dispositions"):
        repairs.add(f"{path}.dispositions", "initial plan dispositions dropped")
    assignments = []
    for index, item in enumerate(result.get("assignments") or []):
        if not isinstance(item, dict):
            continue
        if item.get("objection_id") is not None:
            repairs.add(
                f"{path}.assignments[{index}].objection_id",
                f"initial assignment objection ID {item['objection_id']!r} set to null",
            )
        assignments.append({**item, "objection_id": None})
    if not any(item.get("role") in DEFAULT_QUESTIONS for item in assignments):
        raise ValueError(
            f"{path}.assignments: initial plan needs at least one observability or "
            "dependency assignment with a question"
        )
    for role in ("observability", "dependency"):
        if not any(item.get("role") == role for item in assignments):
            question = DEFAULT_QUESTIONS[role].format(service=scope.alert_service)
            assignments.append({"role": role, "question": question, "objection_id": None})
            repairs.add(f"{path}.assignments", f"missing {role} assignment added")
    if len(assignments) > 4:
        # Keep the first assignment per role, then fill remaining slots in order.
        first = {
            id(next(a for a in assignments if a["role"] == role))
            for role in ("observability", "dependency")
        }
        assignments = [a for a in assignments if id(a) in first] + [
            a for a in assignments if id(a) not in first
        ][:2]
        repairs.add(f"{path}.assignments", "truncated to 4 assignments")
    return {"route": "gather", "assignments": assignments, "dispositions": []}


def repair_decision(
    result: dict[str, Any], scope: Scope, repairs: Repairs
) -> tuple[dict[str, Any], bool]:
    """Normalize a structurally valid decision; returns whether any objection was reopened."""
    path = "action.result"
    known = set(scope.objection_ids)
    dispositions: dict[str, dict] = {}
    reopened = False
    for index, item in enumerate(result["dispositions"]):
        key = item["objection_id"]
        if key not in known or key in dispositions:
            repairs.add(
                f"{path}.dispositions[{index}].objection_id",
                f"unknown or duplicate objection ID {key!r} dropped",
            )
            continue
        dispositions[key] = dict(item)
    for key in scope.objection_ids:
        if key not in dispositions:
            dispositions[key] = {
                "objection_id": key,
                "outcome": "unresolved",
                "reason": "not disposed by orchestrator",
            }
            repairs.add(f"{path}.dispositions", f"missing disposition for {key} set unresolved")
            reopened = True
    assignments = []
    for index, item in enumerate(result["assignments"]):
        key = item["objection_id"]
        if (
            result["route"] != "gather"
            or key not in dispositions
            or dispositions[key]["outcome"] != "gather"
        ):
            repairs.add(
                f"{path}.assignments[{index}].objection_id",
                f"assignment for unrouteable or unknown objection {key!r} dropped",
            )
            continue
        assignments.append(item)
    route = result["route"]
    if route == "gather" and not assignments:
        route = "report"
        repairs.add(f"{path}.route", "gather without actionable assignments changed to 'report'")
    for key, item in dispositions.items():
        routed = (
            (
                item["outcome"] == "gather"
                and route == "gather"
                and any(a["objection_id"] == key for a in assignments)
            )
            or (item["outcome"] == "reanalyze" and route == "reanalyze")
            or item["outcome"] == "unresolved"
        )
        if not routed:
            dispositions[key] = {
                "objection_id": key,
                "outcome": "unresolved",
                "reason": "no matching follow-up routed by orchestrator",
            }
            repairs.add(f"{path}.dispositions", f"unrouted objection {key} set unresolved")
            reopened = True
    return (
        {"route": route, "assignments": assignments, "dispositions": list(dispositions.values())},
        reopened,
    )


def repair_result(
    role: str, stage: str, result: Any, scope: Scope, repairs: Repairs
) -> dict[str, Any]:
    """Semantic repairs for evidence references and runtime-owned identities."""
    if not isinstance(result, dict):
        raise ValueError("action.result: must be an object with the role's result fields")
    path = "action.result"
    result = dict(result)
    if role == "single":
        report = repair_result("reporting", stage, result.pop("report", {}), scope, repairs)
        result = repair_result("reasoning", stage, result, scope, repairs)
        return {**result, "report": report}
    if role == "orchestrator" and stage == "plan":
        return repair_plan(result, scope, repairs)
    if role in {"observability", "dependency"}:
        findings, dropped = _grounded(result.get("findings"), scope, f"{path}.findings", repairs)
        if dropped and not findings and not result.get("gaps"):
            cited = [ref for finding in dropped for ref in finding.get("references") or []]
            raise ValueError(
                f"{path}.findings: every fact cites unknown evidence {cited!r:.200}; cite "
                "evidence_id or tool_receipt_id values from tool results, or report gaps"
            )
        result["findings"] = findings
    elif role == "reasoning":
        hypotheses = []
        for index, item in enumerate(result.get("hypotheses") or []):
            if isinstance(item, dict):
                here = f"{path}.hypotheses[{index}]"
                item = {
                    **item,
                    "supporting_evidence": _ids(
                        item.get("supporting_evidence"),
                        scope,
                        f"{here}.supporting_evidence",
                        repairs,
                    ),
                    "contradicting_evidence": _ids(
                        item.get("contradicting_evidence"),
                        scope,
                        f"{here}.contradicting_evidence",
                        repairs,
                    ),
                }
            hypotheses.append(item)
        result["hypotheses"] = hypotheses
        top = hypotheses[0] if hypotheses and isinstance(hypotheses[0], dict) else {}
        if result.get("conclusion") == "likely_cause" and not (
            set(top.get("supporting_evidence", [])) & scope.observations
        ):
            result["conclusion"] = "inconclusive"
            repairs.add(
                f"{path}.conclusion",
                "likely_cause without an observation supporting the top hypothesis "
                "set to 'inconclusive'",
            )
    elif role in {"critic", "reporting"}:
        if result.get("revision_id") != scope.revision:
            repairs.add(f"{path}.revision_id", "set to the current hypothesis revision")
            result["revision_id"] = scope.revision
        if role == "critic":
            objections = []
            for index, item in enumerate(result.get("objections") or []):
                if isinstance(item, dict):
                    here = f"{path}.objections[{index}]"
                    item = {
                        **item,
                        "evidence_ids": _ids(
                            item.get("evidence_ids"), scope, f"{here}.evidence_ids", repairs
                        ),
                    }
                    if item.get("hypothesis_id") not in scope.hypothesis_ids | {None}:
                        repairs.add(
                            f"{here}.hypothesis_id",
                            f"unknown hypothesis {item['hypothesis_id']!r} set to null",
                        )
                        item["hypothesis_id"] = None
                objections.append(item)
            result["objections"] = objections
        else:
            facts, assumptions = [], list(result.get("assumptions") or [])
            for index, item in enumerate(result.get("facts") or []):
                if isinstance(item, dict) and item.get("kind") != "fact":
                    repairs.add(
                        f"{path}.facts[{index}].kind",
                        f"{item.get('kind')!r} statement moved to assumptions",
                    )
                    assumptions.append(item.get("statement", ""))
                    continue
                facts.append(item)
            result["facts"], _ = _grounded(facts, scope, f"{path}.facts", repairs)
            result["assumptions"] = assumptions
            recommendations = []
            for index, item in enumerate(result.get("recommendations") or []):
                if isinstance(item, dict):
                    item = {
                        **item,
                        "evidence_ids": _ids(
                            item.get("evidence_ids"),
                            scope,
                            f"{path}.recommendations[{index}].evidence_ids",
                            repairs,
                        ),
                    }
                recommendations.append(item)
            result["recommendations"] = recommendations
            if set(result.get("unresolved_objection_ids") or []) != set(scope.objection_ids):
                repairs.add(
                    f"{path}.unresolved_objection_ids", "set to every recorded objection ID"
                )
            result["unresolved_objection_ids"] = list(scope.objection_ids)
    return result
