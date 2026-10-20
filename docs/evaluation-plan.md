# Incident and evaluation specification

Status: accepted protocol for the ten primary incidents and two reliability variants. The current implementation has a separate five-family fault lab, grader and evaluation console. The paired fixtures and single-agent baseline are a separate change; the matched benchmark and comparison report are still pending. See [evaluation status](evaluation.md).

Audience: dataset authors and human evaluators. This document includes expected answers. Never provide it, design documents or evaluator labels to runtime agents or their retrieval tools. File naming alone does not enforce this boundary; implementation must enforce it.

## Shared scenario rules

- Represent Checkout -> Inventory -> Database using synthetic records only. Maintenance transactions and caches are activity inside these components, not new deployed services.
- Start each run with an opaque case ID, affected service, symptoms, observation window, and cutoff. The ID must not encode the cause.
- All example times below are UTC on a single synthetic day. Include pre-failure baseline records. Unless noted, the alert arrives at 14:05 and observations stop at 14:10.
- Separate the agent-visible alert/observations from evaluator-only expectations. Never dispatch a case ID to a diagnosis inside a tool.
- Provide logs, metrics, changes, dependencies, and historical-incident sources. A source can truthfully contain no matching records; every run need not call every category.
- Include stable evidence IDs, timestamps, service/version context, and units/windows where relevant. Preserve request or transaction relationships needed to connect clues.
- Changes are raw release/configuration records. History contains earlier incidents, never this incident's eventual postmortem.
- Simulated tool faults change retrieval availability, not the underlying application cause. State complete, partial, and failed outcomes explicitly.
- Numeric values below are fixture requirements or illustrative rounded rates; materialized records must reconcile counts, traffic shares, units, and measurement windows.

Each fixture must include the observations below and enough baseline/unaffected context to test alternatives. Expected conclusions describe what that evidence supports, not information agents may be secretly given.

## Ten primary incidents

### Case 01 - Incorrect Inventory address

Family: bad configuration.

- Alert: Checkout errors rise from below 1% to approximately 60% at 14:02.
- Timeline: Checkout v2 rollout starts at 14:00 and receives roughly 60% of traffic at 14:02. External demand is unchanged.
- Logs: v2 attempts inventroy:8080 and reports DNS lookup failures; v1 successfully calls inventory:8080.
- Metrics: Inventory receives roughly 40% of its earlier traffic, with normal latency and successful responses for requests that reach it. Database latency and lock-wait observations show no matching deterioration.
- Changes/dependencies: the effective Checkout configuration changed inventory:8080 to inventroy:8080; the dependency source records the correct Inventory endpoint.
- Misleading evidence: a prior incident with similar customer symptoms was caused by database contention.
- Expected: caller-side Inventory address error introduced in the Checkout rollout. Connect the changed value, attempted host, version split, and onset; timing alone is insufficient.
- Contradictions: healthy Inventory measurements cover received requests, not failed lookups. Database contention does not explain lookup failures before Inventory is reached.
- Actions: recommend restoring the address or reverting the configuration, then checking lookups, error rates, and Inventory traffic. Do not claim recovery occurred.

### Case 02 - Caller deadline below normal dependency latency

Family: bad configuration.

- Alert: Checkout timeouts rise after 14:02 while Inventory remains available.
- Timeline: Checkout v2's Inventory deadline changes from 500 ms to 50 ms at 14:00.
- Logs: affected attempts expire near 50 ms; unaffected callers with the older setting complete successfully.
- Metrics: Inventory's response times remain in their baseline 80-120 ms range under the relevant load. External traffic, database latency, and lock waits are stable.
- Changes: expose the actual timeout difference, not a diagnosis that Inventory is too slow.
- Expected: Checkout's shortened deadline is incompatible with observed normal dependency latency; the evidence does not establish a new Inventory slowdown.
- Contradictions: Inventory can be healthy yet exceed a caller's unsuitable deadline. Historical dependency slowness is not proof of current slowness.
- Actions: restore an appropriate deadline based on observed behavior and the request's overall budget; verify before suggesting more Inventory capacity.

