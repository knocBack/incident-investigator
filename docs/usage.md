# Usage reference

Run commands from the repository root. Start with the [quick start](quick-start.md) for a tested zero-cost path or the [console guide](ui.md) for browser operation. Angle-bracket arguments below are templates to replace with actual paths or IDs.

## Investigate a saved source

```sh
uv sync --locked
uv run incident-investigator investigate --case dev-00 --model scripted --script tests/scripted-demo.json
uv run python -m incident_investigator.tools
```

The first command investigates the registered development snapshot; the second prints a retrieval page from each of the five source tools without model calls. `--source` selects another registered manifest root. Repeated `--case` options investigate separate cases concurrently, with independent state. The default source is `fixtures/dev-00`.

The scripted adapter requires an explicit `--script`. Its generic context substitutions bind runtime identities in canned actions. It is a deterministic replay, not a diagnosis fallback.

## Lab snapshot demo

```sh
uv run incident-investigator demo --model scripted --script tests/scripted-demo.json
```

`demo` starts the lab in-process, sends baseline requests, injects a fault, sends failing load and freezes telemetry. The investigator sees only the resulting source snapshot.

| `--fault` | Lab behavior |
| --- | --- |
| `bad_configuration` | Caller timeout becomes too short; default |
| `deployment_regression` | A release makes an inventory query expensive |
| `database_contention` | Database work waits on locks |
| `resource_exhaustion` | Connection pool capacity is exhausted |
| `traffic_overload` | Excess demand overloads service capacity |

