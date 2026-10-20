"""M5 regression: paired snapshots, causal grading and honest missing evidence.

The original five keys remain unchanged. These are exposed development cases, not
an unseen benchmark. Scripted outcomes validate plumbing, not model capability.
"""

import json
from argparse import Namespace
from copy import deepcopy

import pytest

from incident_investigator.contracts import Limits
from incident_investigator.grading.grader import expectation, grade, matches
from incident_investigator.grading.suite import run_suite
from incident_investigator.lab.cases import SECOND_CASES, VARIANTS, freeze_case
from incident_investigator.models import ScriptedModel
from incident_investigator.runtime import investigate
from incident_investigator.sources.loader import load_snapshot

STATEMENTS = {
    "case-02": "Checkout shortened its Inventory deadline to 50 ms below normal response latency",
    "case-04": "Checkout response handling assumes missing stock_count field",
    "case-06": "Inventory inconsistent lock ordering creates database deadlocks",
    "case-08": "Inventory memory exhaustion with probable cache growth",
    "case-10": "Checkout retries amplify attempts and sustain Inventory overload",
    "reliability-01": "Inventory connection leases remain owned after completion causing pool leak",
    "reliability-02": "Checkout new release is suspect; exact mechanism inconclusive",
}


def final(result):
    return {"kind": "final", "result": result}


class CaseScript(ScriptedModel):
    """Deterministic role script, deliberately evaluator-owned and never shipped."""

    def __init__(self, case):
        super().__init__()
        self.case = case

    async def complete(self, messages, schema):
        context = next(
            json.loads(m["content"])
            for m in messages
            if m["role"] == "user" and m["content"].startswith('{"role":')
        )
        role = context["role"]
        evidence = dict(context["evidence"])
        receipts = []
        for message in messages:
            try:
                item = json.loads(message["content"])
            except (ValueError, TypeError):
                continue
            if "tool_receipt_id" in item:
                receipts.append(item)
                evidence.update({r["evidence_id"]: r for r in item.get("observations", [])})
        refs = list(evidence)
        report = dict(
            revision_id=context["hypothesis_revision"],
            summary=STATEMENTS[self.case],
            facts=[],
            assumptions=[],
            recommendations=[],
            unresolved_objection_ids=[],
        )
        reasoning = dict(
            hypotheses=[
                dict(
                    hypothesis_id="h1",
                    statement=STATEMENTS[self.case],
                    confidence="medium",
                    supporting_evidence=refs,
                    contradicting_evidence=[],
                    assumptions=[],
                )
            ],
            conclusion="inconclusive" if self.case == "reliability-02" else "likely_cause",
            explanation="Compared retrieved observations.",
        )
        if self.case == "reliability-02":
            report["summary"] += (
                "; logs and configuration diff unavailable; need to obtain caller logs and inspect effective configuration."
            )
        if role in {"single", "observability", "dependency"} and not receipts:
            names = [
                ("logs", "inventory"),
                ("logs", "checkout"),
                ("logs", "database"),
                ("metrics", "inventory"),
                ("metrics", "checkout"),
            ]
            if role == "dependency":
                names = [
                    ("changes", "checkout"),
                    ("changes", "inventory"),
                    ("metric_stats", "database"),
                    ("dependencies", "checkout"),
                ]
            action = dict(
                kind="tools", requests=[dict(name=n, arguments={"service": s}) for n, s in names]
            )
        elif role == "single" and len(receipts) == 5:
            action = dict(
                kind="tools",
                requests=[
                    dict(name=n, arguments={"service": s})
                    for n, s in [
                        ("changes", "checkout"),
                        ("changes", "inventory"),
                        ("metrics", "database"),
                    ]
                ],
            )
        elif role == "orchestrator":
            action = final(
                dict(
                    assignments=[
                        dict(role=r, question="Gather observations and controls", objection_id=None)
                        for r in ("observability", "dependency")
                    ],
                    route="gather",
                    dispositions=[],
                )
            )
        elif role in {"observability", "dependency"}:
            action = final(dict(findings=[], gaps=[], outputs={}))
        elif role == "reasoning":
            action = final(reasoning)
        elif role == "critic":
            action = final(
                dict(
                    revision_id=context["hypothesis_revision"],
                    objections=[],
                    summary="No further objections",
                )
            )
        elif role == "reporting":
            action = final(report)
        else:
            action = final({**reasoning, "report": report})
        self.actions.append(action)
        return await super().complete(messages, schema)