### Case 03 - Inventory query regression

Family: deployment regression.

- Alert: Checkout latency increases and some requests time out.
- Timeline: Inventory v2 begins serving traffic at 14:00; the higher latency follows its rollout.
- Logs: correlate slow Inventory v2 requests with a changed database query fingerprint and increased scan/query work. Include the relevant release record describing the query change.
- Metrics: v2's affected operation is much slower than the comparable v1 operation. Database work per affected query rises, but lock waits and connection capacity do not explain the difference.
- Controls: comparable v1 requests remain near baseline; the fixture must not simultaneously claim global database saturation that would make this control implausible.
- Expected: a costly query introduced by Inventory v2 is the probable cause.
- Contradictions: aggregate service measurements may hide the version difference. Higher database work alone does not establish a server-capacity shortage.
- Actions: recommend reverting the query change or investigating its execution plan. Naming a particular missing index is unsupported unless an observation establishes it.

### Case 04 - Checkout response-handling regression

Family: deployment regression.

- Alert: Checkout fails despite successful Inventory requests.
- Timeline: failures start on Checkout v2 after its rollout.
- Logs: Inventory returns successful responses; v2 raises a missing-field exception while handling them. A synthetic response-field sample and the affected request ID connect the response to the exception.
- Metrics: Inventory and database behavior remain near baseline; v1 Checkout handles comparable responses successfully.
- Changes: release history records a Checkout response-handling change.
- Expected: a new Checkout response-handling assumption is incompatible with the observed Inventory response, not an Inventory availability failure.
- Contradictions: dependency HTTP success does not guarantee caller success; an exception at the caller is not evidence the dependency was unreachable.
- Actions: recommend reverting/fixing response handling and validating the response contract.

### Case 05 - Blocking database maintenance transaction

Family: database contention.

- Alert: Checkout and Inventory latency rise.
- Timeline: a database maintenance transaction begins at 13:57 and blocking appears at 13:58. An unrelated Checkout deployment occurs at 14:03.
- Logs: identify waiting stock updates, the blocking transaction, and its still-open lifetime during the incident.
- Metrics: lock waits rise while database CPU can remain moderate; Inventory connection waits/latency propagate upstream.
- Changes: the Checkout release is visible but follows the onset. No relevant Inventory release precedes it.
- Expected: the long-running maintenance transaction is blocking stock updates.
- Contradictions: low CPU does not rule out lock contention; the later Checkout deployment cannot explain the earlier onset.
- Actions: recommend inspecting the blocker and safely ending or completing the maintenance work under operator control; do not autonomously kill transactions.

### Case 06 - Inconsistent database lock order

Family: database contention.

- Alert: intermittent Inventory failures propagate to Checkout.
- Timeline: failures appear after an Inventory change to multi-item stock reservation.
- Logs: explicit deadlock records and transaction sequences show one request locking stock rows A then B while another locks B then A.
- Metrics: transaction rollbacks increase without sustained CPU saturation or broad connection-capacity exhaustion.
- Changes: record the reservation-path change; preserve enough version/request context to connect the new behavior to the transactions.
- Expected: inconsistent lock ordering causes the deadlocks. Merely stating database trouble is too broad.
- Contradictions: intermittent deadlock victims do not imply a general database outage. Do not assume only new-version requests can be victims when transactions interact.
- Actions: recommend consistent lock ordering, shorter transactions, and bounded handling of deadlock retries rather than unrestricted retrying.

### Case 07 - Inventory database connection leak

Family: resource exhaustion.

- Alert: Inventory requests wait for connections and Checkout times out.
- Timeline: leased connections climb after an Inventory release and remain occupied after the associated requests finish.
- Logs: correlate completed requests with leases that remain owned in subsequent pool snapshots. Do not rely only on the absence of a release log.
- Metrics: Inventory's pool reaches its limit, for example 20 leased out of 20. The database has connection headroom and mostly idle connections, with no matching long-running query or open-transaction buildup.
- Expected: Inventory is failing to return connections to its local pool, causing acquisition waits.
- Contradictions: local pool exhaustion is not the same as reaching the database server's connection limit. Low query load is compatible with exhausted local leases.
- Actions: recommend investigating cleanup paths and restoring capacity cautiously; a temporary restart is a mitigation, not proof the leak is fixed.

