# Product requirements: incident investigation MVP

Status: PRD finalized on 2026-10-19. Core HLD selects Python + LangGraph + local SQLite. [LLD-01](lld.md) specifies common state and one specialist lifecycle; [LLD-02](tool-contracts.md) specifies frozen JSONL retrieval/page/fault contracts and controlled local settings. Dependency pins are adopted in `uv.lock` after resolution succeeded; other role/coordination contracts, real-model settings/spending cap, and product validation remain outstanding. Functional investigation is the priority; the HLD delivery gap register retains supporting work with completion triggers. Probes/documentation checks do not establish application acceptance.

## Goal and boundaries

Build the Multi-Agent Production Incident Investigation System described in the requirements document: investigate an incident using simulated operational evidence, compare explanations, and recommend evidence-backed corrective actions.

The primary user is the developer/operator running and inspecting local investigations. Assignment reviewers must also be able to reproduce runs and inspect their evidence. The goal is a reliable investigation system. The required single-agent comparison measures quality and resource tradeoffs; demonstrating multi-agent superiority is not a success condition.

The represented application is one fictional e-commerce system: Checkout calls Inventory, and Inventory uses a database. Those components exist as scenario observations, not running services.

- Use local, realistic synthetic data behind working retrieval tools.
- Start an investigation with the affected service, observed symptoms, and a time window. Do not supply a suspected cause or preassembled evidence bundle.
- Define an observation cutoff for each run. Earlier history and baseline measurements may be queried; future observations and the current incident's eventual postmortem may not.
- Recommend actions and verification steps; do not execute production changes or pretend a recommendation has already restored service.
- A live app, polished frontend, queues, Kubernetes, dashboards, and CI servers are not MVP prerequisites.
- An HTTP API, background supervision while specialists remain active, and distributed workers are deferred. Parallel investigation rounds and concurrent independent investigations are MVP requirements.
- Prefer open-source/free components. An external LLM API may receive synthetic data; provider, model, and spending cap still need selection.

The requirements document controls scope.

## Local user flow and artifacts

The MVP provides a local CLI with these capabilities. Exact command names, flags, serialization schemas, and directory layout belong to LLD.

| User action | Required behavior |
| --- | --- |
| Run an incident | Select an authored case and the single-agent or multi-agent mode. Validate the alert and finite execution configuration before starting model work. |
| Run multiple incidents | Allow at least two independent investigations to progress concurrently in the local runner, within a shared concurrency limit. |
| Inspect progress or results | Show the investigation identity, saved progress, current or last recorded execution status, available artifacts, resource usage, and any missing evidence. Inspection must not modify an active investigation. |
| Resume interrupted work | Continue the same investigation from durable completed progress, retaining its evidence, configuration, fault state, and consumed budgets. Work interrupted before its result was saved may repeat. |
| Evaluate | Run a selected evaluation batch, preserve every attempt, and produce per-case and aggregate results under the comparison protocol. |

Each fresh investigation receives a distinct run identity, including repeated runs of the same case. The initial agent input contains only the opaque case ID, affected service, observed symptoms, observation window, and cutoff. Other evidence is retrieved through tools.

Save a readable Markdown report, structured JSON results and run configuration, and an execution trace with task/tool events and available model usage. Preserve tool inputs/results and evidence references needed to audit conclusions. Evaluation expectations and grading outputs stay outside runtime retrieval sources. An interrupted or failed run must retain its already saved artifacts; it must not be presented as a completed report.

Reports contain ranked hypotheses, supporting and contradicting evidence, confidence, facts separated from assumptions, missing observations, recommended actions, verification steps, and unresolved questions. Cite evidence actually retrieved by that investigation. An inconclusive exact cause is valid when the observations do not distinguish alternatives.

## Required investigation behavior

Cover all six named roles, even though the assignment's minimum is five agents:

- Orchestrator: plan and coordinate work.
- Observability: investigate logs and metrics.
- Dependency: examine releases, configuration, and dependencies.
- Reasoning: develop and compare competing explanations.
- Critic: challenge conclusions and request useful follow-up evidence.
- Reporting: produce the final report and recommendations.

