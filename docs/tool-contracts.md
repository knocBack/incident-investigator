# Frozen evidence tool contracts

The loader and tool registry expose validated snapshot records, not filesystem paths or live service access. [Runtime contracts](lld.md) own call admission, retries, counters and acceptance. Tool input/output schemas live in `tools/contracts.py`; source payloads live in `telemetry/models.py`.

## Source boundary

`load_snapshot` selects one registered case from a manifest containing alert, observation and fault-plan paths. Paths must stay inside the configured source root; symlinks and duplicate source identities are rejected. The loader does not scan the repository or load evaluator expectations.

Each authored record has a source, source record ID, event time, availability time and typed data. Supported sources are logs, metrics, changes, dependencies and history. Timestamps must have offsets and normalize to UTC. Metric windows, units and aggregation context remain explicit. Non-finite values and invalid envelopes fail validation.

The loader computes a canonical dataset digest over the normalized alert, observations and fault plan. Runtime evidence IDs combine that identity with source identity. File edits cannot change an already-loaded snapshot. Resume reloads retained sources and verifies versions before external work. Tools serialize fresh response copies so a consumer cannot mutate the underlying data.

Every query enforces the incident cutoff, including availability time. A record backdated before the cutoff but published afterward remains hidden. Metric windows must fit the query interval. Ordinary intervals are half-open, except that evidence exactly at the incident cutoff is included. Missing data is not a healthy reading.

## Retrieval tools

Paged tools default to 50 rows, with a maximum of 100. Unknown fields and invalid context-dependent bounds are rejected. `service` selects checkout, inventory or database. Time arguments require an offset.

| Tool | Filters and behavior |
| --- | --- |
| `logs` | Service/time, severity, version, operation, request ID, trace ID, transaction ID and body substring |
| `metrics` | Service/time, metric name, recorded window size, version and operation; preserves authored units and aggregations |
| `changes` | Service/time, deployment/configuration kind and version; returns raw change records |
| `dependencies` | Service and optional as-of time; returns the latest available one-hop snapshot, without pagination |
| `history` | Service membership, event interval and case-insensitive substring; excludes the current incident and later-published history |

Logs, metrics and changes default to the alert window start and cutoff. History has no default lower bound. Explicit future end/as-of bounds are errors rather than silently widened queries. A complete empty page establishes only that the query found no matching records.

## Discovery, aggregation and correlation

| Tool | Input | Output |
| --- | --- | --- |
| `catalog` | Optional service | Names and summaries of available services, metrics, templates, versions, changes, dependencies, history and enum values |
| `log_counts` | Optional service/time/severity/text, grouping fields, attribute and time bucket | Counts by group, first/last times and bounded citable exemplars |
| `metric_stats` | Optional service/time/name/window and split time | Recorded-series statistics and before/after split summaries, with peak-sample exemplars |
| `trace_logs` | Trace ID and/or request ID, optional time/page filters | Correlated logs across services |

These helpers have no default lower time bound, so pre-alert baselines remain visible. Aggregation derives summaries from existing records; it does not interpolate or rewrite measurements. Derived pages are bounded and can detach exemplars or fail with a request to narrow the query. A derived summary does not establish complete-empty query coverage or manufacture an application-cause observation.

Observability may use catalog, logs, log_counts, trace_logs, metrics, metric_stats and history. Dependency may use catalog, dependencies, changes, history, log_counts and metric_stats. Other roles cannot retrieve evidence. The allowed registry schemas are supplied in each specialist's context.

## Page and pagination contract

A `ToolPage` includes tool name, dataset digest, normalized query/key, cutoff, source status, observations, available match count, next cursor, error and optional derived summary.

| Status | Contract |
| --- | --- |
| `complete` | Known eligible matches; exact count before pagination; a cursor means more records remain |
| `partial` | Some unchanged records may survive, but count/cursor are unknown and an incomplete-source error explains the gap |
| `failed` | No observations, unknown count/cursor and an explicit source error |

Paged results sort by event time and source record ID. A bounded encoded cursor binds dataset, normalized query and last eligible sort key. Wrong-query, wrong-dataset, malformed or nonexistent-position cursors fail. A cursor cannot bypass cutoff or filtering. Page, observation and snapshot byte limits are enforced in addition to row limits.

The query key includes tool-contract version, tool, normalized filters, dataset and cutoff. Page size and cursor are separate from whole-query identity. The runtime's exact-query deduplication also includes page size/cursor; it returns an earlier complete receipt without a fresh tool charge. Failed and partial pages remain eligible for further attempts.

Coverage tracks accepted evidence and known matching counts. New evidence or the first complete-empty retrieval can advance runtime progress. A final page alone, a replayed receipt or changed prose cannot establish new progress. [Runtime contracts](lld.md#limits-and-closure) define review/no-progress closure.

## Controlled tool faults

A snapshot fault plan contains uniquely identified rules bound to a tool/service and optional overlapping time scope. Steps select normal, timeout or partial behavior; partial steps specify how many whole observations to retain. A schedule ends in normal behavior after its steps are consumed. These are retrieval faults, distinct from the application's injected cause.

Runtime selects and checkpoints a step at call admission, increments the saved rule counter and binds the selection to the call. Different query filters do not reset the schedule. A new retry consumes a new step; replaying saved work does not. Fresh investigations have independent counters.

The wrapper only applies the saved selection. Timeout cooperates with the ordinary call deadline. Partial mode retains unchanged records but clears count/cursor and adds an incomplete-source error. It never edits a measurement into a healthy or zero value. Bounded transient failures can retry; persistent failure remains a report gap. Cancellation, replay eligibility and recovery remain runtime responsibilities.

## Local verification

```sh
uv run python -m incident_investigator.tools
uv run pytest -q tests/test_m2_tools.py tests/test_reliability.py -o tmp_path_retention_policy=none
```

These use frozen or controlled local data without provider calls. `config/local-double.json` retains a historical preflight configuration; it is not loaded as a runtime profile. The executable `demo` and `demo-live` profiles are defined in `contracts.py` and documented in [runtime limits](lld.md#limits-and-closure).
