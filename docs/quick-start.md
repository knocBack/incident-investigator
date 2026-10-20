# Quick start

Run commands from the repository root with `uv` and Python 3.13. All scripted commands below were exercised without a provider key. Model-backed commands are documented entry points and were not run during this change.

## 1. Run the free demo

```sh
uv sync --locked
uv run incident-investigator demo --model scripted --script tests/scripted-demo.json
```

The command runs the fault lab in-process, freezes its telemetry and replays canned agent actions. It prints `var/runs/<run-id>/report.md`. The expected outcome is a completed, inconclusive report. Scripted output checks execution and artifacts; it does not measure diagnosis quality.

## 2. Inspect and grade that run

Use the printed run directory in these templates:

```sh
uv run incident-investigator inspect var/runs/<run-id>
uv run incident-investigator eval var/runs/<run-id>
```

`inspect` prints saved exports. `eval` makes no model calls and writes a separate grading suite under `var/suites/`. The demo saves an evaluator expectation pointer so no fault argument is needed. Replace angle-bracket values before running template commands.

## 3. Run the primary set without a key

```sh
uv run incident-investigator eval-suite --model scripted --models scripted \
  --script tests/scripted-demo.json --quick --budget-usd 0
```

This creates a fresh suite with one attempt for each of the ten primary incidents, concurrency five and JSON/Markdown leaderboards. The replay reports inconclusive for all ten; a successful command is not a correct diagnostic grade. Each new attempt starts with its own lab and replay state.

## 4. Open the console

```sh
uv run incident-investigator console
```

Open **http://127.0.0.1:8765**. Runs and suites under `var/` appear automatically. Open a run, inspect its Report and Frozen evidence, then use Grade or Grade this run. The [console guide](ui.md) covers launching jobs, the human rubric and comparisons.

## 5. Use OpenRouter

In the terminal that launches the CLI or console:

```sh
export OPENROUTER_API_KEY='replace-with-your-key'
uv run incident-investigator demo --model openrouter --fault deployment_regression
```

The adapter has a default model; `II_OPENROUTER_MODEL` or `--model-id` selects a complete provider/model ID. The live profile permits 90 seconds per call and 1,200 seconds per run. Model availability, access and charges depend on the provider. A demo has call/time bounds but no shared monetary cap.

For a suite with a monetary guard, supply an available model ID:

```sh
uv run incident-investigator eval-suite --model openrouter \
  --models "$II_OPENROUTER_MODEL" --faults deployment_regression \
  --repeats 1 --concurrency 1 --budget-usd 0.50
```

This template requires a nonempty `II_OPENROUTER_MODEL`. The budget guard reserves before calls and may stop early when unknown charges remain reserved. Inspect the batch's skipped attempts and `budget.json`; unused balance does not guarantee another call fits. See [usage](usage.md) for overrides, recovery and accounting.

## Workspace

`II_HOME` changes the default workspace; `--out` overrides an individual command's output. Keep the same home when launching the console to see those results:

```sh
II_HOME=/tmp/ii-workspace uv run incident-investigator demo --model scripted --script tests/scripted-demo.json
II_HOME=/tmp/ii-workspace uv run incident-investigator console --port 8766
```

The workspace holds local evidence and model output. Imported historical roots are indexed read-only. New grades and reviews are kept inside the selected workspace.