Agents exchange structured results and genuinely collaborate. A predetermined chain of differently prompted LLM calls is insufficient.

The orchestrator assigns explicit investigation questions to the six fixed roles and chooses targeted follow-up from findings and critic objections. The runtime validates assignments and enforces task dependencies, execution limits, and scheduling. The Reasoning Agent compares hypotheses; the orchestrator coordinates the work needed to assess them.

- Dispatch independent specialist tasks concurrently. For the MVP, the supervisor may wait for the current round to complete before planning the next round.
- Execute independent tool requests concurrently when a specialist issues them together. A request that needs an earlier result must wait for that result.
- Give tasks explicit inputs and identified results so specialist logic can later be reused behind background dispatch. Background dispatch and its completion scheduler are not MVP implementation work.
- Respect dependencies: reasoning and review must use the evidence and findings required for their task, rather than silently racing unfinished prerequisites.
- Turn critic objections into targeted evidence gathering, hypothesis revision, or an explicit unresolved issue. A feedback loop must be demonstrable; repeating a fixed prompt chain is insufficient.
- Preserve retrieved observations while allowing conclusions to change as more evidence is gathered. Stable task/evidence identities are required; a separate mutable lifecycle or global deduplication system for individual findings is not required. Detailed representation remains HLD/LLD work.
- Keep simultaneous investigations isolated: evidence, hypotheses, task status, fault counters, artifacts, and budgets must not cross between runs. Two modifying top-level invocations of the same investigation must not execute concurrently; reject the second clearly. Parallel branches inside one invocation remain allowed.
- Merge concurrent findings without dropping another task's result or relying on completion order. State ownership and merge implementation will be specified in HLD/LLD.

## Reliability and resource requirements

- Persist completed progress locally and provide process-restart recovery. A saved report alone is not a resumable investigation checkpoint. HLD selects LangGraph's asynchronous SQLite checkpointer, synchronous durability, and one database per investigation; exact contracts and executable integration checks follow in LLD.
- Make repeated work safe: retain stable task/evidence identities and avoid duplicate committed results on retry/resume. Trace and account for repeated external calls as separate attempts; do not claim exactly-once LLM execution.
- Preserve completed independent work when an agent or tool fails. Retry eligible transient failures within configured limits; treat unavailable/partial evidence explicitly and allow degraded investigation when useful evidence remains.
- Apply finite task/tool deadlines, retry limits, per-run call/token budgets, and a shared concurrency limit across investigations in the local runner. Detect repeated work that produces no useful progress and terminate the loop. Prompts alone do not enforce these requirements.
- Reserve execution capacity before parallel work begins so concurrent tasks cannot each spend the full remaining allowance. Exact accounting, no-progress rules, and limit values are HLD/LLD decisions, fixed before model runs.
- Preserve configured limits and consumed budgets across resume. Missing provider usage is unknown, not zero. Include failed attempts and retries in usage accounting where usage is available.
- Confirmed recovery policy: when a model attempt may have completed but its usage was not saved, retain its full reserved allowance and continue only within the original remaining limits. A retry receives a new attempt identity and allowance. Stop as resource-exhausted if no admissible allowance remains; routine resume does not require manual review of each unknown attempt.
- Report measured usage separately from active or unresolved reservations. A conservative reservation used for admission is not provider-reported consumption; unknown usage remains explicitly unknown. Validate reservation bounds against the selected provider/model before real model runs.
- Record the dataset, prompt, tool-contract, runtime, and checkpoint-schema versions associated with a run. Before resume, detect incompatible changes and refuse clearly while preserving saved progress; do not silently mix evidence or execution rules from different versions. Compatible resume must retain the original run configuration and source observations. Version identification and compatibility checks belong to HLD/LLD; automatic migration is not required.
- Distinguish successful completion, justified inconclusive diagnosis, interruption, resource exhaustion, and unrecoverable failure. Preserve the stop reason and available evidence; avoid claiming that a runtime failure established a root cause. The exact status schema belongs to LLD.
- Use task/tool traces to explain assignments, observable decisions, evidence retrieval, failures, retries, and resource usage. Measure elapsed run latency separately from summed durations of parallel tasks. Keep synthetic source data, run settings, and fault schedules reproducible; model outputs may vary.

