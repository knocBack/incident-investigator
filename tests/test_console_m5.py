"""M5 console controls and matched comparisons, through both repository adapters."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from incident_investigator import cli
from incident_investigator.console import create_console
from incident_investigator.console_jobs import Jobs, RunRequest, build_command
from incident_investigator.console_views import suite_metrics
from incident_investigator.grading.suite import execute
from incident_investigator.storage import repositories


@pytest.fixture(params=["filesystem", "memory"])
def repo(request, tmp_path):
    return repositories(root=tmp_path / "workspace", backend=request.param)


def test_console_forwards_set_system_and_all_limits(repo, tmp_path):
    request = RunRequest(
        case_set="all",
        system="both",
        faults=["case-02", "reliability-01"],
        repeats=2,
        concurrency=2,
        call_seconds=180,
        wall_seconds=1800,
        budget_usd="0.25",
        run_budget_usd="0.08",
        max_input_tokens=180000,
    )
    command, _ = build_command(request, repo, "paired", root=tmp_path)
    args = cli.build_parser().parse_args(command[4:])
    assert (args.set, args.system, args.graph) == ("all", "both", "staged")
    assert args.faults == "case-02,reliability-01"
    assert args.repeats == args.concurrency == 2
    assert args.call_seconds == 180 and args.wall_seconds == 1800
    assert args.budget_usd == "0.25" and args.run_budget_usd == "0.08"
    assert args.max_input_tokens == 180000
    assert args.script.name == "scripted-paired.json"
    command, _ = build_command(RunRequest(case_set="variants"), repo, "all", root=tmp_path)
    args = cli.build_parser().parse_args(command[4:])
    assert args.set == "variants" and args.faults == "all"
    with pytest.raises(ValueError, match="within the selected set"):
        build_command(RunRequest(faults=["reliability-01"]), repo, "invalid", root=tmp_path)


@pytest.mark.parametrize(
    "override",
    [
        {"call_seconds": float("nan")},
        {"call_seconds": 601},
        {"wall_seconds": float("inf")},
        {"run_budget_usd": "NaN"},
        {"budget_usd": "-1"},
    ],
)
def test_console_rejects_nonfinite_or_invalid_limits(override):
    with pytest.raises((ValueError, ValidationError)):
        RunRequest(**override)


def populate(repo):
    store = repo.artifacts
    store.save_json(
        "suites/paired/batch.json",
        {
            "suite_set": "all",
            "systems": ["multi", "single"],
            "launch_id": "live",
        },
    )
    for fault in ("case-02", "reliability-01", "reliability-02"):
        for system in ("multi", "single"):
            name = f"{fault}-{system}"
            prefix = f"suites/paired/{name}"
            unknown = system == "single"
            usage = {
                "cost": None if unknown else 0.02,
                "known_cost": 0.01 if unknown else 0.02,
                "input_tokens": None if unknown else 100,
                "known_input_tokens": 100,
                "output_tokens": None if unknown else 10,
                "known_output_tokens": 10,
                "model_calls": 2,
                "tool_calls": 3,
            }
            store.save_json(
                prefix + "/result.json",
                {
                    "run_id": name,
                    "graph": "staged",
                    "system": system,
                    "case_id": fault,
                    "status": "completed",
                    "usage": {system: usage},
                },
            )
            store.save_json(prefix + "/expectation.json", {"family": fault})
            reliability = None
            if fault == "reliability-01":
                reliability = {
                    "kind": "recovery",
                    "fault_exercised": not unknown,
                    "recovered": not unknown,
                    "success": not unknown,
                }
            elif fault == "reliability-02":
                reliability = {"kind": "uncertainty", "justified_uncertainty": unknown}
            store.save_json(
                prefix + "/grade.json",
                {
                    "run_id": name,
                    "family": "bad_configuration" if fault == "case-02" else "resource_exhaustion",
                    "graph": "staged",
                    "system": system,
                    "suite_kind": "primary" if fault == "case-02" else "reliability",
                    "verdict": "wrong" if fault == "case-02" and unknown else "correct",
                    "resources": {"totals": usage, "wall_seconds": 3},
                    "reliability": reliability,
                },
            )
    repo.runs.refresh()


def test_console_separates_primary_reliability_and_unknown_resources(repo):
    populate(repo)
    suite = repo.runs.suites()[0]
    rows = repo.runs.runs({"suite": suite["id"]})
    metrics = suite_metrics(rows, 2)
    assert len(metrics) == 6
    primary = [r for r in metrics if r["case_set"] == "primary"]
    assert len(primary) == 2 and all(r["n"] == 1 for r in primary)
    assert {r["system"]: r["pass1"] for r in primary} == {"multi": 1, "single": 0}
    r1 = next(r for r in metrics if r["system"] == "single" and r["fault"] == "reliability-01")
    assert r1["reliability"]["recovery"]["not_exercised"] == 1
    assert r1["resources"]["cost"] is None and r1["resources"]["known_cost"] == 0.01
    assert len(repo.runs.runs({"system": "single", "case_set": "variants"})) == 2
    with TestClient(create_console(repo)) as client:
        panel = client.get("/run").text
        for field in (
            "case_set",
            "system",
            "run_budget_usd",
            "budget_usd",
            "call_seconds",
            "wall_seconds",
        ):
            assert f'name="{field}"' in panel
        page = client.get(f"/suites/{suite['id']}").text
        for label in (
            "Primary accuracy",
            "Reliability",
            "R1 not exercised",
            "R2 justified",
            "Multi vs single, per incident",
            "known 0.010000",
            "pass@1",
            "Model / tool calls",
        ):
            assert label in page
        assert "single · variants · staged" in client.get("/").text


def test_live_job_discovers_runs_and_trace_links_before_reports(tmp_path, monkeypatch):
    from incident_investigator import console_jobs

    repo = repositories(root=tmp_path)
    repo.artifacts.save_json("jobs/live/job.json", {"id": "live", "status": "running"})
    repo.artifacts.save_json("suites/live/batch.json", {"launch_id": "live", "suite_set": "all"})
    repo.artifacts.save_json(
        "suites/live/attempts/r.json", {"fault": "case-02", "system": "single"}
    )
    repo.artifacts.save_json(
        "suites/live/r/run.json",
        {
            "run_id": "r",
            "launch_id": "live",
            "case_id": "case-02",
            "graph": "staged",
            "system": "single",
        },
    )
    repo.artifacts.save_json(
        "suites/live/r/phoenix.json", {"url": "http://127.0.0.1:6006/projects/p/traces/t"}
    )
    jobs = Jobs(repo, tmp_path)
    clock = [0.0]
    monkeypatch.setattr(console_jobs, "monotonic", lambda: clock[0])
    refreshes = []
    refresh = repo.runs.refresh

    def counted_refresh():
        refreshes.append(clock[0])
        refresh()

    monkeypatch.setattr(repo.runs, "refresh", counted_refresh)
    jobs.tasks["live"] = None  # Presence represents the independently running child.
    record = jobs.get("live")
    assert record["status"] == "running" and len(record["suites"]) == 1
    run = record["runs"][0]
    assert run["system"] == "single" and run["fault"] == "case-02"
    assert run["phoenix_url"].endswith("/traces/t")
    assert repo.artifacts.read(run["prefix"] + "/result.json") is None
    jobs.get("live")
    assert refreshes == [0.0]
    repo.artifacts.save_json(
        "suites/live/r/phoenix.json", {"url": "https://external.invalid/traces/t"}
    )
    clock[0] = 2.0
    assert jobs.get("live")["runs"][0]["phoenix_url"] is None
    assert refreshes == [0.0, 2.0]


def test_interrupted_export_counts_as_execution_failure(repo):
    import re

    repo.artifacts.save_json(
        "runs/interrupted/result.json",
        {
            "run_id": "interrupted",
            "status": "interrupted",
            "graph": "staged",
            "system": "single",
        },
    )
    repo.artifacts.save_json(
        "runs/interrupted/grade.json",
        {
            "run_id": "interrupted",
            "family": "case-02",
            "verdict": "no_report",
            "process": {"execution_status": "interrupted"},
        },
    )
    with TestClient(create_console(repo)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert re.search(r"<label>Execution failures</label>\s*<strong>1</strong>", page.text)


async def test_bundled_paired_script_completes_both_systems_on_staged(tmp_path):
    args = cli.build_parser().parse_args(
        [
            "eval-suite",
            "--set",
            "primary",
            "--faults",
            "case-02",
            "--system",
            "both",
            "--model",
            "scripted",
            "--models",
            "scripted",
            "--budget-usd",
            "0",
            "--script",
            "tests/scripted-paired.json",
            "--out",
            str(tmp_path / "suite"),
        ]
    )
    await execute(args)
    results = [json.loads(p.read_text()) for p in (tmp_path / "suite").glob("*/result.json")]
    assert len(results) == 2 and {r["system"] for r in results} == {"multi", "single"}
    assert all(r["graph"] == "staged" and r["status"] == "completed" for r in results)
    assert all(r["report"]["summary"] for r in results)
    assert Path(args.out / "leaderboard.json").is_file()