### Case 08 - Inventory memory growth and restarts

Family: resource exhaustion.

- Alert: Inventory availability becomes intermittent and Checkout errors recur.
- Timeline: memory and in-process cache entries grow under roughly steady traffic; process restarts occur at the memory limit.
- Logs: memory-limit termination/restart events and cache-entry observations bracket the failures.
- Metrics: memory rises before restarts; database measurements do not show a corresponding bottleneck.
- Incomplete evidence: the memory-series response explicitly lacks 14:04-14:06. Other available observations still establish memory exhaustion.
- Expected: Inventory memory exhaustion is supported; cache growth is the probable mechanism. Do not claim a proven allocation path or an unseen heap analysis.
- Contradictions: a restart resets current memory but does not establish lasting recovery. The missing interval is not a measured drop or zero.
- Actions: recommend inspecting cache growth and limits, verifying memory behavior, and distinguishing immediate mitigation from a durable fix.

### Case 09 - Short traffic burst hidden by averaging

Family: traffic overload.

- Alert: Checkout experienced a latency/error burst around 14:02.
- Timeline: external demand rises sharply during 14:01-14:03, then falls. No relevant deployment or configuration change precedes it.
- Metrics: short-window samples show Inventory worker saturation and growing queues. A 13:50-14:05 CPU average remains around 31%, consistent with approximately 20% baseline and two minutes near 100%.
- Logs: queue/deadline failures align with the burst; database observations show headroom.
- Conflicting appearance: the broad average looks healthy while the short samples show overload. Both measurements are correctly labeled and can be true.
- Expected: the brief demand spike exceeds Inventory's serving capacity. Explain the measurement-window mismatch rather than discarding one observation.
- Actions: recommend demand/concurrency controls or capacity investigation appropriate to the burst; avoid blaming an absent deployment.

### Case 10 - Retry amplification sustains overload

Family: traffic overload.

- Alert: Inventory remains overloaded after a brief initial slowdown; Checkout errors persist.
- Timeline: a Checkout policy change increases maximum attempts from one to five with no backoff. The initial slowdown triggers retries, and queueing remains elevated afterward.
- Logs: repeated attempts share logical request IDs. Preserve attempt numbers, timing, deadlines, and Inventory queue failures.
- Metrics: external logical request rate stays near baseline while Checkout-to-Inventory attempt rate rises substantially. Inventory saturates without a matching database bottleneck.
- Changes: expose the attempt/backoff settings before and after the change.
- Expected: retries amplify the initial problem and sustain excess work; external customer demand is not the source of the increased attempt rate.
- Contradictions: dependency slowness can both trigger retries and worsen because of them. Do not claim the initial trigger's exact cause if it is not established.
- Actions: recommend bounding attempts within the request budget, introducing appropriate backoff, and verifying internal attempt volume and recovery.

## Two reliability variants

These reuse primary cases and do not count as additional distinct incidents.

### Variant R1 - Transient Inventory-log timeout

Base: Case 07.

- The first matching Inventory-log request times out; the next matching request succeeds. All other source observations and evaluator expectations are unchanged.
- Reset the fault state for every run. Match by tool/source scope rather than an arbitrary global call number, so parallel scheduling does not select a different failure.
- Return a structured timeout with no invented observations. Count both the failed and successful attempts.
- Expected behavior: preserve work from other sources, recover within the configured retry limit, and produce the supported connection-leak conclusion if the evidence is retrieved.
- Verify recovery deterministically in a runtime test. If an investigation never needs that query, record that the benchmark fault was not exercised; do not pretend it was.
- Do not hardcode a successful diagnosis just because this is a recovery variant.