## Tool boundary and evidence

Implement five tool categories: logs, metrics, deployment/configuration history, service dependencies, and historical incidents.

- Agents request information through structured tool inputs. Tools own source-specific querying and normalize their output.
- Query the observations by supported parameters, including service and time; retain version, operation, and measurement-window context where relevant.
- Give evidence stable IDs, source timestamps, and provenance so claims can be checked against what the agent actually retrieved.
- Distinguish complete empty results, partial results, and failed requests. Missing data is not a healthy measurement.
- Return observations and change records, not prepared diagnoses or instructions directing the investigation.
- Keep all evaluator expectations outside the tool-visible sources. Runtime agents must not receive repository, shell, or evaluator-file access.
- Begin with simple structured functions. Ports & Adapters is the boundary principle; a Strategy, plugin framework, MCP server, and additional storage adapters are deferred until needed.

[LLD-02](tool-contracts.md) specifies tool signatures, frozen JSONL source storage, serialization, pagination, and controlled-test limits; [LLD-01](lld.md) specifies the lifecycle boundary. Those contracts are not implemented or integration-validated. Real-model and full-loop settings remain open in the [LLD handoff](lld.md#8-remaining-contracts-and-decision-gates). Do not implement a provider abstraction framework merely to preserve this boundary.

## Incident coverage

The dataset specification defines ten distinct incidents across five failure families:

| ID | Family | Investigation focus |
| --- | --- | --- |
| 01 | Bad configuration | Caller-side dependency addressing versus dependency outage |
| 02 | Bad configuration | Caller deadline versus actual dependency performance |
| 03 | Deployment regression | Expensive new query versus general database saturation |
| 04 | Deployment regression | Response handling versus a failing dependency |
| 05 | Database contention | Blocking transaction versus unrelated deployment |
| 06 | Database contention | Conflicting lock order versus capacity shortage |
| 07 | Resource exhaustion | Inventory connection ownership versus database capacity |
| 08 | Resource exhaustion | Memory growth, process restarts, and incomplete measurements |
| 09 | Traffic overload | Brief saturation hidden by a wider measurement window |
| 10 | Traffic overload | Retry-generated traffic versus external demand |

Two additional reliability variants test transient tool recovery and appropriate uncertainty when decisive evidence is unavailable. They do not count toward the ten distinct incidents.

Detailed source observations, expected diagnostic precision, contradictions, and grading notes are in the human/evaluator-only [evaluation specification](evaluation-plan.md). Neither document is an agent prompt or retrieval source. Case IDs passed to agents must not encode the cause.

## Acceptance and evaluation

Use automated checks plus human review; an LLM judge is deferred.

- Validate coherent timelines, measurement units/windows, version-weighted rates, and source consistency.
- Test all five retrieval categories, supported filters, provenance, completeness states, and cutoff enforcement.
- Verify that report citations refer to observations actually retrieved in that run, not just IDs that exist somewhere in the dataset.
- Human-review whether claims follow from evidence, important contradictions are addressed, uncertainty is justified, and actions are appropriate.
- Test bounded failure recovery, durable resume, task dependencies, both levels of parallel execution, concurrent investigation isolation, safe result merging, critic-requested follow-up, and termination after the runtime has been designed.
- Compare single-agent and multi-agent systems on the same observations and tools, with the same model settings, total per-run limits, and resettable fault conditions.
- Measure root-cause accuracy, evidence grounding, tool calls, wall-clock latency, and actual token usage, including failed attempts and retries. Report the observed quality/cost tradeoff honestly.

Engineering acceptance requires all deterministic reliability checks to pass and demonstrated critic-driven investigation, transient-failure recovery, and justified uncertainty. A passing engineering suite does not by itself establish diagnostic quality. Runtime scenarios and the failure-driven regression process are detailed in the [evaluation specification](evaluation-plan.md).

The diagnostic acceptance threshold will be set after inspecting initial measurements, as agreed on 2026-10-19. Initial measurements are exploratory. Record the chosen target before subsequent validation runs, preserve all results, and do not choose a favorable attempt or silently lower the target. Diagnostic readiness remains unestablished until that target is agreed and validated. No 8/10 or 10/10 target is currently selected.

The ten primary incidents form the diagnostic comparison. Report recovery and insufficient-evidence variants separately rather than obscuring their purpose in one accuracy number. Fix dataset versions, settings, execution budgets, fault conditions, and repeat counts before each evaluation batch. Keep added regressions identifiable so changing suite composition does not silently change the original comparison.

Evaluation grows through the project-local [incident-evals skill](../.agents/skills/incident-evals/SKILL.md), invoked at major behavioral-design and implementation checkpoints by `AGENTS.md` and supplemented by the [checkpoint process](evaluation-plan.md#checkpoint-validation-process). Save decisions first, then add/strengthen focused coverage with controls and observable outcomes. Runtime checks are planned while their implementation is absent; applicable mandatory checks must pass before an implementation milestone is called validated. Human task grading uses assignment fulfillment, grounding/contradictions, and uncertainty, separately from runtime status and incident accuracy. Turn observed failures into small regressions, retain original evidence and correction reasons, and keep expected answers evaluator-only. No new evaluation framework or LLM judge is required.

## Architecture checkpoint and LLD handoff

The [critical review](prd-review.md) checks assignment coverage and records the resume-compatibility clarification. The [HLD](hld.md) is complete as accepted design: HLD-01 ownership, HLD-02 bounded coordination, and HLD-03 runtime/persistence mapping. Python + LangGraph + local SQLite are selected. Runtime owns commits; explicit call boundaries preserve reservations, receipts, and accepted results. Required inputs, objection dispositions, closure eligibility, and durable round/progress rules remain unchanged. Human task-quality review stays separate from runtime success and incident accuracy.

Useful inputs: coordinator/specialist separation, shared session state, tools isolated from agents, agent/tool tracing, token/cost accounting, and an evaluation boundary.


The accepted architecture and transcript checkpoint were saved before focused evaluation updates. LLD-01 specifies common state/task/result records, identity merges, checkpoint/replay transitions, and inspection/export boundaries for one specialist lifecycle. LLD-02 specifies retrieval/source/page/fault contracts and numeric controlled-test settings. The [remaining-contracts handoff](lld.md#8-remaining-contracts-and-decision-gates) is the current index for other role payloads, coordination records, limits, public interfaces, model settings, and evaluation configuration.

Historical note: the initial LLD-02 resolver attempt encountered a PyPI DNS failure; later resolution succeeded and dependency pins are adopted in `uv.lock`. Graph/checkpointer integration validation remains pending. Other contracts close before their affected behavior; provider/model and validated reservation/spending bounds precede real-model calls. Benchmark repeat count, configuration, and harness precede comparison batches, not unrelated lifecycle work with doubles. Minimal traces/guardrails accompany execution; richer observability remains deferred until the system works. Keep [gap completion triggers](hld.md#delivery-gap-register) visible. Workspace procedures are outside this product-design handoff.

## Implementation aids and source lessons

Select tools by actual content, project fit, current API compatibility, maintenance evidence, and dependencies. Popularity is not a selection criterion. The following entrypoints were reviewed on 2026-10-19; they are shortlisted, not installed or blanket-endorsed:

| Candidate | Useful content | Adoption boundary |
| --- | --- | --- |
| [Official LangGraph fundamentals skill](https://github.com/langchain-ai/langchain-skills/blob/main/config/skills/langgraph-fundamentals/SKILL.md) | State updates, routing, `Send`, reducers, and static-edge/`Command` interactions. | Check the relevant language reference against the selected package version and official docs before use. |
| [Official LangGraph persistence skill](https://github.com/langchain-ai/langchain-skills/blob/main/config/skills/langgraph-persistence/SKILL.md) | Thread isolation, checkpoint scope, resume, and parallel-subgraph namespace conflicts. | Select only the storage and subgraph features our design needs. |
| [Official eval-engineering skill](https://github.com/langchain-ai/langchain-skills/blob/main/config/skills/eval-engineering/SKILL.md) | Task design and failure classification. | Its Harbor-based workflow exceeds the initial requirement; use the small project-local maintenance skill instead. |

The upstream skills repository identifies itself as early development. Review exact content and pin the adopted revision during later setup. One example of why source inspection matters: the fundamentals skill's last-write-wins shorthand does not describe concurrent same-field writes without a reducer, which can raise an error. [Official concurrent-update documentation](https://docs.langchain.com/oss/python/langgraph/errors/INVALID_CONCURRENT_GRAPH_UPDATE)

Keep lessons actionable: source -> observed problem -> design rule -> verification.

| Source and problem | Project rule | Verification |
| --- | --- | --- |
| [Anthropic research system](https://www.anthropic.com/engineering/multi-agent-research-system): vague delegation caused duplicate searches; full restarts wasted completed work; synchronous groups simplified coordination but delayed information flow. | Assign explicit task boundaries, run independent work concurrently, and recover saved progress within shared budgets. HLD-02 accepts bounded batch waits with explicit dependency outcomes and deadlines. Its reported speedup is not our performance target. | Inspect assignment traces, overlapping task intervals, delayed dependent dispatch, bounded waits after worker failure, budget enforcement, and restart/resume behavior. |
| [LangGraph runtime](https://docs.langchain.com/oss/python/langgraph/pregel): work is parallel within steps, with updates visible at the next step. | Distinguish parallel rounds from background supervision; preserve explicit dependencies. An async invocation alone does not remove graph synchronization. | Demonstrate overlapping independent work and correctly delayed dependent work. |
| [LangGraph concurrent updates](https://docs.langchain.com/oss/python/langgraph/errors/INVALID_CONCURRENT_GRAPH_UPDATE): concurrent writes need a defined merge. | Return validated partial updates and define ownership/merge behavior. | Vary completion order and verify that every task's findings survive. |
| [Airflow trigger rules](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dags.html#trigger-rules): requiring all upstream successes differs from waiting for all outcomes. | Use settlement joins for partial-evidence reasoning; require accepted results and the specific needed inputs for dependent work. The input-availability check is our project rule. | HLD-DEP-01 covers pending/failure outcomes and a valid result with a missing target. |
| [Python timeout documentation](https://docs.python.org/3/library/asyncio-task.html#asyncio.wait_for): waiting for cancellation can exceed the requested timeout. | Give terminal closure a durable result-eligibility boundary; request cancellation without assuming zero remote work or usage. | HLD-CLOSE-01 checks both closure/acceptance orders and late-call accounting. |
| [Anthropic eval guidance](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents): shared trial state and grader/environment defects can distort scores. | Isolate runs, inspect traces, and classify failures before changing conclusions or graders. | Reset fault state; reproduce defects; retain failures and version corrected fixtures/graders. |
| [incident.io investigation engineering](https://incident.io/blog/building-investigations-what-it-takes-to-build-an-ai-sre): narrative findings hallucinated and old findings encouraged anchoring; hypothesis backtesting compares with responder outcomes. | Separate source observations from interpretations and permit reviewed revision. Task-level human grading is our adaptation, separate from runtime status and incident accuracy; the article does not establish human grading of every internal task. | Preserve earlier results and review evidence-driven changes. Human task review checks assignment fulfillment, grounding/contradictions, and uncertainty. |
| [Claude session recovery](https://code.claude.com/docs/en/sessions#what-a-resumed-session-restores): interrupted tool work is distinct from completed work; active-goal spend baselines can reset. | Record attempt outcomes explicitly and preserve our investigation budgets across resume. Missing usage retains its reservation. | Interrupt before/after a call and confirm unknown status, bounded retries, and retained allowance. |
| [Codex app-server lifecycle](https://learn.chatgpt.com/docs/app-server): sessions retain identified turns/items and can be reopened. | Use stable execution identities and saved results; conversation continuity does not establish exactly-once external execution. | Reapply a saved result without a new call; account for repeated calls under separate attempt identities. |
| [LangGraph checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers): successful node writes and full checkpoints differ; calls hidden inside a node lack separate durability. | HLD-03 uses synchronous admission checkpoints, explicit call-node receipts, later acceptance, and invocation-bound replay guards. | Kill at call/commit boundaries and fail a sibling; reuse saved responses and require new identities/reserves for unknown retries. |
| [SQLite saver source](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/aio.py): ordinary saver reads can invoke setup. | Check compatibility before setup/migration and provide a genuinely read-only inspection path. | Refuse changed versions before writes/calls; inspect an active run without setup, migration, or state changes. |
| [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html): interrupted related writes require an atomic boundary. | Accept a result/evidence references and task completion atomically in the selected checkpoint store; traces are derived artifacts. | Kill during acceptance and verify consistent recovery from the saved response. |
| [Pydantic JSON Schema](https://pydantic.dev/docs/validation/latest/concepts/json_schema/) and [native typed tools](https://docs.langchain.com/oss/python/langchain/tools): duplicated handwritten schemas/argument handling can drift. | LLD-02 reuses validation models, generated schemas, native typed wrappers, and a small function registry. A shared fault wrapper is the only new Decorator-style behavior; source adapters remain simple. | Check exposed schemas and actual accepted/rejected calls together; graph nodes still enforce one admitted operation per call. Planned until implementation exists. |
| [OpenTelemetry log model](https://opentelemetry.io/docs/specs/otel/logs/data-model/) and [metric model](https://opentelemetry.io/docs/specs/otel/metrics/data-model/): event/observation time and measurement context are distinct. | LLD-02 retains event versus source-availability time and metric units/aggregation/windows. Its small synthetic payload does not claim full OTLP compatibility or require an OTel deployment. | Backdated-but-later-available observations remain hidden; metric windows must fit the requested interval and cutoff. Planned source/tool checks. |

## Deliverables and current state

Required outputs: runnable implementation/setup, architecture diagram and coordination explanation, synthetic dataset and automated tests, single-versus-multi evaluation results, and a short technical note.

Completed (checkpoint 2026-10-19): PRD, HLD and LLD documents; the fault-injection lab with five fault families; five frozen evidence tools plus catalog and aggregation helpers; the six-role investigation on the staged LangGraph graph (default) with the deprecated hub graph behind `--graph hub`; the OpenRouter adapter with repair of harmless deviations, query de-duplication and per-call timeouts; the automatic grader, `eval` and `eval-suite` with pass@k/pass^k, quick mode, concurrency and an accurate budget cap; and 210 automated tests.

In testing: the local eval console with live Arize Phoenix traces.

Not completed: M4 reliability hardening (public resume, crash/replay, tool-failure recovery), the ten-incident benchmark with two reliability variants, the single-agent baseline, the single-versus-multi comparison, pass@4 measurement, and a validated diagnostic threshold. Results so far are exploratory (see the README).

## Design evidence

- [Ports & Adapters](https://alistair.cockburn.us/hexagonal-architecture/): external-system coupling prevents isolated testing; a stable application boundary permits real and simulated sources without rewriting core logic.
- [Google SRE: Effective Troubleshooting](https://sre.google/sre-book/effective-troubleshooting/): plausible correlations and previous failures can mislead investigators; compare explanations against both supporting and disconfirming observations.
- [OpenTelemetry Demo failure scenarios](https://opentelemetry.io/docs/demo/feature-flags/): controlled faults make diagnostic behavior repeatable. Borrow failure mechanisms, not the demo's deployment footprint.
- [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents): separate tasks, executions, and grading; use code checks for objective properties and human review for evidentiary judgment.
- [Multi-Agent research system](https://www.anthropic.com/engineering/multi-agent-research-system): Specialized agents can investigate different aspects of a problem in parallel, while a lead agent consolidates findings into a coherent conclusion.
