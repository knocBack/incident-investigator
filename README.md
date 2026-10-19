# incident-investigator

Local multi-agent incident investigation MVP.

Status: M3 six-role local investigation demo; recovery hardening remains M4.

## Try the retrieval tools

```sh
uv run python -m incident_investigator.tools
```

The command loads frozen `dev-00` and prints one JSON page from each retrieval
source. It does not access the live lab or produce a diagnosis. Agents also have
`catalog` (what names/templates/keys exist), `log_counts`, `metric_stats` and
`trace_logs`; see [tool contracts §9](docs/tool-contracts.md#9-real-world-data-access-extension-m3-quality).

## Run the lab

Start a fresh lab in one terminal, with a fresh telemetry file:

```sh
LAB_DEMO_DIR=$(mktemp -d)
echo "$LAB_DEMO_DIR"
uv run python -m incident_investigator.lab serve --telemetry "$LAB_DEMO_DIR/telemetry.jsonl"
```

In a second terminal, set `LAB_DEMO_DIR` to the printed directory, then run:

```sh
export LAB_DEMO_DIR=/path/printed/by/the/first/terminal
uv run python -m incident_investigator.lab load --rps 5 --duration 2
uv run python -m incident_investigator.lab fault set bad_configuration
uv run python -m incident_investigator.lab load --rps 5 --duration 2
LAB_CUTOFF=$(date -u +%Y-%m-%dT%H:%M:%SZ)
uv run python -m incident_investigator.lab snapshot --case manual-demo --cutoff "$LAB_CUTOFF" --service inventory --symptoms timeouts --telemetry "$LAB_DEMO_DIR/telemetry.jsonl" --out "$LAB_DEMO_DIR/case"
uv run incident-investigator investigate --case manual-demo --source "$LAB_DEMO_DIR/case" --model scripted --script tests/scripted-demo.json --out runs/
```

The baseline uses about ten of the lab's 100 inventory units. Load runs again
after fault injection. The snapshot uses the current UTC cutoff and a separate
directory; keep `fixtures/dev-00` intact. An empty snapshot emits a warning.
The scripted replay demonstrates coordination; use `--model codex` for diagnosis.

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
uv run incident-investigator demo --model openrouter --fault bad_configuration --out runs/
uv run incident-investigator investigate --case dev-00 --model openrouter --out runs/
uv run incident-investigator investigate --case dev-00 --case dev-00 --model scripted --script tests/scripted-demo.json --out runs/
uv run incident-investigator inspect runs/<run-uuid>
```

`demo` starts the FastAPI lab in-process, sends baseline requests, injects the
selected fault, sends failing load, freezes telemetry at a cutoff into a temporary
case, and investigates that snapshot. Agents never call the live app. Fault choices:
`bad_configuration` (default), `deployment_regression`, `database_contention`,
`resource_exhaustion`, `traffic_overload`. For OpenRouter, set `OPENROUTER_API_KEY`
in your shell. `--model codex` remains available with a locally authenticated CLI
and selects `gpt-6-luna` with low reasoning effort.

Scripted mode is a deterministic replay, not a diagnostic fallback. Scripts are
caller-supplied with `--script`; the example lives under `tests/` and reports
inconclusive. An empty ScriptedModel fails boundedly and retains a partial report.
Generic `{{context.<key>}}` substitutions bind runtime identities in replay data.
`--source <manifest-root>` selects other explicitly registered frozen cases.
Repeated `--case` runs concurrently with independent state and fault counters.

Each fresh UUID directory contains `report.md`, `result.json`, `trace.jsonl`, and
`state.sqlite`. Reports preserve ranked support/contradictions, facts/assumptions,
verification recommendations, unresolved objections, gaps, and all six roles'
usage. Failed or resource-exhausted investigations exit nonzero after writing artifacts.
Input errors print a concise message and create no run. Unknown token counts remain `unknown`; known subtotals remain available in
JSON. Recommendations never mean a fix was applied. Control-step failures and repaired decisions produce partial reports through
Reporting when capacity permits. Hard execution limits retain a partial summary; inconclusive is a valid diagnosis. `inspect` only reads local exports.
SQLite checkpoints retain admission/receipts/acceptance, but public resume and
crash/replay hardening are deferred to M4. The bounded demo profile and payloads
are specified in [LLD-03](docs/lld-03-roles.md). This is not a recovery guarantee.
LangSmith tracing is explicitly disabled; traces and artifacts remain local.

## OpenRouter models

`--model openrouter` uses `https://openrouter.ai/api/v1/chat/completions` through
httpx. The credential is read only from `OPENROUTER_API_KEY`; it is never included
in saved configuration, prompts, reports or traces. No native/callable tools are
sent to the API. Every role requests retrieval by returning a JSON `tools` action;
the prompt lists that role's registry names and argument schemas.

Default: `nvidia/nemotron-3-super-120b-a12b:free`. Its current public model catalogue
entry advertises structured outputs. The adapter fetches and caches the public
catalogue per run, uses strict `response_format: json_schema` for supporting models,
and uses JSON mode plus the same runtime validation/correction for other models.
Provider parameter support is required and provider fallback is disabled. Each
request has a hard per-call timeout from the run profile (`demo-live`: 90 seconds for
OpenRouter and Codex) and a 4096-token output bound. A model whose provider rejects the
strict schema with HTTP 400 switches to JSON mode for the rest of the run. Models with a
reasoning switch have it disabled only when the catalogue explicitly marks reasoning
as optional (`reasoning.mandatory: false`); otherwise provider defaults apply.
There are no hidden HTTP retries;
the runtime retries eligible call failures once. Specialists see their remaining
model- and tool-call allowance; their last permitted response schema allows a final result
only, so they can summarize retrieved evidence rather than request another query.
Identical repeated queries are answered from the earlier receipt without a tool call.
Harmless output deviations are repaired and counted per role (`repairs` in `result.json`).
Authentication/credit failures
(401/402) are nonretryable; rate limits (429) include the provider message and are
retryable within the same limits. Free-model rate limits can prevent a run.

Set a default model for all roles or override any role with these environment names:

```sh
export II_OPENROUTER_MODEL=nvidia/nemotron-3-super-120b-a12b:free
# Optional overrides, using complete organization/model IDs:
# II_OPENROUTER_MODEL_ORCHESTRATOR
# II_OPENROUTER_MODEL_OBSERVABILITY
# II_OPENROUTER_MODEL_DEPENDENCY
# II_OPENROUTER_MODEL_REASONING
# II_OPENROUTER_MODEL_CRITIC
# II_OPENROUTER_MODEL_REPORTING
II_OPENROUTER_FREE_ONLY=1 uv run incident-investigator demo --model openrouter --fault bad_configuration --out runs/
```

Free-only mode checks the catalogue's zero input/output prices and the `:free`
suffix before sending a completion. `google/gemma-4-31b-it:free` and
`minimax/minimax-m3.1-flash:free` are manual alternatives; their current catalogue
entries use the JSON-mode path rather than advertising structured outputs.
There is no automatic model substitution. Resolved per-role models, generation
IDs, token usage, returned cost and latency are saved locally. Missing usage or cost
remains `unknown`, with known subtotals reported separately.

Paid demo preset (documentation only; not executed during this change):

```sh
export II_OPENROUTER_MODEL=deepseek/deepseek-chat
export II_OPENROUTER_MODEL_ORCHESTRATOR=deepseek/deepseek-chat
export II_OPENROUTER_MODEL_OBSERVABILITY=google/gemini-2.5-flash-lite
export II_OPENROUTER_MODEL_REASONING=deepseek/deepseek-r1-0528
```

`google/gemini-2.5-flash` is another paid Observability option. Per-role mapping is
for demonstrations. The later model-comparison benchmark must use **one model for
both multi-agent and single-agent modes**, with the same budgets and settings.

Mocked adapter tests make no network calls. An additional free-only live API test
is skipped by default; explicitly enable it with `II_LIVE_OPENROUTER=1` and run
`uv run pytest -m live tests/test_openrouter.py`. It requires the key in your shell.

API references: [structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs),
[usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting),
and [public model catalogue](https://openrouter.ai/api/v1/models).

Codex isolation: the installed `codex exec --help` and `codex features list`
expose `--disable shell_tool`, `--disable unified_exec`, and `--disable view_image`; all are applied,
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

## Evaluate

Grade saved runs without model calls:

```sh
uv run incident-investigator eval runs/<uuid> --fault bad_configuration --out /tmp/grades
```

New demos save `expectation.json` beside `result.json`, outside the temporary
agent-visible source snapshot. `eval` can use this pointer automatically; dev-00
retains its evaluator expectation. Expectations and grading code are never runtime
inputs. Five lab families live in `evals/expected/faults`; these development cases
are separate from the ten primary specification cases and two reliability variants.

Run an exploratory batch (requires OpenRouter credentials and explicitly authorizes
paid calls):

```sh
uv run incident-investigator eval-suite --faults all --model openrouter \
  --models organization/model --repeats 2 --budget-usd 0.50 \
  --concurrency 2 --out evals/results/my-batch
```

For a fast smoke batch, use `--quick`: it runs all five faults once with concurrency
five. `--repeats` and `--concurrency` override those defaults. `--k` controls the
leaderboard's pass@k and pass^k cap (default 4); each model/fault row reports n,
correct, and both unbiased pass estimators.

Each model runs every fault with fresh lab/model state. The batch records run order,
prices, settings, all run artifacts, grades, rubric worksheets and a JSON/Markdown
leaderboard. The output directory must be new. A shared budget guard samples key
usage before/after runs and reserves up to 64k prompt tokens (configurable with
`--max-input-tokens`) plus 4096 output tokens with a 10% margin before every call.
Cache and prompt rates are treated as alternative prompt-side rates, using the highest
catalogue tier. The bounded adapter refuses an oversize prompt before sending it.
Failures before any provider response (HTTP errors and connection failures) release
their reservation. Unknown-cost responses and post-send timeouts retain it; a response
generation id is looked up once for its final cost first. Provider price ceilings constrain
routing. Unsupported billing dimensions and reservations that cannot fit are recorded
as skipped attempts, while the batch continues with other models.
Zero-price models work with a zero budget. Aggregate key usage can be delayed or
include other callers, so conservative reservations can stop a batch early.

`--model scripted --models scripted --script tests/scripted-demo.json` runs without
network calls. Grade verdicts are correct, partial, wrong, inconclusive or no_report.
Correct requires the top hypothesis to name the culprit, match mechanism patterns
and cite a retrieved key observation. Citation grounding measures provenance, not
semantic entailment. Complete the five-dimension human worksheet before claiming
semantic quality. Missing usage stays unknown; wall time and summed role durations
are separate. Initial results are exploratory with no pass threshold or superiority
claim. Historical results are in `evals/results/exploratory-2026-10-19`.
