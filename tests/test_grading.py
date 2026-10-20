"""Regression: live failures were bounded but lacked accepted evidence/hypotheses.
Evaluator checks retain those failures and distinguish provenance from semantics.
"""

from argparse import Namespace
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from incident_investigator.grading.budget import BoundedModel, SpendGuard, price_bound
from incident_investigator.grading.grader import FAMILIES, expectation, grade, grade_run, matches
from incident_investigator.grading.suite import _pass_metrics, run_suite
from incident_investigator.lab.app import MemoryTelemetrySink, create_app
from incident_investigator.models import ModelCallError, ScriptedModel


def result():
    return {
        "run_id": "synthetic",
        "diagnosis": "likely_cause",
        "hypotheses": [
            {
                "statement": "inventory configured database timeout reduced to 50 ms",
                "supporting_evidence": ["key"],
                "contradicting_evidence": [],
            }
        ],
        "report": {"summary": "Diagnosis"},
        "state": {
            "evidence": {
                "key": {
                    "source": "logs",
                    "data": {
                        "service": "inventory",
                        "body": "database query timed out at configured deadline",
                        "attributes": {"db_timeout_ms": 50},
                    },
                }
            },
            "tool_receipts": {"receipt": {}},
        },
        "usage": {"reasoning": {"cost": "unknown", "input_tokens": "unknown"}},
    }


@pytest.mark.parametrize(
    ("stored", "canonical"),
    [
        (None, "hub"),
        ("current", "hub"),
        ("explicit", "staged"),
        ("hub", "hub"),
        ("staged", "staged"),
    ],
)
def test_historical_graph_labels_preserve_run_provenance(stored, canonical):
    r = result()
    if stored is not None:
        r["graph"] = stored
    assert grade(r, expectation("bad_configuration"))["graph"] == canonical


@pytest.mark.parametrize(
    ("variant", "verdict"),
    [
        ("good", "correct"),
        ("service_only", "partial"),
        ("key_only", "partial"),
        ("decoy_first", "wrong"),
        ("inconclusive", "inconclusive"),
        ("empty", "no_report"),
    ],
)
def test_verdicts(variant, verdict):
    r = result()
    h = r["hypotheses"][0]
    if variant == "service_only":
        h["statement"] = "inventory has a problem"
        h["supporting_evidence"] = []
    elif variant == "key_only":
        h["statement"] = "checkout deadline timeout"
    elif variant == "decoy_first":
        r["hypotheses"].insert(
            0, {"statement": "checkout deployment regression", "supporting_evidence": []}
        )
    elif variant == "inconclusive":
        r["diagnosis"] = "inconclusive"
    elif variant == "empty":
        r["report"] = {}
    g = grade(r, expectation("bad_configuration"))
    assert g["verdict"] == verdict
    assert g["resources"]["totals"]["cost"] is None
    assert g["resources"]["totals"]["input_tokens"] is None


@pytest.mark.parametrize("citation", ["unknown", "receipt"])
def test_non_observations_cannot_ground_a_cause(citation):
    r = result()
    r["hypotheses"][0]["supporting_evidence"] = [citation]
    g = grade(r, expectation("bad_configuration"))
    assert g["grounding"]["hypothesis_claim_fraction"] == 0
    assert g["verdict"] == "partial"
    assert g["citations"]["unknown" if citation == "unknown" else "receipt_only"] == [citation]


def test_empty_is_not_perfect_grounding_and_missing_result_is_no_report(tmp_path):
    g = grade_run(tmp_path, expectation("bad_configuration"))
    assert g["verdict"] == "no_report"
    assert g["grounding"]["hypothesis_claim_fraction"] is None
    assert (tmp_path / "grade.md").exists()


