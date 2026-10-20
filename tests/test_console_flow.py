"""Regression: a zero-cap model job appeared to succeed with no visible result."""

import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from incident_investigator import cli
from incident_investigator.console import create_console
from incident_investigator.console_jobs import Jobs, RunRequest
from incident_investigator.grading.budget import estimated_input_tokens, price_bound
from incident_investigator.models import ModelCallError, OpenRouterModel, ScriptedModel
from incident_investigator.runtime import investigate
from incident_investigator.storage import repositories


@pytest.fixture(params=["filesystem", "memory"])
def repo(request, tmp_path):
    return repositories(root=tmp_path / "home", backend=request.param)


def prices(free=False, structured=True):
    return {
        "context_length": 128000,
        "pricing": {
            "prompt": "0" if free else "0.000001",
            "completion": "0" if free else "0.000002",
        },
        "supported_parameters": ["structured_outputs"] if structured else [],
    }


def headers(client):
    return {
        "X-Console-Token": re.search(
            r'name="console-token" content="([^"]+)"', client.get("/run").text
        )[1]
    }


def test_preflight_blocks_both_caps_before_start_and_allows_free_calls(repo, tmp_path, monkeypatch):
    jobs = Jobs(repo, tmp_path)

    async def catalogue():
        return {"test/paid": prices(), "test/replay:free": prices(free=True)}

    monkeypatch.setattr(jobs.preflight, "catalogue", catalogue)
    launched = []
    monkeypatch.setattr(jobs, "start", lambda request, ident: launched.append(request))
    body = {
        "provider": "openrouter",
        "model_id": "test/paid",
        "faults": ["case-02"],
        "system": "both",
        "budget_usd": "0",
        "confirm_paid": True,
    }
    with TestClient(create_console(repo, jobs=jobs)) as client:
        token = headers(client)
        preview = client.post("/api/preview", json=body, headers=token)
        assert preview.status_code == 200
        result = preview.json()
        assert not result["launch_allowed"] and float(result["estimated_call_reservation"]) > 0
        assert "Suite cap USD" in result["message"]
        assert result["blocked_fields"] == ["budget_usd"]
        assert float(result["minimum_cap"]) >= float(result["estimated_call_reservation"])
        assert result["max_model_calls"] == 40
        assert float(result["rough_run_reservation"]) >= 40 * float(
            result["estimated_call_reservation"]
        )
        assert (
            "minimum cap" in result["reservation_guide"]
            and "up to 40 model calls" in result["reservation_guide"]
        )
        assert client.post("/api/jobs", json=body, headers=token).status_code == 422
        body.update(budget_usd="1", run_budget_usd="0")
        result = client.post("/api/preview", json=body, headers=token).json()
        assert not result["launch_allowed"] and "Per-run cap USD" in result["message"]
        assert result["blocked_fields"] == ["run_budget_usd"]
        assert client.post("/api/jobs", json=body, headers=token).status_code == 422
        assert not launched
        body.update(model_id="test/replay:free", budget_usd="0")
        result = client.post("/api/preview", json=body, headers=token).json()
        assert result["launch_allowed"] and float(result["estimated_call_reservation"]) == 0
        assert client.post("/api/jobs", json=body, headers=token).status_code == 200
        assert len(launched) == 1
        page = client.get("/run").text
        assert "How it works" in page and "Watch this job" in page
        assert "canned pipeline replay" in page and "inconclusive is expected" in page
        assert 'id="budget_usd"' in page and 'id="run_budget_usd"' in page
        assert 'id="run-error-fields"' in page
        assert "Without an installed replay file" not in page
        assert "Multi: six collaborating roles" in page and "Single: one investigator" in page
        assert "same snapshots and limits" in page
        assert 'data-call-seconds="90.0"' in page and 'data-wall-seconds="1200.0"' in page


