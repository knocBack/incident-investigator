# Incident Investigator: take-home submission

A team of AI agents that investigates a production incident the way an on-call engineer would. It reads logs, metrics, deploy history and service dependencies, forms hypotheses, has them challenged by a critic, and writes a report that cites its evidence.

## Deliverables

| Deliverable | Where |
| --- | --- |
| Runnable implementation and setup | [Repository README](../../README.md) and the quick start below |
| Architecture diagram and how the agents coordinate | [architecture.md](architecture.md) |
| Key decisions | [decisions.md](decisions.md) |
| Synthetic dataset and automated tests | Fault lab in `src/incident_investigator/lab`, frozen case in `fixtures/`, expected answers in `evals/expected/`, 210 automated tests in `tests/` |
| Evaluation results so far | [evaluation.md](evaluation.md) |
| Technical note (500 words max) | [technical-note.md](technical-note.md) |
| Eval console screenshots | [ui.md](ui.md) |
| Coding-assistant transcript | *To be attached by the author* |

## Where the project stands

| Stage | What it covers | Status |
| --- | --- | --- |
| M0 to M2 | Project setup, fault-injection lab, five evidence tools | Done |
| M3 | Six-agent investigation, end to end, on a staged graph | Done |
| Evals | Automatic grading, pass@k and pass^k, quick mode, budget cap | Done |
| Eval console | Web console with live agent traces in Arize Phoenix | In testing ([PR #12](https://github.com/knocBack/incident-investigator/pull/12)) |
| M4 | Reliability: resume after a crash, recovery from tool failures | Left |
| M5 | 10-incident benchmark and single-agent baseline | Left |
| M6 | Final single-agent vs multi-agent comparison | Left |

**Why the benchmark is late:** each investigation takes 5 to 8 minutes (about 20 model calls, made one after another). The first paid runs also exposed a cost limiter that blocked valid runs, which had to be fixed before results could be trusted. A complete benchmark will be shared once the in-progress test finishes.

## Quick start

```sh
uv sync --locked
uv run pytest -q
# Deterministic demo, no API key needed
uv run incident-investigator demo --model scripted --script tests/scripted-demo.json --out runs/
# Real model through OpenRouter
export OPENROUTER_API_KEY=...
export II_OPENROUTER_MODEL=deepseek/deepseek-v3.2
uv run incident-investigator demo --model openrouter --fault deployment_regression --out runs/
# Quick evaluation: 5 faults, 1 run each, 2 at a time
uv run incident-investigator eval-suite --quick --concurrency 2 --models deepseek/deepseek-v3.2
```

Each run saves `report.md`, `result.json`, `trace.jsonl` and a `state.sqlite` checkpoint.

## How it was built

I designed the system and learned the domain with Codex and web research: requirements, high-level and low-level design, and the evaluation plan (see `docs/`). I then implemented it with Firstmate running on Claude Code as the orchestrator, dispatching Codex agents as the main workers. Every change went through its own pull request, with automated CodeRabbit review and CI (11 merged PRs).