def test_future_citation_and_repeated_queries():
    r = result()
    r["state"]["cutoff"] = "2026-01-01T00:00:00Z"
    r["state"]["evidence"]["key"]["available_at"] = "2026-01-02T00:00:00Z"
    r["state"]["calls"] = {
        str(i): {"kind": "tool", "request": {"name": "logs", "arguments": {"service": "inventory"}}}
        for i in range(3)
    }
    g = grade(r, expectation("bad_configuration"))
    assert g["citations"]["unknown"] == ["key"]
    assert g["process"]["repeated_queries"] == 2


@pytest.mark.parametrize("family", FAMILIES)
def test_expectations_match_only_the_injected_lab_signature(family):
    sink = MemoryTelemetrySink()
    with TestClient(create_app(telemetry_sink=sink)) as client:
        client.post("/checkout")
        baseline = deepcopy(sink.observations)
        client.post("/admin/faults", json={"family": family})
        client.post("/checkout")
    expected = expectation(family)
    assert not any(
        matches(r.model_dump(mode="json"), m)
        for r in baseline
        for m in expected["key_evidence_matchers"]
    )
    assert all(
        any(matches(r.model_dump(mode="json"), m) for r in sink.observations)
        for m in expected["key_evidence_matchers"]
    )
    for other in set(FAMILIES) - {family}:
        assert not any(
            matches(r.model_dump(mode="json"), m)
            for r in sink.observations
            for m in expectation(other)["key_evidence_matchers"]
        )


@pytest.mark.parametrize("graph", ["staged"])
async def test_suite_scripted_real_lab_without_network(tmp_path, monkeypatch, graph):
    import httpx

    def forbidden(*args, **kwargs):
        raise AssertionError("network called")

    monkeypatch.setattr(httpx.AsyncClient, "get", forbidden)
    args = Namespace(
        faults="all",
        models="scripted",
        model="scripted",
        repeats=1,
        budget_usd="0",
        out=tmp_path / "batch",
        graph=graph,
    )
    # Exhausted scripts test that real runtime failures stay in the leaderboard.
    board = await run_suite(args, model_factory=lambda _: ScriptedModel())
    assert sum(row["runs"] for row in board["models"]) == 5
    assert len(list(args.out.glob("*/expectation.json"))) == 5
    assert len(list(args.out.glob("*/grade.json"))) == 5
    assert all(row["graph"] == graph for row in board["models"])
    assert all(row["graph"] == graph for row in board["runs"])


async def test_shared_budget_reservations_unknown_and_usage():
    guard = SpendGuard(5, ".10")
    ident = await guard.reserve(Decimal(".06"))
    with pytest.raises(ModelCallError):
        await guard.reserve(Decimal(".06"))
    await guard.finish(ident, None)
    assert guard.unknown == Decimal(".06")
    guard.observe("5.09")
    assert not guard.launch_allowed(Decimal(".02"))
    free = await guard.reserve(Decimal(0))
    await guard.finish(free, 0)
    assert guard.charged() == Decimal(".09")


async def test_guard_reservations_are_atomic_under_concurrency():
    guard = SpendGuard(0, ".10")

    async def reserve():
        try:
            return await guard.reserve(Decimal(".03"))
        except ModelCallError:
            return None

    identifiers = await __import__("asyncio").gather(*(reserve() for _ in range(5)))
    accepted = [ident for ident in identifiers if ident]
    assert len(accepted) == 3
    await __import__("asyncio").gather(*(guard.finish(ident, 0) for ident in accepted))


async def test_429_releases_bounded_reservation():
    class Adapter:
        def model_for_role(self, role):
            return "test/model"

        def build_request(self, *args, **kwargs):
            return {"messages": [], "max_tokens": 1}

        async def complete(self, *args):
            raise ModelCallError("rate limited", retryable=True, status=429, before_response=True)

    guard = SpendGuard(0, 1)
    bounded = BoundedModel(
        Adapter(),
        {"test/model": {"context_length": 1, "pricing": {"prompt": 0, "completion": 0}}},
        guard,
    )
    with pytest.raises(ModelCallError):
        await bounded.complete([], {})
    assert guard.snapshot()["active"] == "0" and guard.unknown == 0