@pytest.mark.parametrize("system", ["multi", "single"])
@pytest.mark.parametrize("structured", [True, False])
async def test_initial_reservation_matches_the_runtime_request(
    tmp_path, monkeypatch, system, structured
):
    jobs = Jobs(repositories(root=tmp_path), tmp_path)
    info = prices(structured=structured)

    async def catalogue():
        return {"test/paid": info}

    monkeypatch.setattr(jobs.preflight, "catalogue", catalogue)
    request = RunRequest(
        provider="openrouter",
        model_id="test/paid",
        faults=["case-02"],
        system=system,
        budget_usd="1",
    )
    preflight = await jobs.preflight.check(request, jobs.repo)

    class Recorder(ScriptedModel):
        async def complete(self, messages, schema):
            self.requests.append((messages, schema))
            raise ModelCallError(
                "local double stops before sending", before_send=True, retryable=False
            )

    recorder = Recorder()
    frozen = jobs.preflight._snapshots["case-02"]
    await investigate(frozen, recorder, tmp_path / "runs", system=system)
    messages, schema = recorder.requests[0]
    rendered = OpenRouterModel.build_request(messages, schema, "test/paid", structured=structured)
    expected = price_bound(
        info,
        max_input_tokens=estimated_input_tokens(rendered),
        output_tokens=rendered["max_tokens"],
    )
    assert preflight["launch_allowed"] and preflight["estimated_call_reservation"] == str(expected)


async def test_unavailable_prices_block_launch_without_a_model_call(tmp_path, monkeypatch):
    jobs = Jobs(repositories(root=tmp_path), tmp_path)

    async def unavailable():
        raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(jobs.preflight, "catalogue", unavailable)
    result = await jobs.preflight.check(
        RunRequest(provider="openrouter", model_id="test/paid"), jobs.repo
    )
    assert not result["launch_allowed"] and "public prices" in result["message"]


@pytest.mark.parametrize("reconciled", [True, False])
def test_all_skipped_job_explains_the_stop_and_links_to_comparison(repo, tmp_path, reconciled):
    store = repo.artifacts
    skip = {
        "fault": "case-02",
        "system": "multi",
        "reason": "suite budget reservation cannot fit remaining budget",
    }
    store.save_json(
        "jobs/blocked/job.json", {"id": "blocked", "status": "completed", "exit_code": 0}
    )
    store.save_json("suites/blocked/batch.json", {"launch_id": "blocked", "skipped": [skip]})
    store.save_json(
        "suites/blocked/leaderboard.json", {"models": [], "runs": [], "skipped": [skip]}
    )
    store.save_json("suites/blocked/budget-stop.json", skip)
    store.save_json("suites/blocked/budget.json", {"calls": [], "state": {"charged": "0"}})
    if reconciled:
        store.save_json("suites/blocked/spend-after.json", {"usage_delta": "0"})
    jobs = Jobs(repo, tmp_path)
    result = jobs.get("blocked")
    assert (
        result["status"] == "stopped"
        and "refused all 1 attempts before sending" in result["outcome"]
    )
    assert ("USD 0 spent" in result["outcome"]) == reconciled
    assert "Raise Suite cap USD" in result["outcome"]
    assert skip["reason"] in result["output"]
    with TestClient(create_console(repo, jobs=jobs)) as client:
        page = client.get("/jobs/blocked").text
        assert 'id="job-outcome"' in page and "Open comparison" in page
        assert f"/suites/{result['suites'][0]['id']}" in page


