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
    assert result["heading"] == "stopped by budget cap"
    assert skip["reason"] in result["output"]
    with TestClient(create_console(repo, jobs=jobs)) as client:
        page = client.get("/jobs/blocked").text
        assert 'id="job-outcome"' in page and "Open results" in page
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
        assert "Profile demo-live" in report and "Case lab-demo" in report


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


@pytest.mark.parametrize("failed_role", ["orchestrator", "reporting"])
async def test_provider_error_fallback_is_explained_without_reclassifying_results(
    tmp_path, failed_role
):
    from test_m3_agents import Scenario

    from incident_investigator.console_views import present_run
    from incident_investigator.grading.grader import expectation, grade_run
    from incident_investigator.sources.loader import load_snapshot

    class ProviderError(Scenario):
        async def complete(self, messages, schema):
            if json.loads(messages[1]["content"])["role"] == failed_role:
                raise ModelCallError("OpenRouter HTTP 502", retryable=False)
            return await super().complete(messages, schema)

    root = tmp_path / "home"
    directory = await investigate(
        load_snapshot("dev-00"), ProviderError(followup=False), root / "runs"
    )
    repo = repositories(root=root)
    result_before = (directory / "result.json").read_bytes()
    g = grade_run(directory, expectation("bad_configuration"))
    repo.runs.refresh()
    row = present_run(repo.runs.runs()[0], repo.artifacts)
    if failed_role == "orchestrator":
        assert row["status"] == "completed" and "partial report completed" in row["outcome"]
        assert "earlier role failed" in row["failure_explanation"]
        assert g["verdict"] == "inconclusive"
    else:
        assert row["status"] == "failed" and "stopped during reporting" in row["outcome"]
        assert g["verdict"] == "no_report"
    assert "provider HTTP 502" in row["outcome"]
    assert (directory / "result.json").read_bytes() == result_before
    transitions = [
        json.loads(line)
        for line in (directory / "trace.jsonl").read_text().splitlines()
        if json.loads(line)["event"] == "state_transition"
    ]
    assert transitions[-1]["model_calls"] == row["model_calls"]
    assert transitions[-1]["tool_calls"] == row["tool_calls"]


def test_paired_suite_explains_progress_timeout_cost_and_grade_conflicts(repo, tmp_path):
    store = repo.artifacts
    store.save_json("jobs/pair/job.json", {"id": "pair", "status": "completed", "exit_code": 0})
    store.save_json(
        "suites/pair/batch.json",
        {"launch_id": "pair", "faults": ["case-02"], "systems": ["multi", "single"]},
    )
    store.save_json("suites/pair/leaderboard.json", {"runs": []})
    store.save_json("suites/pair/prices.json", {"test/free": prices(free=True)})
    for system in ("multi", "single"):
        reason = "reporting: OpenRouter HTTP 502" if system == "multi" else "single: TimeoutError: "
        store.save_json(
            f"suites/pair/{system}/result.json",
            {
                "run_id": system,
                "launch_id": "pair",
                "case_id": "lab-demo",
                "system": system,
                "models": {system: "test/free"},
                "status": "failed",
                "stop_reason": reason,
                "diagnosis": "inconclusive",
                "hypotheses": [{"statement": "tentative", "confidence": "high"}]
                if system == "multi"
                else [],
                "state": {
                    "stage": "reporting" if system == "multi" else "single",
                    "limits": {"call_seconds": 180},
                },
                "usage": {system: {"cost": "unknown", "known_cost": 0}},
            },
        )
        store.save_json(
            f"suites/pair/{system}/grade.json",
            {
                "run_id": system,
                "family": "case-02",
                "verdict": "no_report",
                "diagnosis_checks": {
                    "culprit_service": system == "multi",
                    "mechanism": system == "multi",
                },
                "resources": {"wall_seconds": 0.2769952079979703},
            },
        )
        store.save_json(
            f"suites/pair/{system}/budget.json",
            {
                "state": {"charged": "0", "active": "0", "unknown_reserve": "0"},
                "calls": [{"reservation": "0", "cost": None}],
            },
        )
        if system == "single":
            store.write(
                f"suites/pair/{system}/trace.jsonl",
                (
                    "\n".join(
                        json.dumps({"event": "model_call", "outcome": "TimeoutError:", "at": n + 1})
                        for n in range(2)
                    )
                    + "\n"
                ).encode(),
            )
    repo.runs.refresh()
    jobs = Jobs(repo, tmp_path)
    before = store.read("suites/pair/multi/grade.json")
    with TestClient(create_console(repo, jobs=jobs)) as client:
        suite = repo.runs.suites()[0]
        page = client.get(f"/suites/{suite['id']}").text
        assert "did not finish" in page
        assert "not a like-for-like comparison of completed attempts" in page
        assert (
            "stopped during reporting; provider HTTP 502; retained a high-confidence hypothesis"
            in page
        )
        assert "2 timed-out model calls at 180 s per call" in page
        assert "free model, no charge" in page and "test/free" in page
        assert "time unavailable" not in page
        assert ">.</p>" not in page
        multi = next(r for r in repo.runs.runs() if r["system"] == "multi")
        report = client.get(f"/runs/{multi['id']}?tab=grade").text
        assert "independently of final report delivery" in report
        assert "can pass on a tentative hypothesis" in report
        assert "Case lab-demo" in report
        assert "Capture in local Phoenix" in report
        raw = client.get(f"/runs/{multi['id']}/result.json")
        assert raw.status_code == 200 and raw.json()["run_id"] == "multi"
        assert (
            'href="/runs/' + multi["id"] + '/result.json"'
            in client.get(f"/runs/{multi['id']}").text
        )
        assert "wall 0.3s" in report
        single = next(r for r in repo.runs.runs() if r["system"] == "single")
        page = client.get(f"/runs/{single['id']}?tab=grade").text
        assert "180 s per call" in page and "raise Per-call timeout" in page
        assert "No ranked hypothesis was available for the mechanism check" in page
        job = jobs.get("pair")
        assert job["heading"] == "finished, 2 of 2 runs failed"
        assert "finished, 2 of 2 runs failed" in client.get("/jobs/pair").text
    assert store.read("suites/pair/multi/grade.json") == before


