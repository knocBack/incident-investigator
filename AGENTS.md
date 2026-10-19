# Contributor guide

## Purpose

This repository builds a local, evidence-based multi-agent incident investigation MVP.

## Layout

- `src/`: application package.
- `tests/`: automated checks.
- `docs/`: accepted product and design specifications.
- `config/`: local development profiles.

## Working rules

- Use Python 3.13 and `uv sync --locked` before running `uv run pytest -q`; install local hooks with `uv run pre-commit install`.
- Keep runtime agents isolated from evaluator expectations, private grading data, planning documents, and repository-reading tools. Evaluator information must never enter prompts or tool-visible sources.
- Treat `docs/` as accepted design specifications. Update them only when a separately accepted design change requires it.
- Keep pull requests lean: make one focused change, add the smallest relevant test, and avoid unrelated cleanup.
- Prefer structured tool inputs and outputs. Do not hardcode incident answers.
- Preserve finite execution limits and local-only artifacts.