@pytest.mark.parametrize("case", list(SECOND_CASES) + list(VARIANTS))
async def test_case_fixture_and_grader_controls(tmp_path, case):
    await freeze_case(case, tmp_path)
    snap = load_snapshot(case, tmp_path)
    assert len({r.source for r in snap.observations}) == 5
    assert all(r.available_at <= snap.cutoff for r in snap.observations)
    expected = expectation(case)
    for group in expected["required_evidence_groups"]:
        assert any(
            any(matches(r.model_dump(), matcher) for matcher in group) for r in snap.observations
        )
    if case == "case-02":
        baseline = [
            r
            for r in snap.observations
            if r.source == "metrics"
            and r.data["service"] == "inventory"
            and r.data["name"] == "request_latency"
            and r.event_time.hour == 13
        ]
        assert all(80 <= r.data["value"] <= 120 for r in baseline)
    if case == "case-08":
        assert not [
            r
            for r in snap.observations
            if r.source == "metrics"
            and r.data["name"] == "rss_mib"
            and 4 <= r.event_time.minute <= 6
        ]
    if case == "reliability-02":
        assert "inventroy" not in json.dumps([r.model_dump(mode="json") for r in snap.observations])
        assert not [
            r for r in snap.observations if r.source == "logs" and r.data["service"] == "checkout"
        ]
    # Same synthetic day and switch sequence yields byte-identical observation identity.
    other = tmp_path / "again"
    other.mkdir()
    await freeze_case(case, other)
    assert load_snapshot(case, other).dataset_digest == snap.dataset_digest


@pytest.mark.parametrize("case", list(SECOND_CASES) + list(VARIANTS))
@pytest.mark.parametrize("system", ["multi", "single"])
async def test_scripted_cases_both_systems_and_grading(tmp_path, case, system):
    source = tmp_path / "source"
    source.mkdir()
    await freeze_case(case, source)
    snap = load_snapshot(case, source)
    model = CaseScript(case)
    directory = await investigate(
        snap,
        model,
        tmp_path / "runs",
        system=system,
        limits=Limits(call_seconds=0.3, wall_seconds=20),
    )
    result = json.loads((directory / "result.json").read_text())
    assert result["status"] == "completed", result["stop_reason"]
    if case == "reliability-02":
        assert result["stop_category"] == "inconclusive"
    g = grade(result, expectation(case))
    assert g["verdict"] == "correct", g["diagnosis_checks"]
    assert g["citations"]["unknown"] == []
    if case == "case-04":
        supplemented = deepcopy(result)
        receipt_ids = list(result["state"]["tool_receipts"])[:2]
        supplemented["hypotheses"][0]["supporting_evidence"].extend(receipt_ids)
        graded = grade(supplemented, expectation(case))
        assert graded["verdict"] == "correct"
        assert graded["grounding"]["hypothesis_claim_fraction"] == 1
        for invalid_kind in ("invented", "foreign", "late"):
            invalid = deepcopy(supplemented)
            invalid["hypotheses"][0]["supporting_evidence"].append("bad-reference")
            if invalid_kind != "invented":
                row = deepcopy(next(iter(invalid["state"]["evidence"].values())))
                if invalid_kind == "foreign":
                    row["dataset_digest"] = "foreign-dataset"
                else:
                    row["available_at"] = "2027-01-01T00:00:00Z"
                invalid["state"]["evidence"]["bad-reference"] = row
            checked = grade(invalid, expectation(case))
            assert checked["verdict"] != "correct"
            assert not checked["diagnosis_checks"]["support_references_valid"]
        supplemented["hypotheses"][0]["supporting_evidence"] = receipt_ids
        assert grade(supplemented, expectation(case))["verdict"] != "correct"
        assert grade(supplemented, expectation(case))["grounding"]["hypothesis_claim_fraction"] == 0
    if system == "single":
        assert {t["role"] for t in result["state"]["tasks"].values()} == {"single"}
        assert set(result["usage"]) == {"single"}
        assert result["report"]["revision_id"] == result["state"]["hypothesis_revision"]
        context = json.loads(model.requests[0][0][1]["content"])
        assert "changes" in context["tools"] and "logs" in context["tools"]
        assert "NO native or callable tools" in model.requests[0][0][0]["content"]
    if case == "reliability-01":
        assert g["reliability"]["fault_exercised"] and g["reliability"]["recovered"]
        assert g["reliability"]["success"]
        bypass = deepcopy(result)
        bypass["state"]["calls"] = {}
        bypass_grade = grade(bypass, expectation(case))
        assert bypass_grade["verdict"] == "correct"
        assert bypass_grade["reliability"]["status"] == "not_exercised"
        assert not bypass_grade["reliability"]["success"]
        from incident_investigator.grading.suite import reliability_metrics

        recovery_metrics = reliability_metrics([g, bypass_grade])["recovery"]
        assert recovery_metrics["runs"] == 2 and recovery_metrics["not_exercised"] == 1
        assert recovery_metrics["success"] == recovery_metrics["recovered"] == 1
        failed = deepcopy(result)
        successors = [c for c in failed["state"]["calls"].values() if c.get("retry_of")]
        for successor in successors:
            failed["receipts"].pop(successor["call_id"], None)
        assert not grade(failed, expectation(case))["reliability"]["recovered"]
        unbounded = deepcopy(result)
        for call in unbounded["state"]["calls"].values():
            if call.get("retry_of"):
                call["retry"] = 10
        assert not grade(unbounded, expectation(case))["reliability"]["bounded_successor"]
        calls = [
            c
            for c in result["state"]["calls"].values()
            if (c.get("fault") or {}).get("rule_id") == "source-01"
        ]
        assert len(calls) == 2 and calls[1]["retry"] == 1
    broken = deepcopy(result)
    broken["hypotheses"][0]["supporting_evidence"] = []
    assert grade(broken, expectation(case))["verdict"] != "correct"
    broken = deepcopy(result)
    broken["hypotheses"][0]["statement"] = "Database capacity shortage"
    assert grade(broken, expectation(case))["verdict"] != "correct"
    if case == "reliability-02":
        assert g["reliability"]["justified_uncertainty"]
        broken = deepcopy(result)
        broken["report"]["summary"] += " Hostname inventroy is misspelled."
        assert grade(broken, expectation(case))["verdict"] == "wrong"
        broken = deepcopy(result)
        broken["status"] = "failed"
        assert grade(broken, expectation(case))["verdict"] == "wrong"