@pytest.mark.parametrize(
    "missing", ["prices", "ledger", "charged", "active", "unknown_reserve", "reservation", "cost"]
)
def test_free_cost_requires_zero_prices_and_a_consistent_ledger(repo, missing):
    from incident_investigator.console_views import present_run

    row = {
        "prefix": "suites/s/r",
        "status": "failed",
        "models": {"single": "test/free"},
        "cost": None,
    }
    info = prices(free=True)
    ledger = {
        "state": {"charged": "0", "active": "0", "unknown_reserve": "0"},
        "calls": [{"reservation": "0", "cost": None}],
    }
    if missing in ledger["state"]:
        ledger["state"][missing] = "1"
    elif missing in {"reservation", "cost"}:
        ledger["calls"][0][missing] = "1"
    if missing != "prices":
        repo.artifacts.save_json("suites/s/prices.json", {"test/free": info})
    if missing != "ledger":
        repo.artifacts.save_json("suites/s/r/budget.json", ledger)
    assert not present_run(row, repo.artifacts, "suites/s")["free_no_charge"]


def test_live_job_refreshes_stage_and_admitted_call_counts(repo, tmp_path):
    store = repo.artifacts
    store.save_json("jobs/live/job.json", {"id": "live", "status": "running"})
    store.save_json(
        "runs/live/run.json", {"run_id": "live", "launch_id": "live", "system": "multi"}
    )
    store.save_json("runs/live/phoenix.json", {"url": "http://127.0.0.1:6006/projects/p/traces/t"})
    jobs = Jobs(repo, tmp_path)
    jobs.tasks["live"] = type(
        "Active", (), {"done": lambda self: False}
    )()  # get() only checks membership, no child process is launched.
    store.write(
        "runs/live/trace.jsonl",
        b'{"event":"state_transition","stage":"gathering","model_calls":3,"tool_calls":2}\n',
    )
    assert "gathering; 3 model / 2 tool calls admitted" in jobs.get("live")["runs"][0]["outcome"]
    store.write(
        "runs/live/trace.jsonl",
        b'{"event":"state_transition","stage":"reporting","model_calls":14,"tool_calls":8}\n',
    )
    assert "reporting; 14 model / 8 tool calls admitted" in jobs.get("live")["runs"][0]["outcome"]
    # Remove synthetic task before the app lifespan closes.
    jobs.tasks.clear()
    with TestClient(create_console(repo, jobs=jobs)) as client:
        assert "Phoenix · live detailed view" in client.get("/jobs/live").text


