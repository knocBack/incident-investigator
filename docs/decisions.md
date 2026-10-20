# Design decisions

These decisions describe the implemented MVP and the tradeoffs it accepts.

## Frozen evidence before investigation

Investigators read a fixed snapshot through typed tools. A cutoff excludes records not yet available; a digest identifies the exact inputs. This makes runs inspectable and repeatable without giving an agent control of a live service. Empty or unavailable retrieval is a coverage gap, not evidence that a service is healthy.

## Six roles with a bounded review loop

Separate collection, reasoning, critique and reporting. Two specialists gather evidence concurrently; the next stage waits for their outcomes. This simplifies dependencies and preserves independent findings, at the cost of waiting for the slower worker and making more model calls. The upcoming matched single-agent comparison must measure whether that tradeoff pays off.

## Staged is the only workflow

Remove the deprecated hub graph and its launch option. The earlier five-fault comparison tied at **4/5 for each workflow** after rerunning budget-blocked attempts; it did not establish a diagnostic advantage for retaining both. This is historical removal evidence, not a clean first-attempt benchmark or a multi-agent versus single-agent result. Keeping one staged workflow makes plan, gather, reason, critique and report visible as graph nodes and avoids maintaining duplicate routing. Historical artifact labels remain readable for grading and review; the old workflow cannot be launched or resumed at this version.

## Models propose; runtime owns state

Models return typed actions and role payloads. Runtime allocates identities, checks role permissions and evidence references, accepts receipts and controls budgets. Parallel workers never replace shared control state. Provenance validation proves a citation was retrieved, not that its interpretation is true.

## Repair small deviations and retain uncertainty

Normalize harmless formatting and identity slips, log every repair and strictly validate the result. Repairs must not manufacture source evidence. A bounded correction turn handles unusable output. A likely-cause claim without observation support becomes inconclusive; open objections and missing evidence remain in the report.

## Discover data and reuse queries

A catalog exposes actual metric names, services and log templates. Aggregation helpers make large snapshots usable within small contexts. Identical normalized queries reuse saved receipts without another tool charge. Changed prose and duplicate results do not count as progress.

## SQLite checkpoints are recovery authority

Persist admission before external calls, save worker responses and accept them at separate durable boundaries. Resume reuses saved completions and keeps unresolved calls charged. It verifies compatibility before writes, retains the original deadline and uses a per-run kernel lock. A new process can recover after process death; exactly-once provider execution and recovery across changed runtime versions are outside the contract.

## Limits belong to code

Keep finite run/task call allowances, per-call timeouts, wall deadlines, retries and review rounds. Reserve calls before parallel dispatch. Hard exhaustion keeps partial artifacts. The live profile gives providers longer timeouts; it does not remove bounds. Unknown usage remains unknown even when admission conservatively retains a reserve.

## Separate evaluation from runtime

Expectations, human notes and grading logic remain outside prompts and tool-visible sources. Grade final reports against culprit, mechanism and key evidence; record process quality separately. Keep failures in denominators and preserve every attempt. Citation checks need human semantic review. No fixed diagnostic acceptance threshold or multi-agent superiority claim is selected.

## Narrow source and storage interfaces

Use source functions behind a typed registry and artifact/run/catalog repositories behind the console. Local JSONL and filesystem adapters serve the MVP. New telemetry and object-storage adapters can implement those boundaries later; they are not shipped integrations.

## A local console with an optional Phoenix stack

Use server-rendered pages and small JavaScript files, without a frontend build or account. Launch one bounded subprocess job at a time, with explicit confirmation for provider calls. Every run writes its local trace. Export to the Phoenix stack is opt-in and loopback-only. Local artifacts, including the trace, stay complete when the Phoenix stack is off or unavailable.