@pytest.mark.parametrize("graph", ["staged"])
async def test_suite_parallel_jobs_have_separate_artifacts(tmp_path, graph):
    import asyncio
    import json

    started = asyncio.Event()
    active = 0
    selected = []

    async def demo(args):
        nonlocal active
        selected.append(args.graph)
        active += 1
        if active == 5:
            started.set()
        await asyncio.wait_for(started.wait(), 1)
        directory = args.out / str(__import__("uuid").uuid4())
        directory.mkdir()
        (directory / "result.json").write_text(json.dumps(result()))
        return directory

    args = Namespace(
        faults="all",
        models="scripted",
        model="scripted",
        repeats=1,
        concurrency=5,
        budget_usd="0",
        out=tmp_path / "batch",
        graph=graph,
    )
    board = await run_suite(args, demo_fn=demo, model_factory=lambda _: ScriptedModel())
    assert len(board["runs"]) == 5
    assert len(list(args.out.glob("*/grade.json"))) == 5
    assert selected == [graph] * 5
    assert all(row["graph"] == graph for row in board["models"])
    assert all(row["graph"] == graph for row in board["runs"])


def test_pass_metrics_are_unbiased_estimators():
    assert _pass_metrics(4, 2, 2) == (5 / 6, 1 / 6)


def test_price_bound_and_unpriced_fee():
    info = {
        "context_length": 1000,
        "pricing": {"prompt": ".000001", "completion": ".000002", "request": "0"},
    }
    assert price_bound(info) == Decimal(".0101112")
    info["pricing"]["request"] = ".01"
    with pytest.raises(ValueError):
        price_bound(info)


@pytest.mark.parametrize("graph", ["staged"])
async def test_suite_successful_script_is_reset_for_every_fault(tmp_path, graph):
    import json

    actions = json.loads(Path("tests/scripted-demo.json").read_text())
    args = Namespace(
        faults="all",
        models="scripted",
        model="scripted",
        repeats=2,
        concurrency=5,
        budget_usd="0",
        out=tmp_path / "batch",
        graph=graph,
    )
    board = await run_suite(args, model_factory=lambda _: ScriptedModel(actions))
    assert sum(row["runs"] for row in board["models"]) == 10
    assert all(g["verdict"] == "inconclusive" for g in board["runs"])
    assert all(not g["process"]["failed_roles"] for g in board["runs"])
    assert all(
        json.loads(path.read_text())["graph"] == graph for path in args.out.glob("*/result.json")
    )


async def test_guarded_adapter_enforces_ceiling_and_retains_error_reservation():
    import os
    from unittest.mock import AsyncMock, patch

    from incident_investigator.grading.budget import BoundedModel
    from incident_investigator.models import OpenRouterModel
    from incident_investigator.models.adapter import ModelResult, ModelUsage

    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic-test-key"}):
        adapter = OpenRouterModel()
    catalog = {
        adapter.model_for_role("orchestrator"): {
            "context_length": 1000,
            "pricing": {"prompt": ".000001", "completion": ".000002"},
        }
    }
    guard = SpendGuard(0, ".10")
    bounded = BoundedModel(adapter, catalog, guard)
    adapter.complete = AsyncMock(
        return_value=ModelResult({}, ModelUsage(10, 10, 0.001), 1, "synthetic")
    )
    await bounded.complete([], {})
    assert guard.returned == Decimal(".001")
    request = adapter.build_request(
        [], {}, bounded.model_for_role("orchestrator"), structured=False
    )
    assert request["provider"]["max_price"] == {"prompt": 1.0, "completion": 2.0, "request": 0}
    adapter.complete = AsyncMock(side_effect=TimeoutError)
    with pytest.raises(TimeoutError):
        await bounded.complete([], {})
    assert guard.unknown == Decimal(guard.records[0]["reservation"])
    assert guard.unknown < price_bound(next(iter(catalog.values())))