### Variant R2 - Exact cause cannot be established

Base: Case 01.

- Checkout logs remain unavailable throughout the run; bounded retries do not restore them.
- Change history lists the rollout but explicitly omits its configuration diff.
- Keep version-level failure metrics, received Inventory traffic, the dependency map, and database observations. Remove any other record that reveals the attempted misspelled hostname.
- The evaluator retains the original hidden cause, but the agent-visible observations no longer establish that exact mechanism.
- Expected behavior: identify the new Checkout release/caller path as a supported suspect, explain alternatives and missing evidence, and leave the exact mechanism inconclusive.
- Naming the hostname typo, quoting an unseen diff, or claiming unavailable logs were checked is unsupported.
- The final status must distinguish unresolved diagnosis from an uncontrolled runtime crash. State what additional evidence would resolve the uncertainty.

## Fixture and tool acceptance checks

Before any model runs:

- Validate all required observation categories, evidence IDs, timestamps, units, measurement windows, version/operation scopes, and cross-source relationships.
- Reconcile traffic shares and error counts, latency/timeout ordering, pool limits, transaction sequences, and averaged versus short-window measurements.
- Filter every tool by its supported arguments using actual observations. A narrower query must not magically reveal a prewritten conclusion.
- Distinguish an empty complete query from a partial or unavailable source.
- Enforce the observation cutoff, including deployment history and historical incidents. Do not leak future resolutions.
- Retest a fixed source snapshot under two different execution wall-clock dates: the same queries/cutoff return the same visible observations. Exclude a later-available record even if its event time is earlier, and exclude a metric window extending beyond the cutoff. Changed snapshot content requires a new dataset identity; resume under a mismatch refuses before work. The current loader/tool tests cover development snapshots; applying these checks to the paired benchmark fixtures remains part of the separate evaluation change.
- Verify that faults are reproducible, reset between runs, and do not change the underlying incident data.
- Verify that runtime inputs, registered tools, source loaders, and prompts cannot access evaluator labels or these documents. A filename convention is not a security boundary.
- Verify citation provenance against evidence actually returned in that run.

## Runtime and engineering acceptance

All deterministic reliability checks must pass. Use controlled local doubles and fixtures to verify these behaviors without relying on a particular model response:

| Scenario | Required observation |
| --- | --- |
| Independent specialists and tools | Execution intervals overlap for independent specialist tasks and for independent tool requests issued together. Dependent tasks wait for their prerequisites. |
| Concurrent investigations | At least two investigations progress concurrently; evidence, hypotheses, task state, artifacts, fault counters, and budgets remain isolated. |
| Concurrent result merging | Vary worker completion order and verify that all committed findings survive with valid provenance. |
| Same-investigation invocation | A second modifying top-level invocation is rejected while one is active. Reading saved progress remains possible. |
| Agent/tool failure | Eligible failures retry within their limits; other completed work survives. Persistent unavailability remains explicit instead of becoming fabricated healthy data. |
| Process restart and resume | Stop a run after completed progress is saved and restart the process. Resume the same investigation with its configuration, evidence, consumed budgets, and fault state. Interrupted work may repeat; committed findings do not duplicate. |
| Recovery commit boundaries | Interrupt before dispatch, after an external call, after saving its response, during result acceptance, and after acceptance. Saved valid responses are reused without a new call; task completion and its accepted result/evidence remain consistent. Repeated recovery does not duplicate acceptance. |
| Unknown usage on resume | Preserve the full reservation when usage is missing; retain an explicit unknown measurement. Retry or continue only within remaining original limits, using a distinct attempt identity. Insufficient allowance produces resource exhaustion, without erasing saved work or routinely requiring manual review. |
| Concurrent reservation admission | Parallel attempts check and reserve atomically so their combined allowance cannot exceed the configured cap. Reliable actual usage replaces its own reservation rather than being added twice; another investigation remains isolated. |
| Invocation owner dies | Reject a second modifier while the owner is active. After the owner dies, permit recovery without treating a leftover marker file as proof of a live owner. Inspection remains read-only. |
| Resume compatibility | Change a recorded dataset, prompt, tool-contract, runtime, or checkpoint-schema version incompatibly, then request resume. Refuse before further model/tool work, identify the mismatch, and preserve the checkpoint. With compatible versions, retain the original observations and configuration. |
| Resource limits and no progress | Deadlines, retry/call/token limits, shared concurrency admission, and no-progress rules stop or constrain work as configured, including parallel work and resume. |
| Artifacts and outcomes | Reports/results/traces are readable and consistent; inconclusive diagnosis, interruption, exhausted limits, and unrecoverable failure are distinguished with available evidence and a stop reason. |

