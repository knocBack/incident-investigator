# LLD-02: retrieval contracts and local execution preflight

Status: specified on 2026-10-19 under the authorized LLD-02 plan. The project selected frozen JSONL fixtures. This checkpoint saves contracts and a concrete [controlled-test configuration](../config/local-double.json); it does not implement retrieval functions, scenario fixtures, a runtime, or a test runner. Dependency resolution subsequently succeeded; the lockfile records adopted pins. [LLD-01](lld.md) owns task/call/result lifecycle; this document fills its source/query/page/fault contracts and local settings. Open product decisions are consolidated in the [LLD handoff](lld.md#8-remaining-contracts-and-decision-gates).

## 1. Reuse and module boundary

Use **Ports & Adapters**: five typed retrieval functions behind a small registry bind the runtime to the synthetic source snapshot. The snapshot loader owns source validation; each query function owns its source-specific predicates and result normalization. Agents choose tool names/filters, not backend operations or file locations.

Use Pydantic models for request/result validation and generated JSON schemas. Reuse native typed-tool wrappers (`langchain_core.tools.StructuredTool`) for callable/schema integration, LangGraph `Send` and its SQLite checkpointer for graph dispatch/persistence, and `asyncio.timeout`/`Semaphore` for bounded waits/capacity. Keep one admitted external operation per call node as LLD-01 requires. A shared **Decorator-style async wrapper** applies the runtime's already-selected fault around a retrieval function; it never owns counters or commits state.

Use a dictionary from the five tool names to their definitions. Do not introduce a generic query language, custom tool framework, backend Strategy hierarchy, ORM, or another scheduler. There is one source implementation; narrow functions suffice. Native wrappers do not replace our invocation guards, provenance checks, or durable admission/acceptance rules.

Logical interfaces:

```text
load_snapshot(registered_case, expected_digest?) -> CaseSnapshot
normalize_query(tool_name, arguments, bound_context) -> NormalizedQuery
query_source(snapshot, normalized_query) -> ToolPage
apply_fault(query_callable, saved_fault_selection) -> wrapped_callable
```

The runner binds `case_id`, snapshot, cutoff, and current incident identity to the callable. None is an agent-supplied tool argument. Definitions expose only the selected query schema and description. Tool functions cannot modify task status, budgets, faults, or graph state; `execute_call` and the serialized controller retain those responsibilities.

## 2. Authored sources and immutable snapshots

Register opaque case IDs through an explicit manifest. A case entry names its alert JSON, observation JSONL, and fault-plan JSON within the configured source root. Resolve only those files; reject unknown case IDs, duplicate entries, paths outside the source root, and symlinks. Do not walk the repository or load evaluator directories. Evaluator expectations/graders stay separately registered with the evaluator and never enter this loader.

Each JSONL row is `SourceObservation` with exactly `source`, `source_record_id`, `event_time`, `available_at`, and `data`. `source` is one of the five tool categories; `data` is its typed payload below. Authored rows have no `dataset_digest` or `evidence_id`, avoiding a circular hash. Reject unknown envelope/payload fields, duplicate `(source, source_record_id)`, non-finite numbers, naive timestamps, inconsistent metric windows, and records outside the configured byte bounds. All five categories are registered even when some contain zero rows.

Load and validate once into a private, read-only snapshot owned by the runner. Normalize aware timestamps to UTC. Compute `dataset_digest = SHA256(canonical_json({schema_version, alert, observations, fault_plan}))`, sorting observations by `(source, source_record_id)` and fault rules by `rule_id`. Preserve meaningful array order inside records. Use LLD-01 canonical JSON. This digest identifies the selected case snapshot; a comparison batch records the map of case IDs to their digests.

Add `dataset_digest` and LLD-01's derived `evidence_id` to create runtime `Observation` records. Different content under an already registered source identity is an integrity error. Reordering JSONL lines or object keys alone does not change normalized content. Case content, cutoff, and fault-plan changes do. Prompt/tool/runtime/dependency versions remain separate compatibility identities.

Query functions never mutate snapshot objects and serialize fresh response copies; do not rely on Pydantic's shallow frozen setting to protect nested dictionaries. A caller mutating a returned page must not change subsequent retrieval. Source-file edits after loading cannot change an active run's in-memory snapshot. Before resume, reload/verify the registered content and compare the recorded digest before checkpointer setup or new investigation work. Never silently replace saved observations.

The execution clock is used for deadlines and timings only. Evidence eligibility uses the fixed incident cutoff: `available_at <= cutoff`, plus the source-specific event/as-of rules below. `available_at` means retrievable from the source, which may be later than event occurrence or collector observation. There is no moving simulation clock. Earlier incidents may contain their recorded explanations/resolutions; the active incident's eventual postmortem is excluded by incident identity and availability.

## 3. Source payloads and query models

`Service` is `checkout|inventory|database`. Query strings must be nonempty when supplied; trim identifiers, normalize service/severity enums, preserve case for version/operation/correlation/metric identifiers. Optional filters default to null. Supplied filters combine with AND. `text_contains` is a trimmed, case-folded literal substring, not regex, SQL, semantic search, or a query language. Reject unexpected query fields. Bound filter strings to 256 characters, source-record IDs to 128 characters, and encoded cursors to 2,048 bytes so query/cursor metadata fits a bounded page. Canonical serialization uses Python JSON with sorted keys, compact separators, `ensure_ascii=False`, and `allow_nan=False`; normalize UTC timestamps to six fractional digits and `Z` before hashing.

### Source payloads

All fields listed are required; nullable fields use null and collection fields may be empty. `JSONScalar` has LLD-01's meaning. Values in `attributes` preserve operational context such as target address, lock/pool ownership, attempt counts, or measurement numerators; they are source observations, never evaluator answers.

| Source | `data` payload |
| --- | --- |
| Logs | `service: Service`, `severity: TRACE\|DEBUG\|INFO\|WARN\|ERROR\|FATAL`, `body: str`, nullable `version`, `operation`, `request_id`, `trace_id`, `transaction_id` strings, `attributes: dict[str, JSONScalar]`. |
| Metrics | `service: Service`, `name: str`, `value: finite number`, `unit: str`, `aggregation: gauge\|count\|sum\|rate\|mean\|p50\|p95\|p99`, nullable UTC `window_start`/`window_end`, nullable `version`/`operation` strings, `sample_count: int\|null`, `attributes: dict[str, JSONScalar]`. Counts are nonnegative. |
| Changes | `service: Service`, `change_kind: deployment\|configuration`, `version: str\|null`, `description: str`, `changes: list[{key: str, before: JSONValue, after: JSONValue}]`, `attributes: dict[str, JSONScalar]`. `JSONValue` is finite JSON, including nested objects/lists. Deployment differences may use `changes`; configuration values remain raw differences. |
| Dependencies | `service: Service`, `targets: list[Service]`. Each observation is one complete observed topology snapshot for that service at `event_time`, with unique sorted targets. Empty targets is an observed empty topology; no matching observation is missing coverage. |
| History | `incident_id: str`, `services: list[Service]`, `title: str`, `summary: str`, `symptoms: list[str]`, nullable `root_cause`/`resolution` strings. Services are nonempty, unique, and sorted. `event_time` is the earlier incident's occurrence and `available_at` its source-publication time. |

Point metrics have both window fields null and use `event_time`. Windowed metrics have start < end, `event_time == window_end`, and `available_at >= window_end`. Preserve the authored unit, aggregation, sample count, and window; do not interpolate, re-bucket, or aggregate measurements on demand. Fixture checks must reconcile counts/rates and weighted summaries.

### Query arguments

Logs, metrics, changes, and history share `service: Service`, nullable UTC `start`/`end`, `limit: int = 50`, and nullable `cursor: str`. For logs/metrics/changes, missing start/end resolve to alert-window start and cutoff. For history, missing start means no lower bound and missing end resolves to cutoff. Require start < end when start exists, and end <= cutoff. Reject future bounds instead of silently clamping them. A start earlier than the alert is permitted for baseline investigation.

| Tool | Additional filters and actual retrieval behavior |
| --- | --- |
| `logs` | Nullable `severity`, `version`, `operation`, `request_id`, `trace_id`, `transaction_id`, `text_contains`. Match service and `start <= event_time < end`, then exact fields and body substring. |
| `metrics` | Nullable `metric_name`, positive integer `window_seconds`, `version`, `operation`. Point samples use the event interval. Windowed samples must fit wholly within `[start, end]`; a window ending exactly at end is allowed. `window_seconds` matches recorded window duration and excludes point samples. |
| `changes` | Nullable `change_kind`, `version`. Match service, event interval, kind, and version; return actual deployment/configuration records. |
| `dependencies` | Only `service` and nullable UTC `as_of`, defaulting to cutoff. Require as_of <= cutoff. Return the latest snapshot satisfying `event_time <= as_of` and `available_at <= as_of`. Duplicate snapshot timestamps for one service are fixture-integrity errors. Return one hop; agents can query another service separately. No pagination arguments. |
| `history` | Nullable `text_contains`, matched within any individual title, summary, symptom, root-cause, or resolution string after case folding. Match service membership and incident event interval, require publication by cutoff, and exclude `incident_id == bound case_id`. Never return private case expectations. |

Every source also enforces cutoff availability. One case namespace cannot query another's observations. A complete query with no matching records returns an empty page; it does not establish a healthy application. No snapshot for dependencies is similarly an empty retrieval, not evidence that the service has no dependencies.

## 4. Normalization, pages, and coverage

`NormalizedQuery` has `tool`, resolved `filters` (including service/time or as-of), `dataset_digest`, `cutoff`, `query_key`, `limit`, and `cursor`. The `query_key` hashes canonical `{tool_contract_version, tool, resolved_filters, dataset_digest, cutoff}`; the initial contract version is `tool-contract.v1`. Exclude limit/cursor so changing page size or replaying a page cannot create new full-query coverage. Normalize timestamp offsets and nullable defaults before hashing; text search uses its normalized substring. Keep distinct metric name/window scopes. Dependencies normalize to limit 1 and cursor null internally. Do not semantically deduplicate arbitrary natural-language questions.

```text
ToolPage = {
  "tool": ToolName,
  "dataset_digest": str,
  "query_key": str,
  "query": resolved_filters,
  "cutoff": UTC,
  "source_status": "complete" | "partial" | "failed",
  "observations": [Observation, ...],
  "available_match_count": int | null,
  "next_cursor": str | null,
  "error": Failure | null
}
```

`source_status` describes source retrieval, not coverage of the entire query or success of its owning task:

| Outcome | Contract |
| --- | --- |
| Complete source page | All eligible matches are known to the adapter; count is their exact total before pagination. Error is null. A next cursor means more matching records remain. Empty + count zero is complete-empty retrieval. |
| Partial source | Some observations may be returned unchanged, but coverage is uncertain. Count/cursor are null and error identifies incomplete retrieval. Partial results do not automatically retry; the agent can ask for more evidence within limits. |
| Failed source | Observations are empty, count/cursor are null, and error identifies the failure. Eligible transient timeouts follow the runtime's bounded retry policy. Failure is never an application-cause observation. |

Sort paginated matches by `(event_time, source_record_id)` ascending. Use standard keyset pagination with a bounded base64url JSON cursor containing `version: 1`, dataset digest, query key, and the last returned sort key. Parse/validate it using the same Pydantic boundary mechanism; never deserialize executable objects. Reject wrong query/snapshot, malformed encoding, or a last key absent from that query's eligible matches. Apply filters/cutoff before cursor selection. A cursor is a continuation position, not permission to bypass filtering.

Return the largest prefix fitting requested record count and the complete page's serialized byte bound, then issue the cursor after its last record. Include envelope/cursor overhead in size checks. Enforce record bounds during load so one valid observation plus envelope can fit the page. Invalid queries/cursors or an impossible response bound return a structured error rather than silently omitting evidence. Dependencies return at most one bounded record and no cursor.

Validate query models and context-dependent bounds before admitting a tool batch. A validation failure is bounded correction feedback, not a dispatched tool call. An error discovered during an admitted call, such as a cursor position absent from the eligible records, produces a failed `ToolPage` bound to its already normalized query. No invalid argument fallback may invoke a different tool or widen the query.

Add `query_coverage: dict[query_key, QueryCoverage]` to LLD-01 control state when tool receipt acceptance is implemented. `QueryCoverage` contains `seen_evidence_ids: list[str]` (unique sorted), `expected_matches: int|null`, and `completion_receipt_id: str|null`. Include only observations actually returned and accepted under this exact normalized query. A complete source page establishes the expected count; conflicting complete counts under an immutable snapshot are an integrity error. Complete coverage requires a known count and all that query's matching records returned across accepted receipts. Save the first completing receipt ID once. A final page alone, a forged continuation position, or a changed limit cannot establish it.

This implements HLD-PROG-01's two progress signals: a new observation, or first complete coverage for a normalized query, including complete-empty. Duplicate observations/receipts and changed prose do not create progress. Coverage is checkpointed with receipt processing, so replay cannot count it again. Review-round counters/thresholds remain the later coordination contract; these source records do not claim diagnostic progress.

## 5. Fault wrapper and execution accounting

`FaultPlan` contains `rules: list[FaultRule]`. A rule has `rule_id: str`, `tool: ToolName`, `service: Service`, nullable UTC `window_start`/`window_end` (both null or both present), and `steps: list[FaultStep]`. Steps have `outcome: none|timeout|partial` and `keep_first: int|null`; a partial step requires a nonnegative count, and other outcomes require null. After listed steps, use `none`. Validate unique rule IDs and at most one rule per `(tool, service)` within a case, so even a broad query cannot match competing schedules. A present rule window must have start < end.

A scoped rule matches the tool/service and overlap with the half-open query interval (`query_start < rule_end` and `rule_start < query_end`); dependencies use `rule_start <= as_of < rule_end`. A history query without a lower bound uses negative infinity for this comparison. Steps are shared across matching filters/pages: changing text, time bounds within that scope, limit, or cursor must not restart a schedule.

At call admission the controller uses `counter_key = SHA256(canonical_json([dataset_digest, rule_id]))`, selects the next zero-based step from its saved counter, increments that counter, and saves rule ID, counter key, step, outcome, and keep count with the admitted call in one transition. This extends LLD-01's `FaultSelection` with `rule_id` and nullable `keep_first`; the wrapper needs no mutable plan lookup. The selected fault is runtime-only, never an agent argument. Replaying admission consumes no additional step. A new call attempt consumes a new step; a proven non-dispatch does not rewind an already saved admission. Report actual fault exercise separately from a merely selected fault. Each fresh run starts independent counters.

The shared wrapper receives only that saved selection:

- `none`: invoke the normal query callable.
- `timeout`: wait cooperatively inside the normal `asyncio.timeout` execution boundary until it expires; the executor records a timeout receipt. Tests use short explicit overrides/events rather than waiting ten seconds.
- `partial`: query normally, retain at most `keep_first` whole returned observations, and return partial status with null count/cursor and a `source_incomplete` error. Do not alter individual observation data or fabricate zero measurements.

The executor normalizes tool timeouts/errors to a failed `ToolPage` in the call receipt, with available timing/accounting. Runtime classifies retryability: source timeout is retryable within limits; invalid query/cursor and integrity errors are not automatically retried. Model correction is distinct from retrying the identical external operation. Cancellation and graph replay follow LLD-01; the wrapper provides neither hidden retries nor state writes.

## 6. Concrete local profile

[config/local-double.json](../config/local-double.json) is the source for these controlled-test settings. It is saved configuration, not a loaded/validated application profile; its loader does not exist yet. Reject use with a real model adapter. Synthetic token allowances/counts exercise accounting and are not a provider spending guarantee.

| Group | Values |
| --- | --- |
| Tasks/retries | 16 tasks/run, 2 attempts/task, 1 retry/operation |
| Calls/accounting | 12 model calls, 24 tool calls, 32,768 total synthetic tokens/run |
| Capacity | 2 calls/run, 4 calls across the runner, maximum 4 requests/tool batch; support two progressing investigations |
| Deadlines | 120 s/run, 45 s/task, 10 s/call; original task deadline survives retries/resume |
| Payloads | Context 64 KiB, receipt 128 KiB, result 32 KiB |
| Retrieval | Default 50 rows/page, maximum 100; complete page 32 KiB, observation 8 KiB, source snapshot 8 MiB |
| Fake model request | Input bound 2,048 tokens, output bound 1,024, reserve 3,072 per call; measured fake usage is retained separately |

Page/observation/snapshot byte limits include serialized envelopes where applicable; the snapshot loader bounds both source reads and final normalized content. Fault plans and alerts count toward the snapshot bound. Cursor/query inputs are bounded by the context limit and their typed schema. Test-specific overrides remain finite and are recorded with the run. Real-model settings, validated token bounds/prices, spending caps, full-loop round/no-progress limits, and benchmark repetitions are not selected by this profile.

## 7. Dependency preflight and next executable milestone

Target installed CPython 3.13.13; its SQLite library reports 3.53.4. Candidate direct dependencies are `langgraph==1.2.16`, `langgraph-checkpoint-sqlite==3.1.2`, `pydantic==2.12.5`, `pytest==9.0.2`, and `pytest-asyncio==1.3.0`. Declare `langchain-core` directly because the tool wrapper is imported; select its compatible version through resolution and freeze it with every transitive dependency in the lockfile. Candidates are not adopted pins until resolution and integration checks pass. Do not invent a lockfile or silently substitute a different stack after a resolver failure.

Executed read-only resolution attempt on 2026-10-19, with temporary cache and no installation/output file:

```sh
uv --no-config pip compile --python-version 3.13 --no-python-downloads - <<'REQUIREMENTS'
langgraph==1.2.16
langgraph-checkpoint-sqlite==3.1.2
pydantic==2.12.5
langchain-core
pytest==9.0.2
pytest-asyncio==1.3.0
REQUIREMENTS
```

Outcome: dependency resolution succeeded on 2026-10-19. The adopted lock resolves langgraph 1.2.16, langgraph-checkpoint-sqlite 3.1.2, pydantic 2.12.5, langchain-core 1.6.12, aiosqlite 0.22.1, pytest 9.0.2, and pytest-asyncio 1.3.0. Resolution establishes compatible pins; imports and graph behavior are covered by the preflight test.

The first product-validation gate is a resolved dependency lock plus actual imports, native typed-tool schema/async invocation, one synchronous SQLite-backed graph transition, and successful sibling pending-write recovery with controlled doubles. The LLD-01 specialist target uses these retrieval contracts, one development incident, minimum traces, and basic guards; scripted model responses and controlled failures permit validation without provider selection or paid calls. Other role payloads/full coordination must be specified before their affected behavior. The [LLD handoff](lld.md#8-remaining-contracts-and-decision-gates) records the remaining gates. Workspace procedures are outside this product-design handoff.

## 8. Verification coverage and source lessons

Save this decision checkpoint before extending the evaluation specification through [incident-evals](../.agents/skills/incident-evals/SKILL.md). Reuse fixture/tool acceptance plus HLD-OWN/PROG/DUR/RUN groups. Cover query predicates, cutoff/delayed availability, history exclusion, copied responses, stable paging/byte limits, invalid cursors, complete-empty/partial/failed outcomes, source-fault scope, concurrent isolation, and resume. Keep the ten primary incidents, two variants, and human diagnostic policy unchanged. All executable cases are planned until loaders/tools/runtime and test runner exist; document checks and dependency failures are separate evidence.

| Source -> problem | Project rule -> verification |
| --- | --- |
| [Pydantic JSON Schema](https://pydantic.dev/docs/validation/latest/concepts/json_schema/) and [native typed tools](https://docs.langchain.com/oss/python/langchain/tools): separate handwritten schemas and argument handling can drift. | Generate tool schemas from their validation models; use native typed wrappers. Check exposed schemas and actual accepted/rejected calls together. |
| [OpenTelemetry log model](https://opentelemetry.io/docs/specs/otel/logs/data-model/): event occurrence and observation time are distinct. | Preserve occurrence plus source-availability time; the latter means queryable in this project, not automatically OTel's collector timestamp. Test an old event published after cutoff. No OTel runtime dependency is introduced. |
| [OpenTelemetry metric model](https://opentelemetry.io/docs/specs/otel/metrics/data-model/): a measurement includes temporal and attribute context. | Retain units/aggregation/windows and filter recorded measurements without inventing aggregation. Verify window containment and fixture reconciliation. This small payload is not a full OTLP implementation. |
| [Python asyncio timeout](https://docs.python.org/3.13/library/asyncio-task.html#asyncio.timeout) and [pytest temporary paths](https://docs.pytest.org/en/stable/how-to/tmp_path.html): bounded async execution and isolated test artifacts have established primitives. | Reuse them for timed faults and per-test SQLite/source directories. Observe timeout/cancellation outcomes and run isolation against the real implementation. |

LLD-02 checkpoint verification, before the documentation reconciliation: inline JSON checks confirmed the saved profile's fields, selected values, and finite bound relationships; no application loader exists yet. Document checks passed across seven documents and 61 local links/anchors. The incident definitions, comparison/grading policy, and all 12 earlier skill/HTML/transcript files were unchanged at that checkpoint. Executable coverage and the dependency failure are recorded in the [evaluation checkpoint](evaluation-plan.md#lld-02-checkpoint-coverage--2026-10-19); no source/runtime/diagnostic pass is claimed.