async def test_usage_sampling_and_zero_budget_free_model(tmp_path, monkeypatch):
    import json

    import httpx

    from incident_investigator.grading import suite
    from incident_investigator.models import OpenRouterModel

    usages = iter([7, 7, 7, 7])
    samples = []

    async def usage(client):
        value = next(usages)
        samples.append(value)
        return value

    async def catalogue(client, url, **kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "data": [
                    {
                        "id": "test/free",
                        "context_length": 1000,
                        "pricing": {"prompt": "0", "completion": "0"},
                    }
                ]
            },
        )

    monkeypatch.setattr(suite, "key_usage", usage)
    monkeypatch.setattr(httpx.AsyncClient, "get", catalogue)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")

    # Keep real paid-path initialization and shared guard, with a local scripted call.
    async def fake_demo(args):
        from incident_investigator import cli

        await cli.model(args).complete([], {})
        directory = args.out / "synthetic"
        directory.mkdir()
        (directory / "result.json").write_text(json.dumps(result()))
        return directory

    async def completion(self, messages, schema):
        from incident_investigator.models.adapter import ModelResult, ModelUsage

        return ModelResult({}, ModelUsage(1, 1, 0), 0, "synthetic")

    monkeypatch.setattr(OpenRouterModel, "complete", completion)
    args = Namespace(
        faults="bad_configuration",
        models="test/free",
        model="openrouter",
        repeats=1,
        budget_usd="0",
        out=tmp_path / "batch",
    )
    board = await run_suite(args, demo_fn=fake_demo)
    assert len(board["runs"]) == 1
    assert samples == [7, 7, 7, 7]
    assert (args.out / "spend-after.json").exists()


def test_decoy_and_repair_reporting():
    r = result()
    r["hypotheses"].append(
        {
            "statement": "database contention",
            "supporting_evidence": [],
            "contradicting_evidence": ["key"],
        }
    )
    r["report"]["summary"] = "No evidence for database contention"
    events = [{"event": "validation", "outcome": v} for v in ["accepted", "warning", "rejected"]]
    g = grade(r, expectation("bad_configuration"), events)
    decoy = g["alternatives"][0]
    assert (
        decoy["ranked_down"] and decoy["contradicted"] and decoy["explicitly_ruled_out_in_report"]
    )
    assert g["process"]["invalid_outputs"] == 1
    assert g["process"]["repaired_outputs"] == 1


def test_catalogue_cache_reasoning_and_inactive_modalities_are_bounded():
    info = {
        "context_length": 1000,
        "pricing": {
            "prompt": ".000001",
            "completion": ".000002",
            "input_cache_read": ".0000001",
            "input_cache_write": ".0000005",
            "internal_reasoning": ".000003",
            "image": "1",
            "web_search": "1",
        },
    }
    assert price_bound(info) == (Decimal(".001") + 4096 * Decimal(".000003")) * Decimal("1.10")


def test_catalogue_cache_variants_and_overrides_use_highest_prompt_side_rate():
    haiku = {
        "context_length": 1_000_000,
        "pricing": {
            "prompt": ".0000001",
            "completion": ".0000005",
            "input_cache_write_1h": ".0000002",
            "overrides": [
                {
                    "min_prompt_tokens": 100_000,
                    "prompt": ".0000005",
                    "completion": ".0000025",
                    "input_cache_write_1h": ".000001",
                }
            ],
        },
    }
    gemini = {
        "context_length": 1_048_576,
        "pricing": {
            "prompt": ".00000025",
            "completion": ".0000015",
            "input_cache_read": ".000000025",
            "input_cache_write": ".0000000833333333333333",
            "internal_reasoning": ".0000015",
        },
    }
    assert price_bound(haiku) == Decimal(".08166400")
    assert price_bound(gemini) == Decimal(".02435840000000000000000000000")


