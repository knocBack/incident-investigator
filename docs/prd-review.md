# PRD critical review — 2026-10-19

Original PRD verdict (historical): ready to enter HLD after one narrow recovery clarification, saved in the PRD and evaluation specification. No missing core assignment requirement was found. This is a document review, not evidence of working software.

Current disposition, 2026-10-19: HLD-01/02/03 are accepted in the [HLD](hld.md). R-02 retains supporting-path gaps with completion triggers. [LLD-01](lld.md) specifies common state and one specialist lifecycle; [LLD-02](tool-contracts.md) specifies frozen JSONL retrieval/page/fault contracts and a concrete local double profile. Dependency pins are adopted in `uv.lock` after resolution succeeded; graph/checkpointer integration, remaining contracts, real-model settings, and product validation remain pending. The [LLD handoff](lld.md#8-remaining-contracts-and-decision-gates) consolidates remaining decisions. Requirement coverage remains valid; no application or benchmark validation is implied.

## What was checked

Read [the requirements document](requirements.md) as the scope authority, then compared `mvp-plan.md`, `evaluation-plan.md`, and the project checkpoint.

| Assignment requirement | Where the PRD covers it | Finding |
| --- | --- | --- |
| At least five agents; six roles named | Required investigation behavior | All six covered; structured collaboration and critic-driven follow-up required. |
| Five structured tools; failures and incomplete data | Tool boundary and evidence; evaluation variants | Retrieval, filters, errors, partial results, and unavailable observations covered. |
| Parallel tasks, shared state, dependencies, feedback | Required investigation behavior | Covered; HLD-01/02 and LLD-01 now specify ownership/merge and dependency rules. Runtime integration remains unvalidated. |
| Bounded execution and agent/tool recovery | Reliability and resource requirements | Covered; resume compatibility needed the clarification below. |
| Ranked, grounded reports; confidence and uncertainty | Local user flow; acceptance and evaluation | Covered; provenance checks and human semantic review are distinct. |
| Ten incidents; single-agent comparison; five metrics | Incident coverage; acceptance; evaluation specification | Covered; ten primary incidents and two separate variants. No results exist. |
| Runnable setup, diagram, tests, comparison, note, transcript | Deliverables and current state | All retained, including the 500-word note limit and full assistant transcript. |

## R-01: incompatible resume was not explicit

Severity: material to recovery correctness; small to specify.

Before: the PRD preserved configuration, evidence, budgets, and faults, but did not explicitly say what happens if files or execution contracts change between interruption and resume. Evaluation-batch versioning alone does not protect an ordinary resumed run.

Failure example: an investigation saves evidence from dataset revision A; its process stops; a developer edits that dataset or changes the meaning of a tool field; resume adds revision-B observations under the same run. A stable record ID alone cannot prove that its content stayed the same.

Resolution saved: associate each run with dataset, prompt, tool-contract, runtime, and checkpoint-schema versions. Detect incompatible resume and refuse before new investigation work, preserving the checkpoint. A compatible resume retains original observations and configuration. Automatic migration is unnecessary for the MVP. The exact identity and compatibility mechanism remains HLD/LLD.

Verification specified: a new deterministic acceptance row in `evaluation-plan.md`. The check is specified, not implemented or executed. The subsequent LLD-01 `Versions` record/exact-match preflight and LLD-02 snapshot digest now specify the identity/compatibility mechanism; executable integration remains pending.

## Open work that belongs after the PRD

Use the [remaining-contracts handoff](lld.md#8-remaining-contracts-and-decision-gates) for accepted sources, exact open items, completion gates, and existing verification groups. Common lifecycle/source contracts and the human rubrics already exist; other role/coordination schemas, full-loop settings, public operation contracts, dependency adoption, real-model bounds, and evaluation execution configuration remain open. The complete LLD is not yet specified.

Diagnostic-target selection remains after exploratory measurements and before later validation. Workspace procedures are outside this product-design handoff.

Two remaining risks deserve attention without expanding scope: six roles plus a single-agent baseline can consume substantial model budget; durable concurrent recovery is harder than the happy-path agent graph. Small vertical implementation steps and deterministic runtime checks are already required to address these. No live services or new framework are needed to close the PRD.

## HLD checkpoint completed

HLD-01 accepts one investigation record whose task-scoped contributions are validated and committed by runtime code. HLD-02 accepts bounded collaboration, dependencies, critic feedback, closure, and progress accounting. HLD-03 maps these rules to serialized control transitions, bounded parallel call nodes, and synchronous SQLite-backed graph persistence, with the same infrastructure reused by the single-agent baseline.

LLD-01/02 are specified. Dependency pins are adopted in `uv.lock`; focused real integration checks remain the first product-validation gate for one specialist lifecycle. Other contracts close before the affected behavior; benchmark configuration/harness precede comparison batches. Historical note: the initial LLD-02 preflight encountered a PyPI DNS failure before later resolution succeeded. All product reliability groups remain planned; this documentation reconciliation does not implement the application.

## R-02: supporting HLD paths were under-specified

The initial HLD review called the design ready too broadly. Requirement mentions and evaluation policy did not fully explain how evaluation batches invoke the system, how incident evidence stays fixed across retests, or where system telemetry and guardrail enforcement originate. The user raised these gaps, then clarified that functional investigation takes priority over completing every supporting feature first.

Resolution: retain the core architecture and use [GAP-DATA/OBS/GUARD/EVAL](hld.md#delivery-gap-register) with explicit completion triggers. Fixed observations/cutoff precede working retrieval; minimum tracing/guardrails accompany execution; the harness precedes comparisons. CPU/memory sampling, dashboards, and richer telemetry remain deferred until the system works. LLD-01 supplies common records and enforcement boundaries, not a claim that those features are implemented.

Verification: prospective contract extensions reuse existing HLD evaluation groups. Preserve the ten primary incidents, two variants, private truth boundary, and diagnostic-threshold policy. Execution remains pending because runtime/tools/runner are absent; no reproduced failure or changed fixture/grader is claimed.

## Verified external example

Anthropic describes stateful agents being disrupted by code changes and the need to preserve ongoing work across failures. Our much smaller local implication is to reject incompatible resume rather than introduce deployment machinery. The same article describes vague delegation producing duplicated research; explicit task scope addresses that coordination failure. [Anthropic research-system engineering account](https://www.anthropic.com/engineering/multi-agent-research-system)