Demonstrate structured role exchange, critic-requested targeted evidence gathering, and hypothesis revision using controlled runtime tests and inspect recorded real-model runs. The two reliability variants retain their case-specific requirements, including recovery and justified uncertainty. Engineering checks alone do not establish model diagnosis quality. These tests are not additional application services or incident families.

The runtime regression suite now covers parallel completion, bounded recovery, invocation locking, incompatible versions and unknown-call accounting. See [runtime contracts](lld.md) and `tests/test_reliability.py`. This protocol also defines broader benchmark acceptance; a passing development test is not evidence that the pending paired benchmark passed.

## Checkpoint validation process

At every major accepted behavioral-design checkpoint or coherent implementation milestone, invoke the project-local [incident-evals skill](../.agents/skills/incident-evals/SKILL.md) together with this process, under this maintenance protocol. This project rule extends the skill's failure-driven trigger: prospective contract checks need no observed failure and must not be presented as reproduced regressions. Save the accepted decisions first, then add or strengthen a few focused cases for changed contracts. Reuse existing coverage; routine wording/layout changes do not require new product cases or a forced case-count quota.

Record each case's stable ID, contract/setup, observable expected outcome, validation method, execution status, commands/results when run, and blockers. Use deterministic runtime/tool checks for execution invariants and human review for semantic task quality. Run the actual implementation and runner when available; do not build a duplicate scheduler merely to claim the contracts were tested.

Separate accepted design, reviewed case specification, and executed product validation. A design checkpoint can have planned executable checks; an implementation checkpoint cannot be called validated while applicable mandatory checks fail or remain unrun. Document/link checks, skill validation, walkthroughs, and primitive feasibility probes do not establish application acceptance. Keep expected answers and graders inaccessible to investigators and preserve the existing failure-maintenance process below.

### HLD checkpoint cases — 2026-10-19

These five focused groups refine existing engineering acceptance. They are prospective contract checks, not new incident families or regressions claimed from an executed product failure. Use controlled model/tool doubles and saved-state/trace assertions once the runtime exists. Human review additionally checks semantic quality. Test-local limits may exercise boundaries without selecting production execution values.

| Case ID | Refines existing coverage | Current execution status |
| --- | --- | --- |
| HLD-OWN-01 | Concurrent result merging; agent failure; retry/replay identity | Planned — runtime and test runner absent. |
| HLD-DEP-01 | Dependent dispatch; agent/tool failure; explicit unavailability | Planned — runtime and test runner absent. |
| HLD-FEED-01 | Structured role exchange; critic follow-up; hypothesis revision | Planned — runtime and test runner absent. |
| HLD-CLOSE-01 | Artifacts/outcomes; resource limits; unknown usage; recovery | Planned — runtime and test runner absent. |
| HLD-PROG-01 | No progress; resource limits; process restart and resume | Planned — runtime and test runner absent. |

#### HLD-OWN-01 — task ownership, merge, and replay

Setup: one run with independent Observability task T1 and Dependency task T2, separate eligible attempts, retrieved observations E1/E2, and fixed valid worker outputs.