@pytest.mark.parametrize("available", [True, False])
def test_phoenix_default_and_authorization_guidance(repo, tmp_path, monkeypatch, available):
    import incident_investigator.console as console

    async def reachable():
        return available

    async def catalogue():
        return {"test/free": prices(free=True)}

    monkeypatch.setattr(console, "phoenix_available", reachable)
    jobs = Jobs(repo, tmp_path)
    monkeypatch.setattr(jobs.preflight, "catalogue", catalogue)
    with TestClient(create_console(repo, jobs=jobs)) as client:
        page = client.get("/run").text
        assert bool(re.search(r'name="trace" type="checkbox"\s+checked', page)) == available
        assert "demo-live gives live providers more time" in page
        assert "0 means no paid calls allowed" in page
        assert "Blank inherits the suite cap" in page
        token = headers(client)
        body = {"provider": "openrouter", "model_id": "test/free", "faults": ["case-02"]}
        preview = client.post("/api/preview", json=body, headers=token).json()
        assert preview["authorization_required"] and preview["free_model"]
        assert "Free model" in preview["reservation_guide"]
        response = client.post("/api/jobs", json=body, headers=token)
        assert response.status_code == 422
        assert "Tick 'I explicitly authorize OpenRouter calls'" in response.json()["detail"]
        assert "paid-run confirmation" not in response.text
        assert not repo.artifacts.list_keys("jobs/")