async def test_suite_pairs_snapshots_and_separates_reliability(tmp_path):
    args = Namespace(
        faults="case-02,reliability-02",
        set="all",
        system="both",
        graph="staged",
        models="scripted",
        model="scripted",
        repeats=2,
        concurrency=2,
        budget_usd="0",
        out=tmp_path / "batch",
        k=2,
    )
    board = await run_suite(args, model_factory=lambda _: CaseScript("case-02"))
    assert len(board["runs"]) == 8
    assert {(r["system"], r["suite_kind"]) for r in board["models"]} == {
        (s, k) for s in ("single", "multi") for k in ("primary", "reliability")
    }
    batch = json.loads((args.out / "batch.json").read_text())
    assert len(batch["dataset_digests"]) == 2
    for row in board["models"]:
        for metrics in row["faults"].values():
            assert metrics["n"] == 2 and metrics["k"] == 2
            assert "pass_at_1" in metrics
            assert metrics["resources"]["cost"] is None


@pytest.mark.parametrize(
    ("selection", "faults"),
    [("primary", "reliability-01"), ("variants", "case-02"), ("all", "unknown")],
)
async def test_suite_faults_must_narrow_the_named_set(tmp_path, selection, faults):
    from incident_investigator import cli

    out = tmp_path / "rejected"
    args = cli.build_parser().parse_args(
        [
            "eval-suite",
            "--model",
            "scripted",
            "--models",
            "scripted",
            "--set",
            selection,
            "--faults",
            faults,
            "--out",
            str(out),
        ]
    )
    with pytest.raises(ValueError, match="must be a subset"):
        await run_suite(args)
    assert not out.exists()