async def test_suite_prints_skips_and_retains_the_empty_result(tmp_path, monkeypatch, capsys):
    from incident_investigator.grading import suite

    async def usage(client):
        return 0

    async def get(client, url, **kwargs):
        assert url.endswith("/models")
        return httpx.Response(
            200, json={"data": [{"id": "test/paid", **prices()}]}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(suite, "key_usage", usage)
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    args = cli.build_parser().parse_args(
        [
            "eval-suite",
            "--model",
            "openrouter",
            "--models",
            "test/paid",
            "--faults",
            "case-02",
            "--budget-usd",
            "0",
            "--out",
            str(tmp_path / "batch"),
        ]
    )
    await suite.execute(args)
    output = capsys.readouterr().out
    assert "Skipped case-02 | system multi" in output
    assert "Stopped: all 1 attempts skipped before sending" in output
    assert json.loads((args.out / "budget.json").read_text())["calls"] == []
    assert json.loads((args.out / "leaderboard.json").read_text())["runs"] == []


def test_live_report_explains_pending_output_and_links_back_to_job(repo):
    repo.artifacts.save_json(
        "runs/live/run.json",
        {
            "run_id": "live",
            "launch_id": "live-job",
            "case_id": "case-04",
            "system": "multi",
            "graph": "staged",
        },
    )
    repo.runs.refresh()
    run = repo.runs.runs()[0]
    with TestClient(create_console(repo)) as client:
        page = client.get(f"/runs/{run['id']}?tab=report").text
        assert "This investigation is still running" in page and "report is being prepared" in page
        assert 'href="/jobs/live-job"' in page and "Watch job" in page


def test_mixed_job_warns_about_run_failure_and_explains_truncation(repo, tmp_path):
    store = repo.artifacts
    store.save_json("jobs/mixed/job.json", {"id": "mixed", "status": "completed", "exit_code": 0})
    store.save_json(
        "suites/mixed/batch.json", {"launch_id": "mixed", "limits_profile": "demo-live"}
    )
    for system in ("multi", "single"):
        store.save_json(
            f"suites/mixed/{system}/result.json",
            {
                "run_id": system,
                "launch_id": "mixed",
                "system": system,
                "case_id": "lab-demo",
                "status": "failed" if system == "single" else "completed",
                "stop_reason": "output_truncated" if system == "single" else None,
            },
        )
    jobs = Jobs(repo, tmp_path)
    job = jobs.get("mixed")
    assert (
        job["exit_code"] == 0 and "1 of 2 runs failed: single: output truncated" in job["outcome"]
    )
    assert "output-token limit before finishing the report" in job["outcome"]
    assert job["outcome_tone"] == "warning"
    with TestClient(create_console(repo, jobs=jobs)) as client:
        page = client.get("/jobs/mixed").text
        assert 'class="callout warning"' in page and "1 of 2 runs failed" in page
        failed = next(r for r in job["runs"] if r["system"] == "single")
        report = client.get(f"/runs/{failed['id']}").text
        assert "Try another model or a larger output limit" in report
        assert "Profile demo-live" in report and "· lab-demo" not in report


def test_grade_reasons_explain_saved_failures_without_modifying_the_grade(repo):
    from incident_investigator.console_views import check_reasons

    r = {
        "run_id": "uncertain",
        "status": "completed",
        "diagnosis": "inconclusive",
        "hypotheses": [{"statement": "A tentative service hypothesis", "confidence": "high"}],
    }
    checks = {
        name: False
        for name in (
            "culprit_service",
            "mechanism",
            "key_evidence_cited",
            "required_evidence_groups",
            "support_references_valid",
            "justified_uncertainty",
        )
    }
    g = {
        "run_id": "uncertain",
        "verdict": "inconclusive",
        "family": "bad_configuration",
        "diagnosis_checks": checks,
    }
    store = repo.artifacts
    store.save_json("runs/uncertain/result.json", r)
    store.save_json("runs/uncertain/grade.json", g)
    before = store.read("runs/uncertain/grade.json")
    reasons = check_reasons(r, g)
    assert set(reasons) == set(checks) and all(reasons.values())
    assert "inconclusive" in reasons["culprit_service"]
    repo.runs.refresh()
    run = repo.runs.runs()[0]
    with TestClient(create_console(repo)) as client:
        page = client.get(f"/runs/{run['id']}?tab=grade").text
        assert "no final cause was committed" in page and "does not override" in page
        for name in set(checks) - {"justified_uncertainty"}:
            assert reasons[name] in page
        assert "The report did not meet all requirements for justified uncertainty" not in page
    assert store.read("runs/uncertain/grade.json") == before


def test_job_profile_fallback_and_effective_limit_preview(repo, tmp_path):
    store = repo.artifacts
    store.save_json("jobs/one/job.json", {"limits_profile": "demo-live"})
    store.save_json(
        "runs/one/result.json", {"run_id": "one", "launch_id": "one", "case_id": "lab-demo"}
    )
    repo.runs.refresh()
    run = repo.runs.runs()[0]
    with TestClient(create_console(repo, jobs=Jobs(repo, tmp_path))) as client:
        page = client.get(f"/runs/{run['id']}").text
        assert "Profile demo-live" in page
        token = headers(client)
        preview = client.post(
            "/api/preview", json={"limits_profile": "demo-live"}, headers=token
        ).json()
        assert preview["plan"]["call_seconds"] == 90 and preview["plan"]["wall_seconds"] == 1200
        preview = client.post(
            "/api/preview",
            json={"limits_profile": "demo-live", "call_seconds": 180, "wall_seconds": 1800},
            headers=token,
        ).json()
        assert preview["plan"]["call_seconds"] == 180 and preview["plan"]["wall_seconds"] == 1800