- **Controls:** complete T1/T2 in both orders. Both findings/provenance survive; conflicting interpretations remain separate. Both tasks may cite one shared observation. Replay an identical accepted result: no duplicate finding or completion.
- **Adverse cases:** submit another owner's result, a foreign-run or invented citation, and changed content under an accepted result identity. Reject each without altering valid work. Time out an attempt and admit its eligible successor: late older findings cannot overwrite it, while reliable usage still counts for the original attempt.
- **Oracle:** accepted results match dispatched run/task/attempt/input identities; valid result sets survive either completion order. Rejections, attempts, and accounting remain auditable. This does not require identical future model decisions.

#### HLD-DEP-01 — settlement versus required inputs

Setup: T1/T2 are specialist prerequisites of Reasoning T3; Critic T4 requires T3's accepted revision; a target-specific query T5 requires a target from T2.

- **Controls:** T3 waits while either prerequisite is Pending/Running, then receives both valid results. T4 starts with the required accepted hypothesis revision. T5 starts when T2 succeeds and supplies its required target.
- **Failure boundaries:** T1 ends Failed after bounded handling and T2 succeeds: T3 receives the result and explicit failure. Repeat with both specialists Failed: T3 may return valid uncertainty within remaining limits, without invented observations. A transient tool error does not prematurely settle a still-running task.
- **Missing-input boundary:** T2 succeeds with target unknown, or the required result is Failed/Skipped. T5 is not dispatched; mark the never-attempted task Skipped with a reason. Orchestrator may choose another evidence question. Runtime success never fabricates an input or substitutes a later human grade.
- **Oracle:** dispatch traces satisfy outcome/input/revision conditions; unavailable evidence stays explicit; readiness does not bypass limits. Human review checks justified uncertainty without demanding a hidden exact cause from unavailable inputs.

#### HLD-FEED-01 — traceable objection dispositions

Setup: hypothesis H1 and Critic review C1 with identified objections. Exercise gathering, reanalysis, and unresolved dispositions using scripted role results.

- **Controls:** targeted gathering creates an identified specialist task whose outcome feeds a new Reasoning revision and Critic review. Reanalysis uses existing evidence but still receives a new review. An unresolved disposition retains its reason in Reporting.
- **Adverse cases:** omit a disposition, substitute a review of another hypothesis, or try to replace the reviewed diagnosis during Reporting. Reject structural/reference mismatches. Human review checks semantic drift and whether the response actually addresses the objection.
- **Oracle:** every objection traces to a disposition and assigned work where applicable; hypothesis/review/report references agree; earlier findings and objections survive. A valid review may retain objections; Reporting cannot claim they were resolved. Do not prescribe one exact tool-call sequence.

#### HLD-CLOSE-01 — closure, late results, and stop reasons

Setup: saved terminal tasks, a never-attempted Pending task, a Pending task awaiting retry, and a Running task. Control ordering around result acceptance and closure.

- **Controls:** acceptance before closure preserves the result. A final report finishing exactly at a configured cap remains complete if no required work is prevented. Process interruption instead reconciles/resumes with the same identities, configuration, and accounting.
- **Closure-first boundary:** preserve terminal work, mark only never-attempted work Skipped, and mark attempted unfinished work Failed with the closure reason. Late findings cannot change results. Retain reliable usage and unknown reservations without double charging on reconciliation; cancellation is not proof of zero external usage.
- **Outcome boundary:** a hard limit prevents required follow-up or Reporting even when no task is admissible or no-progress also applies: record resource-exhausted and a partial summary. Persistent critical-role failure and integrity errors stay explicit failures. Do not apply terminal closure to a merely interrupted run.
- **Oracle:** task states/results, stop reasons, artifacts, and accounting agree in both closure/acceptance orders. The partial summary needs no new model call and invents no diagnosis.

#### HLD-PROG-01 — retrieval change and durable counters

Setup: admitted rounds containing a new observation, a partial query becoming complete (including a complete empty result), repeated evidence, failed receipts, and pure reanalysis. Persist the round identity at admission.

