"""Validated role payloads and the finite demo execution profile."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

Role = Literal[
    "orchestrator", "observability", "dependency", "reasoning", "critic", "reporting", "single"
]
Specialist = Literal["observability", "dependency"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Limits(Record):
    max_model_calls: int = Field(default=40, ge=1, le=200)
    max_tool_calls: int = Field(default=60, ge=1, le=300)
    wall_seconds: float = Field(default=240, gt=0, le=3600, allow_inf_nan=False)
    call_seconds: float = Field(default=30, gt=0, le=600, allow_inf_nan=False)
    task_model_calls: int = Field(default=10, ge=1, le=40)
    task_tool_calls: int = Field(default=15, ge=1, le=60)
    call_retries: int = Field(default=1, ge=0, le=2)
    max_review_rounds: int = Field(default=3, ge=1, le=3)
    max_no_progress_rounds: int = Field(default=2, ge=1, le=2)


# Run profiles own every execution bound, including per-call and wall timeouts.
PROFILES = {
    "demo": Limits(),
    # Live providers via OpenRouter/Codex take 15-35 s per call; the wall fits one full
    # review round (plan, eight specialist turns, reasoning, critic, decide, report) at 90 s.
    "demo-live": Limits(call_seconds=90, wall_seconds=1200, task_model_calls=8),
}


class Assignment(Record):
    role: Specialist
    question: str = Field(min_length=1, max_length=2000)
    objection_id: str | None


class Disposition(Record):
    objection_id: str
    outcome: Literal["gather", "reanalyze", "unresolved"]
    reason: str = Field(min_length=1)


class Plan(Record):
    assignments: list[Assignment] = Field(max_length=4)
    route: Literal["gather", "reanalyze", "report"]
    dispositions: list[Disposition]


class EvidenceRef(Record):
    kind: Literal["observation", "tool_receipt"]
    id: str


class Finding(Record):
    statement: str
    kind: Literal["fact", "interpretation", "assumption"]
    references: list[EvidenceRef]

    @model_validator(mode="after")
    def grounded_fact(self):
        if self.kind == "fact" and not self.references:
            raise ValueError("facts require retrieved references")
        return self


class SpecialistResult(Record):
    findings: list[Finding]
    gaps: list[str]
    outputs: dict[str, str | int | float | bool | None]


class Hypothesis(Record):
    hypothesis_id: str
    statement: str
    confidence: Literal["low", "medium", "high"]
    supporting_evidence: list[str]
    contradicting_evidence: list[str]
    assumptions: list[str]


class ReasoningResult(Record):
    hypotheses: list[Hypothesis] = Field(max_length=8)
    conclusion: Literal["likely_cause", "inconclusive"]
    explanation: str


class Objection(Record):
    objection_id: str
    hypothesis_id: str | None
    question: str
    evidence_ids: list[str]
    suggested_role: Specialist | None


class Review(Record):
    revision_id: str
    objections: list[Objection] = Field(max_length=6)
    summary: str


class Recommendation(Record):
    action: str
    verification: str
    evidence_ids: list[str]


class Report(Record):
    revision_id: str | None
    summary: str
    facts: list[Finding]
    assumptions: list[str]
    recommendations: list[Recommendation]
    unresolved_objection_ids: list[str]

    @model_validator(mode="after")
    def facts_only(self):
        if any(finding.kind != "fact" for finding in self.facts):
            raise ValueError("report facts must be labelled fact")
        return self


class ToolRequest(Record):
    name: Literal[
        "catalog",
        "logs",
        "log_counts",
        "trace_logs",
        "metrics",
        "metric_stats",
        "changes",
        "dependencies",
        "history",
    ]
    arguments: dict[str, JsonValue]


class SingleResult(ReasoningResult):
    report: Report


class ToolsAction(Record):
    kind: Literal["tools"]
    requests: list[ToolRequest] = Field(min_length=1, max_length=5)


# Each role's final action has a concrete result schema, including in provider JSON schema.
def action_type(payload: type[Record]):
    from pydantic import create_model

    final = create_model(
        payload.__name__ + "Action",
        __base__=Record,
        kind=(Literal["final"], ...),
        result=(payload, ...),
    )
    return Annotated[ToolsAction | final, Field(discriminator="kind")]


PAYLOADS = {
    "orchestrator": Plan,
    "observability": SpecialistResult,
    "dependency": SpecialistResult,
    "reasoning": ReasoningResult,
    "critic": Review,
    "reporting": Report,
    "single": SingleResult,
}
ALLOWED_TOOLS = {
    "observability": {
        "catalog",
        "logs",
        "log_counts",
        "trace_logs",
        "metrics",
        "metric_stats",
        "history",
    },
    # Dependency follows the call path, so it can check downstream health in aggregate.
    "dependency": {"catalog", "dependencies", "changes", "history", "log_counts", "metric_stats"},
}
ALLOWED_TOOLS["single"] = set().union(*ALLOWED_TOOLS.values())