@pytest.mark.parametrize("response", [200, 503, 302, "unreachable", "external"])
async def test_phoenix_probe_is_bounded_and_loopback_only(monkeypatch, response):
    from incident_investigator.console import phoenix_available

    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    monkeypatch.setenv(
        "II_PHOENIX_URL",
        "https://external.invalid" if response == "external" else "http://127.0.0.1:6123",
    )
    calls = []

    async def get(client, url):
        calls.append(url)
        assert client.timeout.connect == 0.5 and not client.follow_redirects
        if response == "unreachable":
            raise httpx.ConnectError("local collector unavailable")
        return httpx.Response(response, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    assert await phoenix_available() == (response == 200)
    assert len(calls) == (0 if response == "external" else 1)


def test_partial_review_repair_is_not_presented_as_a_role_failure(repo):
    from incident_investigator.console_views import present_run

    repo.artifacts.save_json(
        "runs/repair/result.json",
        {
            "status": "completed",
            "completeness": "partial",
            "stop_reason": "orchestrator decision repaired; unresolved objections retained",
        },
    )
    row = present_run({"prefix": "runs/repair", "status": "completed"}, repo.artifacts)
    assert "final partial report was delivered" in row["failure_explanation"]
    assert "role failed" not in row["failure_explanation"]


def test_comparison_does_not_invent_a_winner_when_grades_are_pending_or_absent():
    from incident_investigator.console_views import comparison_outcome

    row = {"provisional": True, "pass1": 0, "correct": 0, "runs": []}
    assert "Awaiting" in comparison_outcome({"multi": row})
    assert "provisional" in comparison_outcome({"multi": row, "single": row})
    row = {**row, "provisional": False}
    assert "no diagnostic winner" in comparison_outcome({"multi": row, "single": row})
    assert "Single achieved a higher correct-verdict rate" in comparison_outcome(
        {"multi": row, "single": {**row, "pass1": 1, "correct": 1}}
    )
    assert "same correct-verdict rate" in comparison_outcome(
        {"multi": {**row, "pass1": 1, "correct": 1}, "single": {**row, "pass1": 1, "correct": 1}}
    )


@pytest.mark.parametrize(
    "graph,system", [("staged", "multi"), ("single", "single"), (None, "multi")]
)
def test_legacy_artifacts_render_without_new_metadata(repo, graph, system):
    """Regression: historical leaderboard rows lack system and suite_kind."""
    from incident_investigator.console_views import system_metrics

    store = repo.artifacts
    provenance = {"graph": graph} if graph is not None else {}
    models = {"requested": "test/legacy"}
    result = {
        "run_id": "legacy",
        "case_id": "legacy-case",
        "status": "completed",
        "models": models,
        **provenance,
    }
    if graph is None:
        result.pop("status")  # Older incomplete exports can lack final execution metadata.
    grade = {
        "run_id": "legacy",
        "family": "bad_configuration",
        "verdict": "inconclusive",
        "models": models,
        **provenance,
    }
    head = {
        "models": models,
        "runs": 1,
        "verdicts": {"inconclusive": 1},
        "execution_failures": 0,
        "mean_cost": None,
        "mean_grounding": None,
        "faults": {"bad_configuration": {"n": 1, "correct": 0}},
        **provenance,
    }
    store.save_json("suites/legacy/result/result.json", result)
    store.save_json("suites/legacy/result/grade.json", grade)
    store.save_json("suites/legacy/leaderboard.json", {"models": [head]})
    before = {
        name: store.read("suites/legacy/" + name)
        for name in ("result/result.json", "result/grade.json", "leaderboard.json")
    }
    repo.runs.refresh()
    suite = repo.runs.suites()[0]
    rows = repo.runs.runs({"suite": suite["id"]})
    heads = system_metrics({"models": [head]}, rows, 4)
    assert heads[0]["system"] == system and heads[0]["suite_kind"] == "primary"
    assert len(heads[0]["outcomes"]) == 1
    with TestClient(create_console(repo)) as client:
        page = client.get(f"/suites/{suite['id']}")
        assert page.status_code == 200
        assert "System summary" in page.text and "test/legacy" in page.text
        assert "free model, no charge" not in page.text
        for tab in ("report", "grade"):
            response = client.get(f"/runs/{rows[0]['id']}?tab={tab}")
            assert response.status_code == 200
            assert "Case legacy-case" in response.text and "Profile unavailable" in response.text
        assert client.get("/").status_code == 200
    for name, data in before.items():
        assert store.read("suites/legacy/" + name) == data
    # Missing optional aggregates stay unknown, including a fault with no counts.
    minimal = system_metrics({"models": [{"faults": {"old": {}}}]}, [], 4)[0]
    assert minimal["system"] == "multi" and minimal["suite_kind"] == "primary"
    assert minimal["passk"] is None


@pytest.mark.parametrize("status", ["completed", "stopped", "running"])
@pytest.mark.parametrize("observed", [True, False])
def test_missing_attempt_has_final_skip_reason_and_identifiable_callout(repo, status, observed):
    store = repo.artifacts
    skips = [
        {
            "fault": "case-02",
            "model": "test/other",
            "system": "single",
            "reason": "other model skip",
        },
        {
            "fault": "case-02",
            "model": "test/paired",
            "system": "single",
            "reason": "suite cancelled before admission",
        },
        {
            "fault": "reliability-01",
            "model": "test/paired",
            "system": "single",
            "reason": "variant cap refused",
        },
    ]
    store.save_json("jobs/missing/job.json", {"id": "missing", "status": status})
    store.save_json(
        "suites/missing/batch.json",
        {
            "launch_id": "missing",
            "systems": ["multi", "single"],
            "suite_set": "all",
            "skipped": skips,
        },
    )
    store.save_json("suites/missing/leaderboard.json", {"models": [], "skipped": skips})
    if observed:
        store.save_json(
            "suites/missing/r/result.json",
            {
                "run_id": "r",
                "status": "completed",
                "system": "multi",
                "models": {"requested": "test/paired"},
            },
        )
        store.save_json(
            "suites/missing/r/grade.json",
            {"run_id": "r", "family": "case-02", "verdict": "inconclusive"},
        )
    repo.runs.refresh()
    suite = repo.runs.suites()[0]
    with TestClient(create_console(repo)) as client:
        response = client.get(f"/suites/{suite['id']}")
        assert response.status_code == 200
        page = response.text
        assert "case-02 · test/paired · primary" in page
        assert "case-02 · test/other · primary" in page
        assert "reliability-01 · test/paired · variants" in page
        if status == "running":
            assert "Awaiting attempts from both systems" in page
        else:
            assert "Awaiting attempts" not in page
            assert (
                "Single has no retained attempt; skipped: suite cancelled before admission." in page
            )
            assert "Single has no retained attempt; skipped: other model skip." in page
            assert "Single has no retained attempt; skipped: variant cap refused." in page
        assert "Phoenix · live detailed view for agent traces" in client.get("/run").text


def test_provider_limited_comparison_counts_request_errors_separately(repo):
    store = repo.artifacts
    store.save_json(
        "suites/provider/batch.json", {"systems": ["multi", "single"], "faults": ["case-02"]}
    )
    store.save_json("suites/provider/leaderboard.json", {"models": []})
    for system in ("multi", "single"):
        store.save_json(
            f"suites/provider/{system}/result.json",
            {
                "run_id": system,
                "system": system,
                "status": "completed",
                "models": {system: "test/model"},
                "completeness": "partial" if system == "multi" else "complete",
                "state": {"calls": {}},
            },
        )
        store.save_json(
            f"suites/provider/{system}/grade.json",
            {
                "run_id": system,
                "family": "case-02",
                "verdict": "inconclusive" if system == "multi" else "correct",
            },
        )
        if system == "multi":
            store.write(
                f"suites/provider/{system}/trace.jsonl",
                (
                    "\n".join(
                        json.dumps(
                            {
                                "event": "model_call",
                                "call_id": role,
                                "role": role,
                                "outcome": "ModelCallError: OpenRouter HTTP 502",
                            }
                        )
                        for role in ("observability", "dependency", "reasoning")
                    )
                    + "\n"
                ).encode(),
            )
    repo.runs.refresh()
    suite = repo.runs.suites()[0]
    with TestClient(create_console(repo)) as client:
        page = client.get(f"/suites/{suite['id']}").text
        assert "multi: provider HTTP 502 on 3 roles" in page
        assert "not a fair comparison of diagnosis quality" in page
        assert "Single achieved a higher" not in page
        assert "Execution failures count runs that stopped without completing reporting" in page
        assert "Model-call errors" in page and "<span>3</span>" in page
        assert "<span>Execution failures</span><span>0</span>" in page
        from incident_investigator.console_views import present_run, suite_metrics, system_metrics

        rows = [present_run(r, store) for r in repo.runs.runs()]
        heads = system_metrics({}, rows, 1)
        assert next(h for h in heads if h["system"] == "multi")["model_call_errors"] == 3
        assert next(h for h in heads if h["system"] == "single")["model_call_errors"] == 0
        incident = next(h for h in suite_metrics(rows, 1) if h["system"] == "multi")
        assert incident["model_call_errors"] == 3 and incident["execution_failures"] == 0


@pytest.mark.parametrize("verdict", ["inconclusive", "no_report", "partial"])
def test_grade_top_line_explains_the_saved_evidence_failure(repo, verdict):
    store = repo.artifacts
    store.save_json(
        "runs/g/result.json",
        {
            "run_id": "g",
            "diagnosis": "inconclusive",
            "hypotheses": [{"statement": "A tentative cause"}],
        },
    )
    grade = {
        "run_id": "g",
        "family": "bad_configuration",
        "verdict": verdict,
        "diagnosis_checks": {
            "culprit_service": True,
            "mechanism": True,
            "key_evidence_cited": False,
        },
    }
    store.save_json("runs/g/grade.json", grade)
    before = store.read("runs/g/grade.json")
    repo.runs.refresh()
    with TestClient(create_console(repo)) as client:
        page = client.get(f"/runs/{repo.runs.runs()[0]['id']}?tab=grade").text
        assert 'id="grade-summary"' in page
        assert "Cause named, but key evidence was not cited" in page
        assert ("No final report was completed." in page) == (verdict == "no_report")
    assert store.read("runs/g/grade.json") == before


def test_single_system_summary_and_compare_selection_are_unambiguous(repo):
    store = repo.artifacts
    store.save_json("suites/one/batch.json", {"systems": ["multi"], "faults": ["case-02"]})
    store.save_json("suites/one/leaderboard.json", {"models": []})
    for ident, model in (("first", "test/one"), ("second", "test/two")):
        store.save_json(
            f"suites/one/{ident}/result.json",
            {
                "run_id": ident,
                "models": {"requested": model},
                "status": "completed",
                "created": 1792514000,
            },
        )
        store.save_json(
            f"suites/one/{ident}/grade.json",
            {"run_id": ident, "family": "case-02", "verdict": "inconclusive"},
        )
    repo.runs.refresh()
    suite = repo.runs.suites()[0]
    rows = repo.runs.runs()
    with TestClient(create_console(repo)) as client:
        assert "Single-system run: no comparison." in client.get(f"/suites/{suite['id']}").text
        page = client.get("/compare").text
        assert 'id="comparison-table"' not in page
        assert "Choose both Left and Right" in page
        labels = re.findall(r'<option value="[^"]+"[^>]*>(.*?)</option>', page, re.S)
        assert any(
            "test/one" in label and "2026-10-20" in label and "case-02" in label for label in labels
        )
        assert (
            'id="comparison-table"'
            not in client.get("/compare", params={"left": rows[0]["id"]}).text
        )
        assert (
            'id="comparison-table"'
            in client.get("/compare", params={"left": rows[0]["id"], "right": rows[1]["id"]}).text
        )
        assert "Start at" in client.get("/").text and "Run evaluation" in client.get("/").text


@pytest.mark.parametrize("unknown", [False, True])
def test_landing_and_resource_cells_distinguish_no_charge_from_unmeasured_usage(repo, unknown):
    store = repo.artifacts
    store.save_json("suites/cost/batch.json", {})
    store.save_json("suites/cost/prices.json", {"test/free": prices(free=True)})
    for ident, model in (("free", "test/free"), ("replay", "scripted")):
        if unknown and ident == "free":
            model = "test/unpriced"
        store.save_json(
            f"suites/cost/{ident}/result.json",
            {
                "run_id": ident,
                "status": "completed",
                "models": {"requested": model},
                "usage": {
                    "role": {
                        "cost": "unknown",
                        "known_cost": 0,
                        "input_tokens": "unknown",
                        "known_input_tokens": 799,
                        "output_tokens": "unknown",
                        "known_output_tokens": 17,
                    }
                },
            },
        )
        store.save_json(
            f"suites/cost/{ident}/budget.json",
            {"state": {"charged": "0", "active": "0", "unknown_reserve": "0"}, "calls": []},
        )
    repo.runs.refresh()
    with TestClient(create_console(repo)) as client:
        page = client.get("/").text
        stat = page.split("Total cost (USD)</label>")[1].split("</strong>")[0]
        assert ("free model, no charge" in stat) == (not unknown)
        assert ("at least USD 0.000000 (some calls unmeasured)" in stat) == unknown
        assert "at least 799" in page and "some calls unmeasured" in page
        assert "known 799" not in page
        suite = repo.runs.suites()[0]
        assert client.get(f"/suites/{suite['id']}").status_code == 200


async def test_model_error_counts_use_receipts_when_older_trace_is_absent(repo):
    from incident_investigator.console_views import present_run

    repo.artifacts.save_json(
        "runs/r/result.json",
        {
            "state": {"calls": {"c": {"kind": "model", "role": "single"}, "t": {"kind": "tool"}}},
            "receipts": {"c": {"error": "TimeoutError:"}, "t": {"error": "source unavailable"}},
        },
    )
    row = present_run({"prefix": "runs/r", "status": "failed"}, repo.artifacts)
    assert row["model_call_errors"] == 1 and row["provider_issue"] == "request timed out"


def test_first_click_launch_waits_for_current_preview_in_shipped_javascript():
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the browser-script interaction regression")
    tests = Path(__file__).parent
    completed = subprocess.run(
        [
            node,
            str(tests / "console_launch_test.cjs"),
            str(tests.parent / "src/incident_investigator/web/console.js"),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert (
        "first-click launch, latest-settings check, authorization and retry passed"
        in completed.stdout
    )


@pytest.mark.parametrize("graph,system", [(None, "multi"), ("single", "single")])
def test_legacy_suite_infers_its_single_system_and_results_link(repo, tmp_path, graph, system):
    store = repo.artifacts
    store.save_json("jobs/old/job.json", {"id": "old", "status": "completed"})
    store.save_json("suites/old/batch.json", {"launch_id": "old"})
    store.save_json("suites/old/leaderboard.json", {"models": []})
    store.save_json(
        "suites/old/r/result.json",
        {"run_id": "r", "launch_id": "old", "graph": graph, "status": "completed"},
    )
    store.save_json(
        "suites/old/r/grade.json", {"run_id": "r", "family": "case-02", "verdict": "inconclusive"}
    )
    repo.runs.refresh()
    suite = repo.runs.suites()[0]
    jobs = Jobs(repo, tmp_path)
    with TestClient(create_console(repo, jobs=jobs)) as client:
        page = client.get(f"/suites/{suite['id']}").text
        assert "Single-system run: no comparison." in page
        assert "no skip reason was recorded" not in page
        assert f"Systems {system}" in page
        assert "Open results" in client.get("/jobs/old").text
    assert jobs.get("old")["suites"][0]["results_label"] == "Open results"


def test_empty_hypothesis_summary_does_not_invent_a_retained_hypothesis():
    from incident_investigator.console_views import grade_summary

    for verdict in ("inconclusive", "no_report"):
        text = grade_summary(
            {"diagnosis": "inconclusive", "hypotheses": []},
            {
                "verdict": verdict,
                "diagnosis_checks": {"culprit_service": False, "mechanism": False},
            },
        )
        assert "No ranked hypothesis was returned" in text
        assert "retained hypothesis" not in text
        assert ("No final report was completed" in text) == (verdict == "no_report")


def test_tied_verdict_summary_compares_measured_per_attempt_resources_only():
    from incident_investigator.console_views import comparison_outcome

    multi = {
        "provisional": False,
        "pass1": 1,
        "correct": 2,
        "n": 2,
        "runs": [],
        "grounding": 0.4,
        "resources": {
            "wall_seconds": 307.04,
            "input_tokens": 295820,
            "model_calls": 22,
            "tool_calls": 20,
        },
    }
    single = {
        "provisional": False,
        "pass1": 1,
        "correct": 1,
        "n": 1,
        "runs": [],
        "grounding": 1,
        "resources": {
            "wall_seconds": 60.41,
            "input_tokens": 60734,
            "model_calls": 5,
            "tool_calls": 4,
        },
    }
    text = comparison_outcome({"multi": multi, "single": single})
    assert "same correct-verdict rate" in text and "Per retained attempt" in text
    assert "Single used 61% less wall time" in text
    assert "Single used 59% fewer input tokens" in text
    assert (
        "Single used 55% fewer model calls" in text and "Single used 60% fewer tool calls" in text
    )
    assert "Single had higher measured grounding (1.00 vs 0.40)" in text
    assert "higher correct-verdict rate" not in text
    single["resources"].update(wall_seconds=None, input_tokens=None, known_input_tokens=60734)
    text = comparison_outcome({"multi": multi, "single": single})
    assert "less wall time" not in text and "fewer input tokens" not in text
    multi["grounding"] = None
    assert "higher measured grounding" not in comparison_outcome({"multi": multi, "single": single})


def test_grounding_hint_and_launch_button_status_are_visible(repo, tmp_path):
    store = repo.artifacts
    store.save_json("suites/hint/batch.json", {"systems": ["multi"]})
    store.save_json("suites/hint/leaderboard.json", {"models": []})
    repo.runs.refresh()
    with TestClient(create_console(repo, jobs=Jobs(repo, tmp_path))) as client:
        page = client.get(f"/suites/{repo.runs.suites()[0]['id']}").text
        assert (
            "fraction of ranked hypotheses" in page and "(measured/total) counts attempts" in page
        )
        assert "not proof of semantic support" in page
        form = client.get("/run").text
        assert 'id="launch-status" role="status"' in form
        assert 'aria-describedby="launch-status"' in form


def test_tied_summary_skips_scripted_replays_and_compares_rounded_grounding():
    from incident_investigator.console_views import comparison_outcome

    def side(wall, grounding, scripted):
        return {
            "provisional": False,
            "pass1": 1,
            "correct": 1,
            "n": 1,
            "runs": [],
            "grounding": grounding,
            "scripted_no_charge": scripted,
            "resources": {"wall_seconds": wall, "model_calls": 2},
        }

    scripted = comparison_outcome({"multi": side(3, 0.5, True), "single": side(1, 0.9, True)})
    assert "Per retained attempt" not in scripted
    live = comparison_outcome({"multi": side(2, 0.501, False), "single": side(1, 0.499, False)})
    assert "Single used 50% less wall time" in live
    assert "grounding" not in live