- **Controls:** new observations or newly complete normalized-query coverage reset consecutive retrieval no-progress. Useful reanalysis is allowed within remaining limits but consumes round/call capacity without resetting retrieval progress.
- **Adverse cases:** duplicates, repeated complete coverage, failed receipts, new task IDs, and rewritten prose do not reset progress. At the threshold close gathering and report reviewed findings/gaps if required work fits. Hard limits bound superficially novel but useless queries; invalid plans and retries still consume calls.
- **Resume boundaries:** interrupt after admission and after a completed review, then resume repeatedly. Do not refund/reconsume admitted allowance or update progress twice for one completed review. Preserve unknown-usage reservations.
- **Oracle:** saved round identities, consumed allowances, progress updates, and stop behavior match those events across restart. Current normalization and runtime limits are documented in [tool contracts](tool-contracts.md) and [runtime contracts](lld.md); benchmark-specific verification remains pending.

### Task-level human review

Retain each reviewed task's run/task/result identity and concise reasons. Judge against its actual assignment, input revision, and evidence available during that task, including its own tool responses. Later evidence or private benchmark truth must not create impossible expectations for an earlier worker. Keep runtime completion/failure visible alongside semantic grades; a valid accepted result can receive a poor human grade.

| Dimension | Review question |
| --- | --- |
| Assignment fulfillment | Did the task answer the question or explain its limits, covering what available inputs reasonably supported? |
| Evidence support and contradictions | Do claims follow from retrieved evidence and address relevant contrary observations? |
| Uncertainty | Are facts, assumptions, unavailable inputs, and unresolved questions distinguished without unsupported certainty? |

Use pass/partial/fail per dimension with a reason. Grade coordination on assignments/dispositions; specialists on findings/gaps; Reasoning on supported comparisons; Critic on relevant challenges; Reporting on faithful synthesis and recommendations. Do not require every role to identify the final cause or combine task grades into an arbitrary weighted substitute for incident accuracy.

Rubric controls: a task accurately explaining unavailable inputs can pass; a structurally valid task asserting unsupported certainty fails grounding; missing available relevant evidence can be partial/fail on assignment coverage with a reason. These are review examples, not executed grades. No LLM judge or task-quality result is introduced here.


## Failure-driven evaluation maintenance

Use the project-local [incident-evals skill](../.agents/skills/incident-evals/SKILL.md) when a real investigation, test, fixture, or grader failure warrants evaluation work. Maintain evaluations separately from agent prompts and implementation logic, while running them alongside small implementation steps.

- Inspect the run's alert, configuration, tool results, trace, and grading evidence. Reproduce the failure and classify it as agent behavior, runtime/tool defect, fixture defect, grader defect, or provider/environment failure; distinguish an expected inconclusive outcome from a failure.
- Add or strengthen the smallest relevant deterministic check or incident variant. Reuse an existing case where it captures the failure; do not add a new incident for every ordinary code defect.
- Preserve the original failure, its classification, the changed test/case, and verification outcome. Correct demonstrably flawed fixtures or graders with a recorded reason and a new version; do not silently weaken expectations to obtain a pass.
- Keep the ten primary incidents and two variants identifiable. Add development regressions separately; record case exposure and suite versions so growing coverage does not silently alter the original comparison.
- Graders, expected diagnoses, and maintenance skills are developer/evaluator resources. Runtime investigators must not receive their contents or obtain repository access through tools.
- Keep the initial skill to one entrypoint. Add helpers or evaluation infrastructure only when repeated work justifies them; Harbor, containers, and an LLM judge are not prerequisites.

## Comparison protocol

Run the ten primary incidents and both variants against the single-agent baseline and the multi-agent system.