`--limits-profile demo` selects 240 seconds per run and 30 per call; `demo-live` selects 1,200 and 90. Call/review limits remain finite in both. Live adapters default to `demo-live`, scripted to `demo`. Full limits are in [runtime contracts](lld.md#limits-and-closure).

Staged is the only workflow. `--graph staged` is optional; results, traces, grades and batch metadata record it. Offline grading preserves historical artifact provenance without executing a graph.

## Saved output and inspection

Default output lives under `var/`; `II_HOME` selects another workspace and explicit `--out` overrides command output.

```text
var/runs/<run-id>/       demo and investigate
var/suites/<batch-id>/   evaluation batches and separate grades
var/lab/                lab telemetry and snapshots
var/jobs/<job-id>/       console job metadata, capped log and isolated grading input
var/reviews/<run-key>/   human-rubric.json sidecars
var/imports.json        read-only external roots
var/index.json          rebuildable index
```

| Run artifact | Purpose |
| --- | --- |
| `report.md` | Human report with hypotheses, facts, assumptions, recommendations and gaps |
| `result.json` | Structured result, state, usage, repairs and stop categories |
| `trace.jsonl` | Local model/tool calls, validation and transitions |
| `state.sqlite` | Authoritative graph checkpoints |
| `run.json` | Immutable configuration, models, limits, deadline and versions |
| `source/` | Registered input retained for compatible recovery |
| `snapshot/` | Frozen review input with manifest, alert, observations and fault plan |
| `expectation.json` | Demo evaluator pointer, outside the runtime source |
| `phoenix.json` | Phoenix trace/session link, when exported |

```sh
uv run incident-investigator inspect var/runs/<run-id>
uv run incident-investigator eval var/runs/<run-id>
```

`inspect` reads exports without opening a writable saver. `eval` grades offline and writes a separate suite by default. An unfinished run can have retained input and checkpoints but no final report. Failed/resource-exhausted investigations exit nonzero after exporting available progress. Inconclusive is a valid diagnostic outcome, distinct from execution failure. Recommendations do not mean remediation occurred.

## Resume

Use the original model mapping and a compatible runtime:

```sh
uv run incident-investigator investigate --resume var/runs/<run-id> --model openrouter
```

Omit `--case` and `--source`; resume loads the saved source. It preserves limits, wall deadline, fault progression and consumed allowances. Changed dataset, prompt, tool, runtime, dependency or checkpoint versions are refused before writable saver setup or adapter calls. Older runs without versioned metadata cannot resume. Do not expect resume across an upgrade.

Saved completions are reused. An admitted call with no saved response retains its charge and may retry with fresh identities within the original allowance. Exactly-once provider execution is not guaranteed. A kernel lock rejects concurrent modification of one run and releases on process death. An abrupt kill may leave stale exports; resume regenerates them from checkpoints.

Scripted recovery also requires an explicit file containing the remaining actions via `--script`. A full initial replay is not generally a valid continuation.

## Model adapters

```sh
export OPENROUTER_API_KEY='replace-with-your-key'
uv run incident-investigator demo --model openrouter --fault bad_configuration
```

This command can incur provider charges and was not executed for this documentation change. The key is read from the environment and excluded from saved model configuration. The adapter uses its default model unless `II_OPENROUTER_MODEL` or `--model-id` overrides it. Complete provider/model IDs are required.

Optional role overrides use `II_OPENROUTER_MODEL_ORCHESTRATOR`, `II_OPENROUTER_MODEL_OBSERVABILITY`, `II_OPENROUTER_MODEL_DEPENDENCY`, `II_OPENROUTER_MODEL_REASONING`, `II_OPENROUTER_MODEL_CRITIC` and `II_OPENROUTER_MODEL_REPORTING`. Use one model across roles and systems for a fair comparison. `II_OPENROUTER_FREE_ONLY=1` requires zero catalog token prices and a free-suffixed model before completion calls; provider rate limits still apply.

The adapter fetches a public model catalog, requests strict schema output when supported and falls back to JSON mode if a provider rejects that schema. Runtime validation applies to both. Models propose JSON tool requests; no native callable tools are sent to the provider. Output is capped at 4,096 tokens. Authentication/credit errors are nonretryable; rate limits can retry within runtime bounds. There are no hidden HTTP retries or automatic model substitutions.

`--model codex` uses a locally authenticated CLI adapter. It disables ordinary shell/image/search features, uses an empty working directory and checks the event stream for tool execution. A read-only process sandbox does not prevent filesystem reads; this is not an OS confidentiality boundary. Evaluator material must remain inaccessible to model processes. Runtime prompts never include design documents or expected answers.

## Evaluation suites

A zero-cost smoke batch:

```sh
uv run incident-investigator eval-suite --model scripted --models scripted \
  --script tests/scripted-demo.json --quick --budget-usd 0
```

`--set primary|variants|all` selects the ten incidents, two reliability variants, or both. `--system multi|single|both` selects investigators under the staged workflow. `--faults` narrows the chosen set. `--run-budget-usd`, `--call-seconds` and `--wall-seconds` override per-run cost and finite time limits equally for both systems.

`--quick` defaults to the selected set once at concurrency five. `--repeats` and `--concurrency` override those defaults. `--faults` accepts `all` or comma-separated lab family IDs. `--models` accepts comma-separated model IDs. Every attempt gets fresh lab/model state. Explicit output directories must be new.

`--k` sets the pass@k/pass^k cap (default four). Effective k is clamped to the observed attempts in each fault/model/workflow group. Grades, rubric worksheets, `leaderboard.json` and `leaderboard.md` retain outcomes and resources. See [evaluation](evaluation.md) for definitions and limitations.

For OpenRouter use `--model openrouter --models` with available complete model IDs, and set an explicit `--budget-usd`. The suite guard samples key usage, reserves conservative input/output costs before calls and refuses oversize prompts before sending. `--max-input-tokens` defaults to 64,000; output is capped at 4,096. Known billed costs settle reserves; unknown responses/timeouts retain them unless reconciled. Unsupported billing dimensions or insufficient capacity produce recorded skips. Inspect `budget.json`, batch settings and skipped entries. Demo/investigate have execution bounds but do not use this suite monetary guard.

Unknown tokens and cost remain unknown; known subtotals are available separately in raw JSON. Wall time is distinct from summed role durations. Lexical correctness and valid citations require human semantic review. No comparison result or acceptance threshold is implied by a successful CLI exit.

## Console, history and tracing

```sh
uv run incident-investigator console
```

The console binds to `127.0.0.1:8765`. `--port` selects another port. `incident-investigator import` takes one or more existing artifact roots and indexes them in place without editing the originals. Keep grades and reviews in the chosen workspace. `II_EVALS` selects an evaluator catalog containing `faults/*.json`; it never changes investigator sources.

`II_STORAGE=memory` selects the memory viewer/review backend, with subprocess launch unavailable by default. Filesystem storage is the normal persistent backend. The Phoenix stack is optional; [the console guide](ui.md#start-with-phoenix) includes the verified setup and trace behavior.

## Development checks

```sh
uv sync --locked
uv run pre-commit install
uv run pytest -q -o tmp_path_retention_policy=none
uv run ruff check .
uv run ruff format --check .
```

Live provider tests are opt-in and skipped by default. The normal suite uses scripted or mocked adapters. Follow the accepted [evaluation plan](evaluation-plan.md) when maintaining evaluator fixtures and keep them separate from runtime-visible evidence.