async def test_run_and_suite_spend_limits_both_apply():
    from decimal import Decimal

    from incident_investigator.grading.budget import RunSpendGuard, SpendGuard
    from incident_investigator.models import ModelCallError

    suite = SpendGuard(0, ".10")
    run = RunSpendGuard(suite, ".03")
    ident = await run.reserve(Decimal(".02"))
    with pytest.raises(ModelCallError):
        await run.reserve(Decimal(".02"))
    assert sum(suite.active.values()) == Decimal(".02")
    await run.finish(ident, ".01")
    other = RunSpendGuard(suite, ".10")
    ident = await other.reserve(Decimal(".08"))
    with pytest.raises(ModelCallError):
        await run.reserve(Decimal(".02"))
    assert not run.active
    await other.release(ident)
    assert not suite.active


async def test_single_total_allowance_exceeds_specialist_allowance(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    await freeze_case("case-02", source)
    directory = await investigate(
        load_snapshot("case-02", source),
        CaseScript("case-02"),
        tmp_path / "runs",
        system="single",
        limits=Limits(task_model_calls=1, task_tool_calls=1),
    )
    result = json.loads((directory / "result.json").read_text())
    assert result["status"] == "completed"
    assert result["usage"]["single"]["model_calls"] == 3
    assert result["usage"]["single"]["tool_calls"] == 8
    replay = ScriptedModel()
    assert await investigate(None, replay, resume=directory) == directory
    assert replay.requests == []
    resumed = json.loads((directory / "result.json").read_text())
    assert (
        resumed["system"] == "single"
        and resumed["graph"] == "staged"
        and resumed["usage"] == result["usage"]
    )


async def test_three_run_concurrent_suite_fits_eleven_cent_budget(tmp_path):
    """Regression: byte-as-token plus duplicate-schema reserves rejected cheap runs."""
    from dataclasses import replace
    from decimal import Decimal

    from incident_investigator.grading.budget import (
        BoundedModel,
        SpendGuard,
        estimated_input_tokens,
    )
    from incident_investigator.models import OpenRouterModel
    from incident_investigator.models.adapter import ModelUsage

    model_id = "deepseek/deepseek-v3.2"
    catalog = {
        model_id: {
            "context_length": 163840,
            "supported_parameters": ["structured_outputs"],
            "pricing": {"prompt": ".00000028", "completion": ".00000042"},
        }
    }
    guard = SpendGuard(0, ".11")

    class UsageScript(CaseScript):
        build_request = staticmethod(OpenRouterModel.build_request)

        def model_for_role(self, role):
            return model_id

        async def complete(self, messages, schema):
            context = json.loads(messages[1]["content"])
            self.case = context["alert"]["case_id"]
            response = await super().complete(messages, schema)
            estimate = estimated_input_tokens(
                self.build_request(messages, schema, model_id, structured=True)
            )
            # Billed-token double uses representative observed role sizes; it does
            # not pretend the request estimate is an exact provider tokenizer.
            role_tokens = {
                "orchestrator": 700,
                "observability": 10000,
                "dependency": 8000,
                "reasoning": 16000,
                "critic": 16000,
                "reporting": 18000,
            }
            tokens = min(estimate, role_tokens[context["role"]])
            cost = float(Decimal(tokens) * Decimal(".00000028") + 512 * Decimal(".00000042"))
            return replace(response, usage=ModelUsage(tokens, 512, cost), model=model_id)

    args = Namespace(
        faults="case-02,case-04,case-10",
        set="primary",
        system="multi",
        graph="staged",
        models=model_id,
        model="scripted",
        repeats=1,
        concurrency=2,
        budget_usd=".11",
        out=tmp_path / "batch",
        max_input_tokens=180000,
        k=1,
    )
    board = await run_suite(
        args,
        model_factory=lambda _: BoundedModel(
            UsageScript("case-02"), catalog, guard, max_input_tokens=180000
        ),
    )
    assert [g["verdict"] for g in board["runs"]] == ["correct"] * 3
    assert guard.charged() < Decimal(".11")
    assert guard.unknown == 0
    assert all(g["resources"]["totals"]["input_tokens"] > 0 for g in board["runs"])
    assert all(g["resources"]["totals"]["cost"] > 0 for g in board["runs"])


async def test_unbilled_admission_refusal_preserves_completed_usage(tmp_path):
    from dataclasses import replace

    from incident_investigator.models import ModelCallError
    from incident_investigator.models.adapter import ModelUsage

    class RefuseAfterCall(CaseScript):
        async def complete(self, messages, schema):
            if self.requests:
                raise ModelCallError("local reservation refused", retryable=False, before_send=True)
            response = await super().complete(messages, schema)
            return replace(response, usage=ModelUsage(123, 45, 0.001))

    source = tmp_path / "source"
    source.mkdir()
    await freeze_case("case-04", source)
    directory = await investigate(
        load_snapshot("case-04", source),
        RefuseAfterCall("case-04"),
        tmp_path / "runs",
        system="single",
    )
    result = json.loads((directory / "result.json").read_text())
    assert result["usage"]["single"]["input_tokens"] == 123
    assert result["usage"]["single"]["output_tokens"] == 45
    assert result["usage"]["single"]["cost"] == 0.001


async def test_recovered_generation_cost_reaches_result():
    from incident_investigator.grading.budget import BoundedModel, SpendGuard
    from incident_investigator.models import OpenRouterModel
    from incident_investigator.models.adapter import ModelResult, ModelUsage

    class Adapter:
        build_request = staticmethod(OpenRouterModel.build_request)

        def model_for_role(self, role):
            return "test/model"

        async def complete(self, messages, schema):
            return ModelResult({}, ModelUsage(10, 5, None), 0, "generation", "test/model")

        async def generation_cost(self, identity):
            return 0.00001

    bounded = BoundedModel(
        Adapter(),
        {
            "test/model": {
                "context_length": 100000,
                "pricing": {"prompt": ".000001", "completion": ".000001"},
            }
        },
        SpendGuard(0, 1),
    )
    response = await bounded.complete([], {})
    assert response.usage == ModelUsage(10, 5, 0.00001)


async def test_cancelled_suite_retains_partial_reports_and_leaderboard(tmp_path):
    import asyncio
    from dataclasses import replace

    from incident_investigator.models.adapter import ModelUsage

    started = asyncio.Event()
    blocked = 0

    class InterruptedScript(CaseScript):
        async def complete(self, messages, schema):
            nonlocal blocked
            if self.requests:
                blocked += 1
                if blocked == 2:
                    started.set()
                await asyncio.Event().wait()
            response = await super().complete(messages, schema)
            return replace(response, usage=ModelUsage(100, 10, 0.001))

    args = Namespace(
        faults="case-02",
        set="primary",
        system="both",
        graph="staged",
        models="scripted",
        model="scripted",
        repeats=1,
        concurrency=2,
        budget_usd="0",
        out=tmp_path / "batch",
        k=1,
    )
    task = asyncio.create_task(
        run_suite(args, model_factory=lambda _: InterruptedScript("case-02"))
    )
    await asyncio.wait_for(started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    board = json.loads((args.out / "leaderboard.json").read_text())
    assert len(board["runs"]) == 2
    assert all(g["process"]["execution_status"] == "interrupted" for g in board["runs"])
    for result_path in args.out.glob("*/result.json"):
        result = json.loads(result_path.read_text())
        assert result["completeness"] == "partial"
        assert result["stop_reason"] == "interrupted"
        assert (result_path.parent / "report.md").exists()
        assert (result_path.parent / "grade.json").exists()
        # Previously committed paid work survives; unreceived calls stay unknown.
        assert sum(u["known_cost"] for u in result["usage"].values()) >= 0.001
        assert any(u["cost"] == "unknown" for u in result["usage"].values())


@pytest.mark.parametrize("operation", ["finish", "release"])
@pytest.mark.parametrize("lock_scope", ["parent", "local"])
async def test_cancelled_budget_settlement_is_retryable_and_idempotent(operation, lock_scope):
    """Review regression: popping a parent identity before awaiting leaked reserves."""
    import asyncio
    from decimal import Decimal

    from incident_investigator.grading.budget import RunSpendGuard, SpendGuard

    parent = SpendGuard(0, ".10")
    run = RunSpendGuard(parent, ".05")
    ident = await run.reserve(Decimal(".02"))
    lock = parent.lock if lock_scope == "parent" else run.lock
    await lock.acquire()
    coroutine = (
        run.finish(ident, ".001", source="generation")
        if operation == "finish"
        else run.release(ident)
    )
    settlement = asyncio.create_task(coroutine)
    await asyncio.sleep(0)
    assert not settlement.done()
    settlement.cancel()
    with pytest.raises(asyncio.CancelledError):
        await settlement
    lock.release()
    # BoundedModel's unknown-cost cleanup must replay the original operation.
    await run.finish(ident, None)
    await run.finish(ident, None)
    await run.release(ident)
    assert not parent.active and not run.active and not run.parent_ids and not run.pending
    assert len(parent.records) == len(run.records) == 1
    expected_cost = Decimal(".001") if operation == "finish" else Decimal(0)
    assert parent.returned == run.returned == expected_cost
    assert parent.unknown == run.unknown == 0
    assert parent.records == run.records
    if operation == "finish":
        assert run.records[0]["source"] == "generation"
    # A retained unknown bound would wrongly refuse this otherwise affordable call.
    successor = await run.reserve(Decimal(".049"))
    await run.release(successor)
    assert not parent.active and not run.active and not run.parent_ids and not run.pending


async def test_cancelled_unbilled_release_preserves_cancellation_and_zero_charge():
    import asyncio

    from incident_investigator.grading.budget import BoundedModel, RunSpendGuard, SpendGuard
    from incident_investigator.models import ModelCallError, OpenRouterModel

    parent = SpendGuard(0, ".10")
    run = RunSpendGuard(parent, ".05")
    entered = asyncio.Event()

    class Adapter:
        build_request = staticmethod(OpenRouterModel.build_request)

        def model_for_role(self, role):
            return "test/model"

        async def complete(self, messages, schema):
            await run.lock.acquire()
            entered.set()
            raise ModelCallError("unbilled refusal", retryable=False, before_send=True)

    bounded = BoundedModel(
        Adapter(),
        {
            "test/model": {
                "context_length": 100000,
                "pricing": {"prompt": ".000001", "completion": ".000001"},
            }
        },
        run,
    )
    task = asyncio.create_task(bounded.complete([], {}))
    await entered.wait()
    task.cancel()
    run.lock.release()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not run.pending and not run.parent_ids and not run.active and not parent.active
    assert run.charged() == parent.charged() == 0
    assert run.records == parent.records
    assert run.records[0]["cost"] == "unbilled_failure"


async def test_suite_timeout_propagates_to_runtime_adapter_and_manifest(tmp_path, monkeypatch):
    from dataclasses import replace

    import httpx

    from incident_investigator.grading import suite
    from incident_investigator.models import OpenRouterModel
    from incident_investigator.models.adapter import ModelUsage

    model_id = "deepseek/deepseek-v3.2"
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")

    async def usage(client):
        return 0

    async def catalogue(client, url):
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "data": [
                    {
                        "id": model_id,
                        "context_length": 180000,
                        "pricing": {"prompt": "0", "completion": "0"},
                    }
                ]
            },
        )

    async def complete(self, messages, schema):
        assert self.timeout_seconds == 180
        if not hasattr(self, "_test_script"):
            self._test_script = CaseScript("case-02")
        result = await self._test_script.complete(messages, schema)
        return replace(result, usage=ModelUsage(1, 1, 0), model=model_id)

    monkeypatch.setattr(suite, "key_usage", usage)
    monkeypatch.setattr(httpx.AsyncClient, "get", catalogue)
    monkeypatch.setattr(OpenRouterModel, "complete", complete)
    args = Namespace(
        faults="case-02",
        set="primary",
        system="both",
        graph="staged",
        models=model_id,
        model="openrouter",
        repeats=1,
        concurrency=2,
        budget_usd="0",
        out=tmp_path / "batch",
        max_input_tokens=180000,
        call_seconds=180,
        wall_seconds=1800,
        k=1,
    )
    board = await run_suite(args)
    assert [g["verdict"] for g in board["runs"]] == ["correct", "correct"]
    batch = json.loads((args.out / "batch.json").read_text())
    assert batch["limits"]["call_seconds"] == 180 and batch["limits"]["wall_seconds"] == 1800
    assert batch["max_input_tokens"] == 180000
    for path in args.out.glob("*/result.json"):
        assert json.loads(path.read_text())["state"]["limits"]["call_seconds"] == 180
