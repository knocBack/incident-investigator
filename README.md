# incident-investigator

Local multi-agent incident investigation MVP.

Status: M3 six-role local investigation demo; recovery hardening remains M4.

## Try the retrieval tools

```sh
uv run python -m incident_investigator.tools
```

The command loads frozen `dev-00` and prints one JSON page from each retrieval
source. It does not access the live lab or produce a diagnosis.

## Run the lab

```sh
uv run python -m incident_investigator.lab serve --telemetry telemetry.jsonl
uv run python -m incident_investigator.lab load --rps 10 --duration 30
uv run python -m incident_investigator.lab fault set bad_configuration
uv run python -m incident_investigator.lab snapshot --case dev-00 --cutoff 2026-01-01T00:01:00Z --service inventory --symptoms timeouts --out fixtures/dev-00
```

## Quickstart

```sh
uv sync --locked
uv run pytest
uv run pre-commit install
```

## Docs

- [Requirements](docs/requirements.md)
- [Product requirements](docs/mvp-plan.md)
- [Architecture](docs/hld.md)
- [Low-level design](docs/lld.md)
- [Tool contracts](docs/tool-contracts.md)
- [Evaluation plan](docs/evaluation-plan.md)
- [Accepted decisions](docs/decisions.md)

## Demo

```sh
uv sync --locked
uv run incident-investigator demo --model scripted --script tests/scripted-demo.json --out runs/
uv run incident-investigator demo --model codex --fault bad_configuration --out runs/
uv run incident-investigator investigate --case dev-00 --model codex --out runs/
uv run incident-investigator investigate --case dev-00 --case dev-00 --model scripted --script tests/scripted-demo.json --out runs/
uv run incident-investigator inspect runs/<run-uuid>
```

`demo` starts the FastAPI lab in-process, sends baseline requests, injects the
selected fault, sends failing load, freezes telemetry at a cutoff into a temporary
case, and investigates that snapshot. Agents never call the live app. Fault choices:
`bad_configuration` (default), `deployment_regression`, `database_contention`,
`resource_exhaustion`, `traffic_overload`. Model use needs a locally authenticated
`codex` CLI; the adapter selects `gpt-6-luna` with low reasoning effort.

Scripted mode is a deterministic replay, not a diagnostic fallback. Scripts are
caller-supplied with `--script`; the example lives under `tests/` and reports
inconclusive. An empty ScriptedModel fails boundedly and retains a partial report.
Generic `{{context.<key>}}` substitutions bind runtime identities in replay data.
`--source <manifest-root>` selects other explicitly registered frozen cases.
Repeated `--case` runs concurrently with independent state and fault counters.

Each fresh UUID directory contains `report.md`, `result.json`, `trace.jsonl`, and
`state.sqlite`. Reports preserve ranked support/contradictions, facts/assumptions,
verification recommendations, unresolved objections, gaps, and all six roles'
usage. Unknown token counts remain `unknown`; known subtotals remain available in
JSON. Recommendations never mean a fix was applied. Limits produce partial
reports; inconclusive is a valid diagnosis. `inspect` only reads local exports.
SQLite checkpoints retain admission/receipts/acceptance, but public resume and
crash/replay hardening are deferred to M4. The bounded demo profile and payloads
are specified in [LLD-03](docs/lld-03-roles.md). This is not a recovery guarantee.
LangSmith tracing is explicitly disabled; traces and artifacts remain local.

Codex isolation: the installed `codex exec --help` and `codex features list`
expose `--disable shell_tool` and `--disable unified_exec`; both are applied,
with web search disabled, user config/rules ignored, an empty temporary working
directory, and a read-only sandbox. The JSON event stream is checked and the call
fails if any command/tool/function/web/file-change execution event appears. See
[official configuration documentation](https://developers.openai.com/codex/config-basic/).
Residual risk: read-only does not prevent reading the filesystem. Event rejection
happens after execution and cannot undo an attempted read; this CLI boundary is
not an OS-level confidentiality sandbox. Do not place private evaluator content in
a model-accessible environment. Runtime prompts contain only package role text,
task context, retrieved frozen observations and prior accepted results; runtime
never imports evaluators or reads design documents. Call/time bounds limit runtime,
not provider billing; missing usage is reported honestly.