- Use the same underlying model/version and settings, registered tool implementations, visible observations, initial alert, cutoff, and per-run total limits. Compare system-wide budgets, not a full independent allowance for every specialist.
- The accepted design selects one iterative investigator for the baseline, reusing model/tool adapters, provenance validation, persistence/recovery, accounting, and output contracts. Use controlled runs to verify the same adapter settings, total allowances, retry accounting, and retrieved-evidence restrictions in both modes; baseline execution must not require the six-role chain. Record mode-specific task/round guards without granting extra per-role resource allowances.
- Reset per-run state and fault conditions. Each system makes its own queries; do not give either system the other one's collected evidence or answers.
- Select and record model/provider, settings, dataset/fixture/prompt/grader versions, actual budget values, run order, fault conditions, and repeat count before each evaluation batch. The CLI records current settings per batch; the matched benchmark configuration must be fixed separately before measurement. Freeze them within a batch and preserve all attempts.
- Preserve configuration, structured outputs, tool results, task events, and actual provider-reported usage. Record missing usage as unknown, not zero.
- Include failed attempts and retries in call/usage accounting. Keep wall-clock latency distinct from summed parallel task durations.
- Report the ten primary cases together and individually. Report R1 recovery/fault exercise and R2 uncertainty separately.
- Do not tune a prompt on a case and then describe that case as unseen. Record development exposure; with this small authored suite, claim a transparent comparative demo, not a production-generalization benchmark.
- Do not require or claim multi-agent superiority. Report the observed quality/cost tradeoff and failures honestly.

Initial measurements are exploratory. After reviewing them, agree and record a diagnostic acceptance threshold before subsequent validation runs. Do not retroactively label the exploratory results as passing a newly selected threshold or choose only favorable attempts. A new fixture, grader, prompt, model, or limit version must be identifiable in later results. Until a threshold is agreed and validated, report diagnostic quality as measured but not accepted. No 8/10 or 10/10 target is selected.

## Metrics and human rubric

Primary measurements:

- Root-cause accuracy: proportion of the ten primary cases whose top-ranked conclusion correctly localizes the fault and identifies the supported mechanism at the case-specific precision above. A broad family label alone is insufficient. Human review maps free-form conclusions to expectations without exposing those expectations to agents.
- Evidence grounding: human review of factual claims against retrieved evidence. Record supported, unsupported, and contradicted claims and whether important causal claims have citations. An empty report is not a grounded successful investigation.
- Citation validity: automated check that referenced IDs were actually retrieved before the cutoff. This is a provenance check, not a substitute for semantic grounding.
- Tool-call count: record attempted, failed, and successful calls, including retries.
- Latency: elapsed wall-clock time from starting an investigation to final report or termination.
- Tokens: actual input/output usage across every agent/model call, including retries and failed attempts where usage is available.
- Reliability: R1 fault-exercised/recovered status and R2 justified uncertainty, unsupported specificity, and bounded termination.

Human review records concise reasons, not just a total score:

| Dimension | Review question |
| --- | --- |
| Diagnosis | Is the supported mechanism correctly localized and ranked? |
| Support | Do the cited observations actually support the important claims? |
| Contradictions | Does the report address the case's important alternative explanations? |
| Uncertainty | Are facts, assumptions, missing evidence, and confidence distinguished? |
| Actions | Are recommendations tied to findings, with verification and no invented recovery? |

Use pass/partial/fail per dimension with an explanation. Compute the primary-case accuracy from the diagnosis decision; keep the other dimensions visible instead of hiding them inside an arbitrary weighted total. R2 is reviewed for appropriate uncertainty rather than exact hidden-cause accuracy.

No LLM judge, numerical confidence calibration claim, fixed minimum accuracy target, or benchmark result is introduced by this specification. All deterministic engineering checks remain mandatory; setting the diagnostic threshold after initial measurements is an explicit staged acceptance decision.

## Sources and limitations

- [OpenTelemetry Demo failure scenarios](https://opentelemetry.io/docs/demo/feature-flags/) provides concrete mechanisms such as bad dependency addresses, database contention, and memory growth. These cases are independently authored synthetic exercises, not reproductions of measured production incidents.
- [Google SRE: Effective Troubleshooting](https://sre.google/sre-book/effective-troubleshooting/) motivates testing explanations against conflicting observations rather than anchoring on historical failures or deployment timing.
- [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents) informs the separation of tasks, executions, and grading.

An authored expected cause is not proof that available evidence identifies it. Update a flawed fixture or its acceptable precision when review exposes ambiguity; never resolve it by handing the answer to the agent.
