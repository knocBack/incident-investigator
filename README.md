# incident-investigator

Local multi-agent incident investigation MVP.

Status: M2 frozen-source retrieval tools and deterministic model adapter.

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
