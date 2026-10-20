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
@pytest.mark.parametrize("system", ["staged", "single"])
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
        graph=system,
        limits=Limits(call_seconds=0.3, wall_seconds=20),
    )
    result = json.loads((directory / "result.json").read_text())
    assert result["status"] == "completed", result["stop_reason"]
    g = grade(result, expectation(case))
    assert g["verdict"] == "correct", g["diagnosis_checks"]
    assert g["citations"]["unknown"] == []
    if system == "single":
        assert {t["role"] for t in result["state"]["tasks"].values()} == {"single"}
        assert set(result["usage"]) == {"single"}
        assert result["report"]["revision_id"] == result["state"]["hypothesis_revision"]
        context = json.loads(model.requests[0][0][1]["content"])
        assert "changes" in context["tools"] and "logs" in context["tools"]
        assert "NO native or callable tools" in model.requests[0][0][0]["content"]
    if case == "reliability-01":
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
        graph="single",
        limits=Limits(task_model_calls=1, task_tool_calls=1),
    )
    result = json.loads((directory / "result.json").read_text())
    assert result["status"] == "completed"
    assert result["usage"]["single"]["model_calls"] == 3
    assert result["usage"]["single"]["tool_calls"] == 8
