# Design decisions

These decisions describe the implemented MVP and the tradeoffs it accepts.

## Frozen evidence before investigation

Investigators read a fixed snapshot through typed tools. A cutoff excludes records not yet available; a digest identifies the exact inputs. This makes runs inspectable and repeatable without giving an agent control of a live service. Production AI investigation tools test changes the same way, against incidents frozen in time [4]. Empty or unavailable retrieval is a coverage gap, not evidence that a service is healthy. Negative results are still reported, not discarded [5].

## Six roles with a bounded review loop

Separate collection, reasoning, critique and reporting. An Orchestrator leads and delegates to specialists, the orchestrator-worker pattern [2]. Reasoning proposes hypotheses to test, as in SRE troubleshooting [5]. Two specialists gather evidence concurrently, as independent subtasks run in parallel [1]. The critic follows the evaluator-optimizer pattern, where one model call evaluates another's output [1], and tries to disprove the leading hypothesis [4]. The next stage waits for both specialists. This simplifies dependencies and preserves independent findings, at the cost of waiting for the slower worker and making more model calls. Synchronous waits and higher token use are known costs of multi-agent systems [2]. Agentic systems trade latency and cost for task performance [1], so the upcoming matched single-agent comparison must measure whether that tradeoff pays off.

## Staged is the only workflow

Remove the deprecated hub graph and its launch option. The earlier five-fault comparison tied at **4/5 for each workflow** after rerunning budget-blocked attempts; it did not establish a diagnostic advantage for retaining both. This is historical removal evidence, not a clean first-attempt benchmark or a multi-agent versus single-agent result. Keeping one staged workflow makes plan, gather, reason, critique and report visible as graph nodes and avoids maintaining duplicate routing. Historical artifact labels remain readable for grading and review; the old workflow cannot be launched or resumed at this version.

## Models propose; runtime owns state

Models return typed actions and role payloads. Runtime allocates identities, checks role permissions and evidence references, accepts receipts and controls budgets. Parallel workers never replace shared control state. Every claim must link back to retrieved evidence [4]. Provenance validation proves a citation was retrieved, not that its interpretation is true.

## Repair small deviations and retain uncertainty

Normalize harmless formatting and identity slips, log every repair and strictly validate the result. Repairs must not manufacture source evidence. A bounded correction turn handles unusable output. A likely-cause claim without observation support becomes inconclusive; open objections and missing evidence remain in the report. A trustworthy investigator says when it is not sure [4].

## Discover data and reuse queries

A catalog exposes actual metric names, services and log templates. Aggregation helpers make large snapshots usable within small contexts. Identical normalized queries reuse saved receipts without another tool charge. Changed prose and duplicate results do not count as progress.

## SQLite checkpoints are recovery authority

Persist admission before external calls, save worker responses and accept them at separate durable boundaries. Resume reuses saved completions and keeps unresolved calls charged. It verifies compatibility before writes, retains the original deadline and uses a per-run kernel lock. A new process can recover after process death; exactly-once provider execution and recovery across changed runtime versions are outside the contract.

## Limits belong to code

Long agent runs need mitigations, because minor system failures can be catastrophic [2]. Keep finite run/task call allowances, per-call timeouts, wall deadlines, retries and review rounds. Reserve calls before parallel dispatch. Hard exhaustion keeps partial artifacts. The live profile gives providers longer timeouts; it does not remove bounds. Unknown usage remains unknown even when admission conservatively retains a reserve.

## Separate evaluation from runtime

Expectations, human notes and grading logic remain outside prompts and tool-visible sources. Grade final reports against culprit, mechanism and key evidence; record process quality separately. Grading the output rather than the path is the usual advice for agents [3]. Keep failures in denominators and preserve every attempt. Citation checks need human semantic review. No fixed diagnostic acceptance threshold or multi-agent superiority claim is selected.

## Narrow source and storage interfaces

Use source functions behind a typed registry and artifact/run/catalog repositories behind the console. This is the ports-and-adapters (hexagonal) pattern: the core is tested in isolation from its data sources [6]. Local JSONL and filesystem adapters serve the MVP. Lab log records carry severity, body, trace ID and attributes, and metric records carry name, value, unit and aggregation, following the OpenTelemetry logs [7] and metrics [8] data models. Lab faults are switched on at runtime, like the OpenTelemetry demo's feature flags [9]. New telemetry and object-storage adapters can implement those boundaries later; they are not shipped integrations.

## A local console with an optional Phoenix stack

Use server-rendered pages and small JavaScript files, without a frontend build or account. Launch one bounded subprocess job at a time, with explicit confirmation for provider calls. Every run writes its local trace, because agent quality is judged by reading transcripts from many trials [3]. Export to the Phoenix stack is opt-in and loopback-only. Local artifacts, including the trace, stay complete when the Phoenix stack is off or unavailable.

## References

1. Anthropic, 2024. [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents). Describes parallel sectioning into independent subtasks, the evaluator-optimizer workflow, and the latency and cost tradeoff of agentic systems.
2. Anthropic, 2025. [How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system). Reports that multi-agent systems use about 15 times more tokens than chats, and that synchronous execution can block the system while it waits for one subagent.
3. Anthropic, 2026. [Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents). Advises grading what the agent produced, not the path it took, and reading transcripts and grades from many trials.
4. incident.io. [Building Investigations: what it takes to build an AI SRE](https://incident.io/blog/building-investigations-what-it-takes-to-build-an-ai-sre). Tests changes against incidents frozen in time, uses an adversarial agent to disprove hypotheses, and links every claim to its evidence.
5. Google. [Site Reliability Engineering, Chapter 12: Effective Troubleshooting](https://sre.google/sre-book/effective-troubleshooting/). Describes iteratively hypothesizing and testing causes, warns that correlation is not causation, and says negative results should not be ignored.
6. Alistair Cockburn. [Hexagonal Architecture](https://alistair.cockburn.us/hexagonal-architecture/). Ports and adapters let an application be tested in isolation from its run-time devices and databases.
7. OpenTelemetry. [Logs data model](https://opentelemetry.io/docs/specs/otel/logs/data-model/). Defines log record fields such as timestamp, trace ID, severity, body and attributes.
8. OpenTelemetry. [Metrics data model](https://opentelemetry.io/docs/specs/otel/metrics/data-model/). Defines metric streams with a name, unit, attributes and aggregation properties.
9. OpenTelemetry. [Demo feature flags](https://opentelemetry.io/docs/demo/feature-flags/). Uses runtime feature flags to simulate failure scenarios in the demo application.
