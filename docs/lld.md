# LLD-01: one complete task lifecycle

Status: LLD-01 specified on 2026-10-19 under the authorized development priority plan. [LLD-02](tool-contracts.md) specifies retrieval/snapshot/page/fault contracts and a concrete controlled-test profile. Both are design specifications, not implemented or integration-validated behavior. They preserve [HLD-01/02/03](hld.md). Dependency pins are adopted in `uv.lock` after resolution succeeded; integration validation remains pending. The [remaining-contracts handoff](#8-remaining-contracts-and-decision-gates) distinguishes unresolved decisions from unexecuted verification.

The first implementation target is one specialist task, using scripted model responses and LLD-02 retrieval adapters with the real LangGraph/SQLite integration. Controlled tool responses also exercise adverse/recovery checks. The task must gather evidence, return a validated result, and recover saved work. Completing that task does not establish a complete investigation or a final report.

## 1. Representation and ownership

Use a thin `TypedDict` for graph channels and Pydantic validation for boundary records. Persist JSON-compatible values, not provider SDK objects. Reject unexpected fields and invalid types; explicit runtime checks enforce references, ownership, transitions, and limits. Validation of a schema never proves an interpretation is correct.

```python
class GraphState(TypedDict):
    control: ControlState
    inbox: Annotated[dict[str, CallReceipt], merge_identical]
```

`ControlState` is a typed JSON record containing `revision: int`, `run: RunRecord`, and ID-keyed registries named `tasks`, `task_attempts`, `calls`, `observations`, `results`, and `events`, plus `processed_receipt_ids: list[str]` and `fault_state: dict[str, int]`. LLD-02 adds `query_coverage: dict[str, QueryCoverage]` for actual normalized-query retrieval coverage. Later coordination contracts add the HLD's identified rounds, routes, and progress records before implementing those behaviors; do not reconstruct them from prose.

- Exactly one serialized controller writes `control`. Its next complete value replaces the prior value; parallel call nodes cannot write it.
- Call nodes return only immutable `inbox` entries. `merge_identical` unions entries by receipt ID, treats an identical replay as a no-op, and raises an integrity error for different content under the same ID. It never mutates its arguments.
- The controller consumes each receipt once using `processed_receipt_ids`, validates it against its saved call, and updates evidence, accounting, task progress, and audit events in one control transition.
- The inbox remains retained for this bounded MVP. It is part of checkpoint state, not a second event database. Inspection/export can show saved but not yet processed receipts separately.

All runtime entity IDs are opaque UUID strings allocated by code and saved before use. `result_id` equals its logical `task_id`, because a task has at most one accepted terminal result. Revised interpretations require another task. Evidence identity is the SHA256 of the canonical JSON array `[dataset_digest, source, source_record_id]`; membership in this run's saved evidence is still required. Identical evidence may have the same identity in two isolated runs without being available to either run until retrieved.

Canonical JSON uses sorted object keys, compact separators, UTF-8, finite numbers, and preserved array order. All persisted timestamps are UTC RFC3339 strings; durations are nonnegative integer milliseconds. Record comparisons include their saved content, not a model's claimed identity. IDs created in an unsuccessful control transition have no authority and cannot be dispatched.

## 2. State records

These are the common envelopes and the first specialist payload. A nullable field must be present with `null`; list/map fields default to empty only where explicitly stated below. Configured sizes and numeric limits must be finite before execution; this document selects no production values.

| Record | Required fields and types |
| --- | --- |
| `RunRecord` | `run_id: str`, `mode: multi_agent\|single_agent`, `alert: Alert`, `versions: Versions`, `settings: object`, `limits: Limits`, `started_at: UTC`, `deadline_at: UTC`, `status: running\|interrupted\|completed\|resource_exhausted\|failed`, `stop_reason: Failure\|null`, `finished_at: UTC\|null`. Identity, alert, versions, settings, limits, start, and deadline are immutable. |
| `Task` | `task_id: str`, `role: Role`, `question: str`, `dependencies: list[Dependency]`, `result_contract: str`, `status: pending\|running\|succeeded\|failed\|skipped`, `deadline_at: UTC\|null`, `active_attempt_id: str\|null`, `result_id: str\|null`, `stop_reason: Failure\|null`. The first slice accepts only the Observability/Dependency specialist contract. Active attempt is set only while Running. |
| `TaskAttempt` | `attempt_id: str`, `task_id: str`, `number: int`, `input_revision: int`, `context: TaskContext`, `started_at: UTC`, `deadline_at: UTC`, `status: active\|succeeded\|failed\|cancelled`, `phase: needs_model\|waiting_tools\|candidate_result`, `model_call_ids: list[str]`, `pending_operation_ids: list[str]`, `visible_refs: list[EvidenceRef]`, `failure: Failure\|null`. Lists start empty except inherited visible references. |
| `CallAttempt` | `call_id: str`, `task_id: str`, `attempt_id: str`, `operation_id: str`, `retry_of: str\|null`, `invocation_id: str`, `kind: model\|tool`, `request: object`, `request_digest: str`, `fault_selection: FaultSelection\|null`, `admitted_at: UTC`, `deadline_at: UTC`, `status: admitted\|received\|unresolved\|not_dispatched`, `findings_eligible: bool`, `reserved_tokens: int`, `usage: Usage\|null`, `usage_receipt_id: str\|null`. Model requests save rendered messages, model/settings, and response/output bounds. Tool requests save tool name and validated arguments. |
| `CallReceipt` | `receipt_id: str`, `run_id: str`, `call_id: str`, `kind: response\|error\|not_dispatched\|replay_blocked\|late_response\|usage_update`, `recorded_at: UTC`, `dispatched_at: UTC\|null`, `duration_ms: int\|null`, `payload: object\|null`, `capture: complete\|truncated`, `failure: Failure\|null`, `usage: Usage\|null`. Normal response/error/non-dispatch uses ID `call_id + ":completion"`. Replay notice uses `call_id + ":blocked:" + current_invocation_id`; a collected late response/usage amendment gets `call_id + ":late:" + UUID`, allocated once and retained on reapplication. Earlier receipts remain immutable. |
| `Observation` | `evidence_id: str`, `dataset_digest: str`, `source: logs\|metrics\|changes\|dependencies\|history`, `source_record_id: str`, `event_time: UTC`, `available_at: UTC`, `data: object`. Source-specific payloads and metric windows follow in the tool contract. Query-to-observation relationships stay in tool receipts. |
| `TaskResult` | `result_id: str`, `task_id: str`, `attempt_id: str`, `input_revision: int`, `terminal_call_id: str`, `contract: specialist.v1`, `body: SpecialistResult`, `accepted_at: UTC`, `accepted_revision: int`. Code supplies the envelope; the model supplies only the candidate body. |

Supporting records:

- `Alert`: `case_id: str`, `affected_service: str`, `symptoms: list[str]`, `window_start: UTC`, `window_end: UTC`, `observation_cutoff: UTC`. Require start < end <= cutoff. The opaque case ID is not a diagnosis.
- `Versions`: digest strings named `dataset`, `prompts`, `tools`, `runtime`, `state`, and `dependencies`. Compare exact recorded values before graph/checkpointer setup on resume. The dependency identity covers the selected interpreter/package lock once pins exist.
- `Dependency`: `task_id: str`, `condition: settled|requires_input`, `input_name: str|null`. `requires_input` requires an accepted result and a non-null named value in its `body.outputs`; `settled` has `input_name=null`. Exact hypothesis/review references will extend this contract before those roles are implemented. Validate known IDs and acyclicity before admitting a batch.
- `TaskContext`: `alert: Alert`, `role: Role`, `question: str`, `prerequisite_result_ids: list[str]`, `evidence_refs: list[EvidenceRef]`, `unavailable: list[Failure]`. Code resolves these references to selected saved content when preparing a model request. The model receives no database handle, raw graph state, other run, planning document, or evaluator data.
- `EvidenceRef`: `kind: observation|tool_receipt`, `id: str`. Tool receipt references allow honest statements about complete-empty, partial, and failed retrieval without inventing an observation. They do not establish application health or a root cause.
- `FaultSelection`: `rule_id: str`, `counter_key: str`, `step: int`, `outcome: none|timeout|partial`, `keep_first: int|null`. Save alongside the source counter update; null for calls without a source fault schedule. LLD-02 defines rule-scoped counter keys and whole-record partial selection. This runtime-only field is not an agent-supplied tool argument.
- `Usage`: `input_tokens: int|null`, `output_tokens: int|null`, `provider_request_id: str|null`. Unknown fields remain null. A reservation is replaced only by reliable complete usage within the provider's validated accounting bound; partial usage keeps the full reserve held. Preserve partial measured values separately for reporting, without adding them to that same held reserve.
- `Failure`: `code: str`, `message: str`, `retryable: bool`. Runtime classifies errors; model text cannot grant a retry. Codes distinguish timeout, invalid output, disallowed action, missing input, resource limit, identity conflict, storage failure, and replay block.
- `Limits`: required integer fields `max_tasks`, `max_task_attempts`, `max_call_retries`, `max_model_calls`, `max_tool_calls`, `max_total_tokens`, `max_parallel_calls`, `max_tool_batch`, `max_context_bytes`, `max_receipt_bytes`, `max_result_bytes`, `run_timeout_ms`, `task_timeout_ms`, `call_timeout_ms`. Values are positive except retry count may be zero. Later coordination settings add round/no-progress limits before enabling their loop. Reject absent settings; controlled tests supply finite test-local values.

Task deadline starts null and is assigned at first dispatch as `min(run.deadline_at, now + task_timeout_ms)`. It is then immutable across task retries; each task attempt inherits it. Call deadline is bounded by task/run deadlines and `now + call_timeout_ms`. All survive interruption. Use a monotonic clock for local durations/timeouts, translating only the remaining persisted deadline into a local wait. Waiting for runner capacity consumes deadline time. Expired work cannot dispatch on resume.

`Role` is `orchestrator|observability|dependency|reasoning|critic|reporting|investigator`; the first slice admits only the two specialist roles. Run-wide task/model/tool/token limits include all attempts. Task-attempt count applies per logical task, call-retry count per operation, and tool-batch/byte limits per action or captured payload. Runner aggregate capacity is a separate finite setting passed through invocation context; a run's `max_parallel_calls` cannot override it.

## 3. Specialist actions and validation

A model response is exactly one tagged action:

```text
{"kind": "tools", "requests": [{"name": ToolName, "arguments": object}, ...]}
{"kind": "final", "result": SpecialistResult}

SpecialistResult = {
  "findings": [{"statement": str,
                "kind": "fact" | "interpretation" | "assumption",
                "references": [EvidenceRef, ...]}, ...],
  "gaps": [str, ...],
  "outputs": {str: JSONScalar}
}
```

`findings`, `gaps`, and `outputs` are required and may be empty. `JSONScalar` is a string, finite number, boolean, or null. Facts need at least one allowed reference; assumptions can lack references. Semantic grounding and uncertainty remain human evaluation questions. A valid specialist result can contain only gaps and still succeed at runtime.

`ToolName` is one of `logs`, `metrics`, `changes`, `dependencies`, `history`. Observability may request logs/metrics/history; Dependency may request changes/dependencies/history. The later single investigator gets all five; coordination/reasoning/critic/reporting get no retrieval tools. Do not implement unvalidated role-specific payloads through an arbitrary-object fallback.

Validate a tools action as one bounded batch before dispatch: allowed names, source-specific argument schemas, batch size, and independent requests. A dependent query belongs in a later model turn. Invalid batches dispatch no tools, retain the model receipt/usage, and receive bounded correction/failure handling. Never reserve a full run allowance for each role.

`operation_id` identifies a logical requested call. For tools it is the originating model call ID plus the zero-based request index; for a model turn it is task-attempt ID plus turn index. The turn index counts distinct model operations, not retries. A retry retains that operation ID but has a distinct call ID, invocation binding, allowance, and `retry_of`. An unknown prior model turn retries its saved request; a saved valid model response is processed instead of regenerated. A resumed tool batch retries only unresolved operations, retaining its saved siblings.

The current task's permitted references are its inherited context and validated retrievals from its attempts, with actual source content supplied through saved context/model requests. A new task attempt can explicitly inherit its task's saved evidence and receipts. Merely existing elsewhere in the dataset or a parallel worker's unseen state does not make evidence citable. A fact about missing coverage cites its tool receipt. Different content under one observation identity is an integrity error. Same content from repeated retrieval reuses the observation while retaining each query receipt.

Require same run/task/active-attempt binding, eligible terminal call, required input revision, expected result contract, and available permitted references before accepting a result. Do not trust model-provided identity fields or accept writes to runtime fields. Terminal task results are immutable; replay of the identical acceptance is a no-op.

## 4. Module interfaces and durable transitions

Use simple modules, not a plugin or repository framework. Records/validators define contracts; the controller implements deterministic transitions; adapters own model calls and retrieval; the runner owns locks/capacity and graph/checkpointer lifecycle. Inspection/export reads saved state without invoking graph execution or saver setup.

| Interface | Input -> output; allowed effects |
| --- | --- |
| `prepare_context` | `(control, task_id) -> TaskContext`; select bounded accepted inputs and real evidence, no external I/O. If required inputs cannot fit, return an explicit context-limit error; do not silently remove prerequisites. |
| `admit_calls` | `(control, prepared_requests, invocation_id, now) -> next_control`; validate readiness/limits and add identified admitted calls/requests/reserves. No external call. |
| `execute_call` | `(saved_call, invocation_context) -> inbox update`; guard invocation/deadline/capacity, perform at most one adapter call, return bounded raw receipt/available usage. No task/result/budget write. |
| `accept_receipts` | `(control, inbox) -> next_control`; process unconsumed receipts, reconcile usage, validate tool observations or model actions, advance the owning task attempt. No external I/O. |
| `accept_result` | `(control, terminal_call_id, candidate) -> next_control`; bind identities and atomically include accepted body, required evidence references, attempt completion, and task success in one control value. |
| `recover_task` | `(control, recovered_inbox, invocation_id, now) -> next_control`; reuse saved receipts, fence unresolved old calls, preserve accounting/deadlines, and identify admissible new calls. It does not invoke adapters or refund admissions. |

These pure interfaces propose state; the graph makes it authoritative only after persistence. Controller operations share one control update when required to preserve an invariant. Framework wiring must follow this sequence:

| Boundary | What must be durably saved before advancing |
| --- | --- |
| Task start | Pending -> Running, a new active task attempt, resolved context/input revision, deadline, and task-attempt admission. No model call yet. |
| Call admission | Identified requests, their operation/retry/invocation links, reservations, counters, and applicable source-fault selections. Save with synchronous durability before dispatch nodes execute. |
| Call receipt | A call node performs one external operation and returns its raw response/error and available usage to the inbox. Node pending writes preserve successful siblings; a completed function alone is not proof that its output reached storage. |
| Receipt acceptance | Save validated evidence, processed receipt IDs, usage reconciliation, action/batch progression, and events together in control. A raw receipt alone does not succeed a task. |
| Result acceptance | Accepted terminal result, required references to saved evidence/receipts, task/attempt success, and event are one control update. No downstream task can see only part of it. |
| Failure/closure | Preserve accepted work; record failed attempts, retry Pending or terminal Failed/Skipped outcomes, eligibility changes, accounting, and stop reason under HLD-02. No final diagnosis is synthesized by the runtime. |

Call nodes use dynamic fan-out where needed and return to the serialized controller after the current call superstep settles. They do not contain an agent loop or bypass LangGraph's step barrier. Preserve each task's unfinished progression across those steps; HLD-02 requires the whole admitted specialist batch to settle before dependent Reasoning. Queued dependencies consume no runner execution permit. A finite shared runner semaphore bounds actual adapter calls across investigations; it is supplied through invocation context, not persisted as a live object.

An allowed call retry stays inside the active task attempt. End the prior call's findings eligibility before admitting its successor; a timeout/error or unresolved old call cannot later supply a replacement result. A task-level retry first ends its old attempt, moves Running -> Pending, and starts a distinct task attempt when admitted, preserving evidence and all usage. Invalid output correction and retries are bounded by call, retry, task-attempt, and deadline limits. Task states and closure follow the five HLD-02 states; process interruption is not terminal closure.

Source-fault selection occurs at call admission under the source-scope rules in LLD-02: persist the chosen fault and counter update together. A replay of a saved admission does not consume the fault again. A new external attempt consumes its own source-fault step. Changing query/page parameters within a matching fault scope does not reset its schedule.

## 5. Recovery, accounting, and late output

Each modifying runner invocation gets a fresh UUID in non-checkpointed runtime context after acquiring the per-investigation process lock. Before graph/checkpointer setup, compare immutable version metadata read-only. Do not supply the saved invocation ID as the resumed process identity. Invoke graph execution once per invocation; recovery after a graph/storage error uses a fresh invocation identity. Disable SDK and graph call-node retries rather than letting the framework repeat a call under the same admission.

On replay, `execute_call` compares the saved call's invocation ID with the current one **before acquiring a permit or invoking an adapter**. A mismatch returns `replay_blocked`, not another external call. This notice is not proof that the old call was never sent. Pending writes from already successful call nodes are reused by the graph. The controller handles saved receipts before unresolved notices; a valid saved completion takes precedence over a notice for the same call.

| Saved condition | Controller action |
| --- | --- |
| Task succeeded | Reuse accepted result; dispatch no further calls for that task. |
| Completion saved, acceptance missing | Process saved receipt and accept/reject its action/result without another call. Repeated recovery must not apply the action twice. |
| Old call admitted, no saved completion | Mark unresolved and ineligible for findings, retain its full allowance, and keep the task attempt/context/deadline. Admit a distinct call only if the remaining original limits permit. |
| Partial tool batch saved | Retain settled operations and their evidence. Retry only eligible unresolved/failed operations, then give the model the complete batch's explicit outcomes. |
| Task/run deadline expired or capacity exhausted | Do not dispatch. Apply resource-exhausted closure and generate a partial progress summary from saved state. |
| Checkpoint write fails | Do not advance to the next external step. Surface storage failure; preserve the last durable state and treat calls without durable completions as unknown on recovery. |

Admission charges model/tool call counts once per admitted external attempt and holds a per-model-call token allowance. Derive these totals from saved call/task-attempt registries rather than maintaining competing mutable totals. The token admission total is complete known usage plus full allowances for calls whose usage is active, partial, or unknown. Never also charge partial known tokens for the same held allowance. Tool calls hold zero model tokens but consume tool-call/deadline/capacity limits. Unknown dispatch is reported separately from a confirmed external attempt; admission counts are conservative execution guards, not fabricated measured activity.

A current-invocation `not_dispatched` receipt may release its model reservation only if the executor establishes that it invoked no adapter. Old-invocation replay blocking cannot do that. Complete reliable usage replaces its own reservation; exceeding an allegedly validated bound is an explicit accounting failure that stops new dispatch and retains the observed usage. Never clamp real usage to the configured cap.

Late responses and reliable usage amendments are retained as separate immutable receipts. The controller may update accounting for their original call after task/run closure, under the modifying lock and a serialized accounting-only transition; it cannot reopen the task or admit new work. Accept only a matching provider/call identity and consistent reliable measurements. Conflicting usage evidence is an integrity error, not a last-write-wins update. If no collector is still alive, usage remains unknown and reserved; do not introduce a background reconciliation service.

Closure ends findings eligibility before requesting bounded local cancellation/draining. Acceptance wins only if its complete control transition was durably committed before closure. A raw or late response never wins that race merely because the network operation finished first. Cancellation does not establish zero provider usage. An unclean process death may leave status Running; inspection labels the saved status and absent live owner accurately, without changing it.

## 6. Walkthrough and minimum observability

The following is a generic task-flow illustration, not a benchmark answer:

1. Start task T1/attempt A1 from committed context. Admit model call M1 and save its request/reservation.
2. M1 returns two independent retrieval requests. Save its receipt, then process that action once and admit calls C1/C2 as one bounded batch.
3. C1/C2 execute concurrently. Each saved receipt remains separately auditable; overlapping intervals demonstrate real parallel execution. Valid returned records become observations only through controller acceptance.
4. Admit M2 with both outcomes, including partial/failure states. It returns findings with real references or records the missing evidence.
5. Save M2's receipt before atomically accepting result T1 and Succeeded. A dependent task sees the accepted result, never a half-completed acceptance.
6. Kill after C1's receipt is durable but C2 has no saved completion: resume reuses C1, blocks replay of C2's old dispatch, and retries C2 under a new call ID only within its remaining allowances. A saved M2 response similarly avoids another model call.

Minimum telemetry comes from these records, not a new monitoring deployment. Controller events have `event_id: str`, `revision: int`, `at: UTC`, `kind: str`, nullable `task_id`/`attempt_id`/`call_id`, and `details: object`. IDs are derived from committed revision, kind, and entity so reprocessing cannot duplicate the same logical event. Record admission, validation rejection, accepted result, retry, recovery, and closure. Each event names referenced records rather than repeating full prompts/evidence.

Model requests retain rendered messages/settings and receipts retain returned output; tool requests/receipts retain arguments, source records, completeness, and errors. Credentials, authorization headers, and client connection objects never enter these records. Prompt and retrieval content are data, with no permission to change registered tools, ownership, or limits. Oversized/invalid responses retain a bounded diagnostic payload and available usage, marked truncated/rejected, and cannot be accepted as a complete result. Tool-specific bounded partial retrieval follows the tool contract.

Read-only inspection and derived JSON trace exports correlate run -> task -> task attempt -> call -> receipt -> evidence/result, including unprocessed receipts. Show saved revision, dispatch uncertainty, stop reasons, task/call durations, wall-clock run latency, calls, measured input/output tokens, and held allowances separately. Estimated monetary cost is derived only after model prices/settings are recorded and is labelled an estimate; missing usage is not zero cost. Diagnostic correctness and human grades are evaluator outputs, not runtime health metrics.

CPU/memory sampling, dashboards, hosted tracing, and richer telemetry are deferred until the investigation system works. Minimal tracing and basic runtime guardrails accompany the first executable slice rather than becoming another prerequisite project.

## 7. Verification and next implementation gate

Extend existing cases through [incident-evals](../.agents/skills/incident-evals/SKILL.md): HLD-OWN-01 for identity merge/provenance; DEP-01 for actual inputs; DUR-01 for call/acceptance and partial batches; CLOSE-01 for late accounting; RUN-01 for inspection/isolation. Use scripted valid controls and adverse outputs against the real graph/checkpointer, not a duplicate scheduler or model-only demonstration. All remain planned until the runtime/dependencies exist.

Retrieval requests/receipts, frozen JSONL snapshots/cutoffs/faults, normalized coverage, and finite local double settings are specified in [LLD-02](tool-contracts.md). Dependency pins are adopted in `uv.lock` after resolution succeeded; real graph/checkpointer checks remain a product-validation gate. Other role payloads/coordination contracts precede their affected behavior. Provider/model, validated reservation bounds, and spending cap precede real-model calls; benchmark configuration and harness precede comparison batches. The [HLD gap register](hld.md#delivery-gap-register) retains supporting work without blocking unrelated functional work. Workspace procedures are outside this product-design handoff.

Framework evidence, checked 2026-10-19: the [Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api) supports typed state/reducers and documents node re-execution; [checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers) document synchronous step durability and successful sibling pending writes. Problem -> rule -> verification: a call hidden inside a replayed node can repeat; separate admission/call/acceptance and guard invocation ownership; test process interruption and checkpoint failure against pinned releases. These sources support the design direction, not proof of our integration or selected pins.

## 8. Remaining contracts and decision gates

This is the current index of remaining product decisions, not an implementation schedule. LLD-01 common/specialist contracts and LLD-02 retrieval contracts are specified; the complete six-role LLD is not. No new schema, numerical setting, provider, or acceptance target is selected by this documentation reconciliation.

All verification below is **planned, unexecuted**. Runtime, source fixtures, tools, runner, and resolved dependency integration are absent. The existing [evaluation specification](evaluation-plan.md) owns the checks and grading policy; its expectations never become runtime context.

| Open item | Already agreed / source | Still to specify or establish | Resolve before | Verification to reuse |
| --- | --- | --- | --- | --- |
| Dependency adoption and integration | Python + LangGraph + local SQLite; adopted versions and the initial failed resolution attempt are recorded in [LLD-02](tool-contracts.md#7-dependency-preflight-and-next-executable-milestone). | A compatible interpreter/package lock and actual typed-tool, synchronous-checkpoint, pending-write, and read-only inspection results. Dependency identity must cover adopted versions. | The first executable lifecycle can be called validated. | HLD-DUR-01, HLD-RUN-01; native-tool/preflight controls. |
| Remaining role payloads | [HLD-01](hld.md#investigation-task-role-and-attempt) defines all six responsibilities and structured exchange; LLD-01 defines the two specialist contracts. The single investigator reuses infrastructure without the six-role chain. | Validated Orchestrator, Reasoning, Critic, Reporting, and single-investigator action/result schemas; hypothesis/review references and report confidence/uncertainty representation. No arbitrary-object fallback. | Each affected role is implemented. | HLD-OWN-01, HLD-DEP-01, HLD-FEED-01; report provenance and human task/report review. |
| Coordination records and transitions | [HLD-02](hld.md#hld-02-bounded-investigation-rounds-and-explicit-dependency-outcomes) accepts settlement versus required inputs, exact reviewed revisions, objection dispositions, and durable round/progress identities. | Fields and atomic transitions for admitted rounds, hypothesis revisions, reviews/objections, routes/dispositions, and progress updates, including replay and closure. | The coordination/feedback loop is implemented. | HLD-DEP-01, HLD-FEED-01, HLD-PROG-01, HLD-DUR-01, HLD-CLOSE-01. |
| Full-loop settings | LLD-01 defines finite task/call/deadline enforcement; LLD-02 saves double-only values. HLD-02 defines what counts as retrieval progress. | Round/no-progress thresholds and their persisted configuration; mode-specific guards within shared total per-run allowances. Existing double values are not real-model budgets. | The affected loop is enabled. | HLD-PROG-01, HLD-CLOSE-01, HLD-RUN-01; baseline parity controls. |
| CLI and report/export contracts | [PRD user flow](mvp-plan.md#local-user-flow-and-artifacts) requires run, concurrent runs, inspection, resume, evaluation, readable reports, JSON, and traces. LLD-01 keeps inspection read-only and exports derived. | Command/flag and input/output contracts, artifact layout, report schema/revision binding, and partial/inconclusive presentation. Preserve the distinction between execution status and diagnosis. | The corresponding public operation is implemented. | HLD-RUN-01, HLD-CLOSE-01; citation validity and human report review. |
| Real-model adapter and spending | Keep unknown usage reserved, use new attempt identities for retries, disable hidden retries, and report measured usage separately. Synthetic incident data may go to an external API. | Provider/model selection, settings, validated request/output token-reservation bounds, recorded prices, and user-approved spending cap. | Any real-model call. | HLD-CLOSE-01, HLD-DUR-01; provider-bound and usage/accounting checks. |
| Evaluation execution configuration | The [comparison protocol and rubric](evaluation-plan.md#comparison-protocol) fix fair per-run conditions, fresh state/faults, all-attempt retention, five assignment metrics, and human grading. | Batch input/result schemas, repeat count, run ordering, concrete limits, and repeat aggregation/missing-result representation. Preserve ten primary incidents and report R1/R2 separately. | A comparison batch. | GAP-EVAL, HLD-RUN-01, comparison/metric checks and human review. |
| Diagnostic acceptance target | All deterministic reliability checks are mandatory; an evidence-supported inconclusive outcome can be correct. No numerical diagnosis target is selected. | User-selected diagnostic threshold after reviewing exploratory measurements; record it prospectively with evaluation versions. | Subsequent diagnostic validation, after exploratory measurements. | Existing acceptance protocol; never retroactively pass exploratory runs under a later target. |

Submission work remains visible in the [PRD deliverable status](mvp-plan.md#deliverables-and-current-state): runnable setup, a diagram/explanation checked against the implementation, synthetic data/tests, actual comparisons, the maximum-500-word technical note, and the complete coding-assistant transcript. Existing snapshots have explicit cutoffs and remain unchanged; they are not a claim that the full current conversation has been exported.
