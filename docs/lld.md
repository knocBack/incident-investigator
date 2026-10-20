# Runtime state and recovery contracts

This document describes the implemented runtime. [Architecture](architecture.md) explains the component boundaries, [role contracts](lld-03-roles.md) define model payloads and [tool contracts](tool-contracts.md) define retrieval.

## State ownership and identity

`Control` is the authoritative investigation record: run configuration, current stage, tasks, calls, evidence, receipts, hypothesis revisions, objections, dispositions, fault progression and usage. `GraphState` carries that control record and an inbox. Workers write only receipts to the inbox; one stage commit node validates and updates control.

Runtime allocates run, task, attempt, call and invocation identities. Models cannot reset counters or choose the identity of a retrieved observation. An identity-based inbox reducer accepts identical replay and rejects different payloads under one call ID. Task results are scoped to their owning task. Conflicting specialist findings coexist for reasoning to assess.

A task has one logical attempt. Individual failed calls can retry with fresh call/attempt identities and a `retry_of` link within the configured allowance. Accepted evidence and results are retained across those retries. A successful task means a valid payload was accepted, not that the diagnosis was correct.

## Admission, execution and acceptance

1. Construct task context from the alert, allowed registry schemas, committed evidence and prerequisites. Exclude evaluator information.
2. Serialize admission through the control writer. Charge the model/tool allowance, select any controlled tool fault, and checkpoint the dispatched call before external execution.
3. Execute one operation per worker. Apply the run deadline, per-call timeout, caller semaphore and shared local-runner capacity. Save payload, errors, available usage and timing as a raw receipt.
4. Commit receipts at the next control boundary. Validate identities, role permissions, schemas and task-visible citations. Repair bounded harmless deviations and log repairs. Accept evidence and results together with their task state.
5. Admit the next calls, or settle the stage and choose a conditional edge.

Duplicate normalized retrieval is answered from an existing receipt without a new tool call. A failed or partial source remains a gap. Tool failure records cannot support an application-cause claim. One correction turn handles unusable model output; call retries do not create unlimited correction turns.

## Limits and closure

| Bound | `demo` | `demo-live` |
| --- | --- | --- |
| Model calls per run | 40 | 40 |
| Tool calls per run | 60 | 60 |
| Model calls per task | 10 | 8 |
| Tool calls per task | 15 | 15 |
| Wall seconds per run | 240 | 1,200 |
| Seconds per call | 30 | 90 |
| Retries per call | 1 | 1 |
| Review rounds | 3 | 3 |
| No-progress rounds | 2 | 2 |

OpenRouter and the local CLI adapter default to `demo-live`; scripted mode defaults to `demo`. An explicit `--limits-profile` overrides that choice. Provider adapters inherit the profile timeout. A model action can request at most five tools, and a plan at most four assignments.

Progress is newly retrieved evidence or a first complete-empty query page, assessed once per reviewed round. Duplicate searches, failed pages and changed prose do not reset the no-progress count. Every critic objection receives a gather, reanalyze or unresolved disposition. Reanalysis consumes a round and requires a new critic review. Open objections remain explicit when the loop closes.

A task allowance failure settles only that task. Failed planning, reasoning, critic or decision work routes to Reporting when capacity remains. Failed Reporting and hard global limits retain partial exports. `stop_category` distinguishes completed, inconclusive, interrupted, resource_exhausted and failed outcomes; `stop_reason` carries detail. A completed execution can have an inconclusive diagnosis.

## Durable recovery

Each run has one SQLite checkpointer with synchronous durability. Admission, raw worker writes and accepted control state have separate durable boundaries. Traces and exports are not recovery authority.

| Last durable state | Resume behavior |
| --- | --- |
| Accepted result | Reuse it without another external call |
| Saved worker response | Validate and commit it without another external call |
| Admitted call with no saved response | Keep its charge; retry under fresh identities only if limits allow |
| Expired original deadline | Export bounded termination without fresh external work |

`run.json` records immutable limits, model mapping, original deadline, source digest, faults and compatibility versions. `source/` retains the exact registered input. Resume reloads it and checks dataset, prompts, tool contract, runtime, dependencies and checkpoint schema before writable saver setup. Changed versions are refused; resume across code upgrades is not supported.

A kernel lock on `invocation.lock` rejects a second modifying invocation. Process death releases the lock even though the file remains. Each invocation has a fresh identity; pending work from an earlier invocation is not blindly re-executed. Missing responses are indistinguishable from calls completed remotely without a saved response, so exactly-once provider execution cannot be guaranteed.

The wall deadline and consumed allowances never reset. Missing token/cost information remains unknown, with known subtotals available separately. The evaluation-suite monetary guard retains unresolved reserves; runtime call bounds and provider billing are distinct accounting layers.

## Local artifacts

`report.md` and `result.json` export diagnosis and process state. `trace.jsonl` records calls, validation and transitions. `state.sqlite`, `run.json` and `source/` support compatible recovery. `snapshot/` retains the reviewable input with manifest and digest. A demo's `expectation.json` is an evaluator pointer outside the agent-visible source. Grading writes separate suite artifacts by default.

Graceful interruption exports saved partial progress. An abrupt kill may leave stale exports; resume rebuilds them from checkpoints. Runs without versioned metadata are viewable but cannot use this recovery contract.
