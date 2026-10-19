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
Orchestrator disposition.

Repair, don't reject: the runtime (`repair.py`) normalizes harmless deviations in every
role's output before strict validation and never invents evidence. It extracts a JSON
action wrapped in a markdown fence or prose, drops unknown fields
and renames common argument aliases, defaults omitted nullable/list fields, coerces
near-miss enums and action kinds, truncates over-long lists, drops tool requests a role may
not use or whose arguments are invalid, sets initial-plan objection IDs to null and adds a
missing specialist assignment, drops unknown or duplicate IDs and unknown citations (a claim
stays while valid support remains; a fact left without references is dropped), corrects
reference kinds, sets runtime-owned revision/objection IDs, moves non-fact report statements
to assumptions, and downgrades `likely_cause` without observation support to `inconclusive`.
Each repair is a trace `validation` event with outcome `repaired`, its `field` path and
`rule`; `result.json` counts repairs per role. Only structurally unusable output is rejected
(unparseable, wrong action kind, no usable content); the rejection names the field and rule
and receives the one correction turn. In decisions, omitted objections receive an explicit
unresolved disposition ("not disposed by orchestrator").
Inconsistent routes fall back to unresolved reporting. Gathering requires a targeted assignment carrying the
objection ID; reanalysis consumes a new round and must receive another Critic review.
An unresolved disposition remains visible at closure. Gathering/reanalysis dispositions
remain open until a later reviewed decision explicitly leaves them unresolved; they
are never silently treated as resolved.

Loop profile: max_review_rounds **3**, max_no_progress_rounds **2**, model calls
40/run and 10/task, tools 60/run and 15/task, wall time 240s/run, 30s/call, one call
retry, one correction turn, at most five tools/action and four assignments/plan.
The `demo-live` profile, used by `--model openrouter|codex`, sets 90s/call, 1200s/run
wall and 8 model calls/task; adapters take their per-call timeout from the profile.
These are demo execution bounds, not provider billing guarantees. No task retry.
Progress means new retrieved observation IDs or a first complete-empty query page; changed prose, failed pages and duplicate evidence do not reset
no-progress. Update once per reviewed round. At review/no-progress closure, retain
all open objections explicitly and run Reporting if capacity permits. A hard run call
or wall bound produces resource_exhausted with a partial summary; a spent task allowance
settles only that task as failed, and its batch continues. After bounded
correction/retries, failed planning, reasoning, decide or Critic steps close the loop
and run Reporting with partial completeness and unresolved objections if capacity
permits. Reporting may use a null revision only when no hypothesis was accepted.
Only Reporting failure or hard execution limits may end without a Reporting result.

Artifacts: `state.sqlite`, `trace.jsonl`, `result.json`, `report.md`, local per UUID
run directory. JSON retains tasks, receipts, revisions, objections, dispositions,
evidence, completeness, stop reason and all six roles' measured usage (unknown token
components remain unknown). Runtime never reads docs or evaluator material.

OpenRouter is an optional chat-completions adapter selected with `--model openrouter`.
Resolved models are recorded per role; the credential is never part of state.
Prompts expose registry schemas and JSON action requests, with no native tools.
The public model catalogue selects schema output versus JSON mode. The runtime
owns bounded retries; provider authentication/credit errors are nonretryable and
rate limits are retryable. Receipts retain generation IDs, available tokens,
returned cost and latency. Per-role model maps are demo settings; comparison
benchmarks must use one model for both modes.

Specialist model requests expose the remaining task model- and tool-call allowance. The
final permitted task model call, or any call after the tool allowance is spent, receives a
final-only response schema; all evidence and result validation still applies, and invalid
output cannot exceed the saved limits. Identical normalized queries are answered from the
earlier receipt without a tool call ([tool contracts §9](tool-contracts.md)). If a provider
rejects the strict response schema (HTTP 400), the OpenRouter adapter switches that model to
JSON mode for the rest of the run.