async def test_bounded_model_refuses_oversize_prompt_without_provider_request():
    import os
    from unittest.mock import AsyncMock, patch

    from incident_investigator.models import OpenRouterModel

    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic-test-key"}):
        adapter = OpenRouterModel()
    model = adapter.model_for_role("orchestrator")
    bounded = BoundedModel(
        adapter,
        {
            model: {
                "context_length": 1000,
                "pricing": {"prompt": ".000001", "completion": ".000002"},
            }
        },
        SpendGuard(0, 1),
        max_input_tokens=10,
    )
    adapter.complete = AsyncMock()
    with pytest.raises(ModelCallError, match="input token limit"):
        await bounded.complete([{"role": "user", "content": "x" * 100}], {})
    adapter.complete.assert_not_awaited()


async def test_returned_cost_over_reservation_fences_later_calls():
    guard = SpendGuard(0, "1")
    ident = await guard.reserve(Decimal(".01"))
    await guard.finish(ident, ".02")
    with pytest.raises(ModelCallError):
        await guard.reserve(Decimal(0))


async def test_suite_stops_launching_when_price_reservation_exceeds_cap(tmp_path, monkeypatch):
    import httpx

    from incident_investigator.grading import suite

    async def usage(client):
        return 0

    async def catalogue(client, url):
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "data": [
                    {
                        "id": "test/paid",
                        "context_length": 1000,
                        "pricing": {"prompt": ".001", "completion": ".001"},
                    }
                ]
            },
        )

    async def forbidden(args):
        raise AssertionError("budget must stop before demo launch")

    monkeypatch.setattr(suite, "key_usage", usage)
    monkeypatch.setattr(httpx.AsyncClient, "get", catalogue)
    args = Namespace(
        faults="all",
        models="test/paid",
        model="openrouter",
        repeats=1,
        budget_usd=".01",
        out=tmp_path / "batch",
    )
    board = await run_suite(args, demo_fn=forbidden)
    assert board["runs"] == []
    assert (args.out / "budget-stop.json").exists()


async def test_suite_skips_unaffordable_model_and_runs_later_model(tmp_path, monkeypatch):
    import json

    import httpx

    from incident_investigator.grading import suite
    from incident_investigator.models import OpenRouterModel

    async def usage(client):
        return 0

    async def catalogue(client, url, **kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "data": [
                    {
                        "id": "test/unaffordable",
                        "context_length": 1000,
                        "pricing": {"prompt": ".001", "completion": ".001"},
                    },
                    {
                        "id": "test/cheap",
                        "context_length": 1000,
                        "pricing": {"prompt": "0", "completion": "0"},
                    },
                ]
            },
        )

    async def completion(self, messages, schema):
        from incident_investigator.models.adapter import ModelResult, ModelUsage

        return ModelResult({}, ModelUsage(1, 1, 0), 0, "synthetic")

    async def fake_demo(args):
        from incident_investigator import cli

        await cli.model(args).complete([], {})
        directory = args.out / "synthetic"
        directory.mkdir()
        (directory / "result.json").write_text(json.dumps(result()))
        return directory

    monkeypatch.setattr(suite, "key_usage", usage)
    monkeypatch.setattr(httpx.AsyncClient, "get", catalogue)
    monkeypatch.setattr(OpenRouterModel, "complete", completion)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")
    args = Namespace(
        faults="bad_configuration",
        models="test/unaffordable,test/cheap",
        model="openrouter",
        repeats=1,
        budget_usd=".01",
        out=tmp_path / "batch",
    )
    board = await run_suite(args, demo_fn=fake_demo)
    assert len(board["runs"]) == 1
    assert board["runs"][0]["suite_attempt"]["model"] == "test/cheap"
    batch = json.loads((args.out / "batch.json").read_text())
    assert batch["skipped"][0]["model"] == "test/unaffordable"
    assert board["skipped"] == batch["skipped"]
