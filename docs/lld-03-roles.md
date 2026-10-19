# LLD-03: demo role and coordination contracts

M3 implements the B7/B8 six-role loop. Recovery hardening and public resume remain
M4; baseline/evaluation remain M5. Existing accepted ownership and closure rules apply.
`contracts.py` supplies Pydantic schemas; unexpected fields are rejected.

Every model emits either `tools {requests: [{name, arguments}]}` or
`final {result: <role payload>}`. Only specialists may use retrieval tools. All
identities, attempts, counters and envelopes are runtime-owned. Each task has one
attempt (S3), bounded call retries, and one invalid-output correction turn.

| Role | Payload |
| --- | --- |
| Orchestrator plan/decide | assignments `{role, question, objection_id?}`, route `gather/reanalyze/report`, dispositions `{objection_id, outcome: gather/reanalyze/unresolved, reason}` |
| Observability / Dependency | findings `{statement, kind: fact/interpretation/assumption, references: [{kind: observation/tool_receipt, id}]}`, gaps, scalar outputs |
| Reasoning | ordered hypotheses `{hypothesis_id, statement, confidence: low/medium/high, supporting_evidence, contradicting_evidence, assumptions}`, conclusion `likely_cause/inconclusive`, explanation |
| Critic | exact `revision_id`, objections `{objection_id, hypothesis_id?, question, evidence_ids, suggested_role?}`, summary |
| Reporting | exact reviewed `revision_id`, summary, cited facts, assumptions, recommendations `{action, verification, evidence_ids}`, unresolved objection IDs |

Nullable fields are required, with JSON null when absent. Empty evidence arrays
are explicit; absence of contradictions is not proof of causality. Facts require
references. Runtime checks all references against task-visible retrieved records;
receipts may establish gaps but never application causes. Reporting cannot change
the ranked hypotheses or perform retrieval. Recommendations are proposals, not
claims that a fix was applied.

The controller owns typed control state. Parallel role/call nodes return immutable
receipts only; identity-based reducers reject conflicting replay. Each Send executes
one model/tool operation. Synchronous per-run SQLite checkpoints separate admission,
raw receipt storage, and validation/acceptance. Inspect reads the derived result
without invoking models or opening a writable checkpointer. Trace is a local audit
export, not recovery authority. No public resume is claimed in M3.

Initial plans must include both specialist roles. The whole specialist batch settles
before Reasoning, including failed/inconclusive tasks. A reasoning task ID identifies
its revision; Critic must review that exact revision. Every objection receives an
Orchestrator disposition. Runtime repairs structurally valid decisions: unknown or
duplicate objection references are dropped with validation warnings; omitted
objections receive an explicit unresolved disposition ("not disposed by orchestrator").
Inconsistent routes fall back to unresolved reporting. Gathering requires a targeted assignment carrying the
objection ID; reanalysis consumes a new round and must receive another Critic review.
An unresolved disposition remains visible at closure. Gathering/reanalysis dispositions
remain open until a later reviewed decision explicitly leaves them unresolved; they
are never silently treated as resolved.

Loop profile: max_review_rounds **3**, max_no_progress_rounds **2**, model calls
40/run and 10/task, tools 60/run and 15/task, wall time 240s/run, 30s/call, one call
retry, one correction turn, at most five tools/action and four assignments/plan.
These are demo execution bounds, not provider billing guarantees. No task retry.
Progress means new retrieved observation IDs or a first complete-empty query page; changed prose, failed pages and duplicate evidence do not reset
no-progress. Update once per reviewed round. At review/no-progress closure, retain
all open objections explicitly and run Reporting if capacity permits. A hard call,
task or wall bound produces resource_exhausted with a partial summary. After bounded
correction/retries, failed planning, reasoning, decide or Critic steps close the loop
and run Reporting with partial completeness and unresolved objections if capacity
permits. Reporting may use a null revision only when no hypothesis was accepted.
Only Reporting failure or hard execution limits may end without a Reporting result.

Artifacts: `state.sqlite`, `trace.jsonl`, `result.json`, `report.md`, local per UUID
run directory. JSON retains tasks, receipts, revisions, objections, dispositions,
evidence, completeness, stop reason and all six roles' measured usage (unknown token
components remain unknown). Runtime never reads docs or evaluator material.
