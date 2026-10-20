"""M5 console controls and matched comparisons, through both repository adapters."""

import asyncio
import json
import re
from argparse import Namespace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from incident_investigator import cli
from incident_investigator.console import create_console
from incident_investigator.console_jobs import Jobs, RunRequest, build_command, job_plan
from incident_investigator.console_views import suite_metrics, system_metrics
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


def test_job_limit_covers_the_selected_attempts_and_profiles(repo):
    paid_plan = job_plan(
        RunRequest(
            faults=["case-02", "case-04", "case-06", "case-08", "case-10"],
            system="both",
            concurrency=1,
            wall_seconds=1800,
        ),
        repo,
    )
    assert paid_plan == {
        "attempts": 10,
        "concurrency": 1,
        "wall_seconds": 1800,
        "call_seconds": 30,
        "timeout_seconds": 19800,
        "termination_grace_seconds": 120,
    }
    all_plan = job_plan(RunRequest(case_set="all", system="both", repeats=2), repo)
    assert all_plan["attempts"] == 48 and all_plan["timeout_seconds"] == 6336
    assert (
        job_plan(RunRequest(case_set="variants", limits_profile="demo-live"), repo)["wall_seconds"]
        == 1200
    )
    if repo.execution_root is not None:
        with TestClient(create_console(repo)) as client:
            panel = client.get("/run").text
            token = re.search(r'name="console-token" content="([^"]+)"', panel)[1]
            response = client.post(
                "/api/preview",
                json={
                    "faults": ["case-02"],
                    "system": "both",
                    "wall_seconds": 1800,
                    "concurrency": 1,
                },
                headers={"X-Console-Token": token},
            )
            assert response.status_code == 200
            assert response.json()["plan"]["timeout_seconds"] == 3960
            assert 'id="job-limit"' in panel and "one-hour console timeout" not in panel


@pytest.mark.parametrize("mode", ["timeout", "cancel", "unresponsive"])
async def test_job_shutdown_finalizes_before_ingesting_or_escalates(
    repo, tmp_path, monkeypatch, mode
):
    from incident_investigator import console_jobs

    root = tmp_path / "child-home"
    output = root / "suites" / "child"
    started, exited = asyncio.Event(), asyncio.Event()
    signals = []

    class Child:
        returncode = None
        delivered = False

        @property
        def stdout(self):
            return self

        async def read(self, size):
            await exited.wait()
            if self.delivered:
                return b""
            self.delivered = True
            return b"final output retained\n"

        async def wait(self):
            await exited.wait()
            return self.returncode

        def terminate(self):
            signals.append("SIGTERM")
            if mode == "unresponsive":
                return
            output.mkdir(parents=True)
            for name in ("leaderboard.json", "budget.json", "spend-after.json"):
                (output / name).write_text('{"finalized": true}')
            self.returncode = -15
            exited.set()

        def kill(self):
            signals.append("SIGKILL")
            self.returncode = -9
            exited.set()

    child = Child()

    async def spawn(*args, **kwargs):
        started.set()
        return child

    monkeypatch.setattr(console_jobs.asyncio, "create_subprocess_exec", spawn)
    repo.artifacts.save_json(
        "jobs/child/job.json",
        {
            "id": "child",
            "status": "running",
            "plan": {
                "timeout_seconds": 60 if mode == "cancel" else 0.01,
                "termination_grace_seconds": 0.01,
            },
        },
    )
    jobs = Jobs(repo, root)
    task = asyncio.create_task(jobs.execute("child", ["child"], output))
    await started.wait()
    if mode == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    record = repo.artifacts.json("jobs/child/job.json")
    assert signals == (["SIGTERM", "SIGKILL"] if mode == "unresponsive" else ["SIGTERM"])
    assert record["status"] == ("interrupted" if mode == "cancel" else "failed")
    assert record["exit_code"] == child.returncode
    if mode != "unresponsive":
        assert "final output retained" in repo.artifacts.read("jobs/child/output.log").decode()
        for name in ("leaderboard.json", "budget.json", "spend-after.json"):
            assert repo.artifacts.json("suites/child/" + name) == {"finalized": True}
    else:
        assert record["termination"] == "SIGKILL"


async def test_cli_sigterm_cancels_once_and_allows_evaluation_finalization(monkeypatch):
    import signal

    from incident_investigator.grading import suite

    loop = asyncio.get_running_loop()
    callbacks, removed, finalized = {}, [], []
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda sig, callback: callbacks.update({sig: callback})
    )
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: removed.append(sig))

    async def evaluation(args):
        try:
            callbacks[signal.SIGTERM]()
            callbacks[signal.SIGTERM]()
            await asyncio.sleep(0)
        finally:
            await asyncio.sleep(0)
            finalized.append(asyncio.current_task().cancelling())

    monkeypatch.setattr(suite, "execute", evaluation)
    task = asyncio.create_task(cli._graceful_evaluation(Namespace()))
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finalized == [1] and removed == [signal.SIGTERM]


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
            "at least USD 0.010000",
            "pass@1",
            "Model / tool calls",
        ):
            assert label in page
        assert "single · variants · staged" in client.get("/").text


def test_suite_shows_recorded_headlines_and_incident_grounding_failures(repo):
    populate(repo)
    store = repo.artifacts
    for system in ("multi", "single"):
        for case in ("case-02", "reliability-01", "reliability-02"):
            prefix = f"suites/paired/{case}-{system}"
            g = store.json(prefix + "/grade.json")
            g["grounding"] = {"hypothesis_claim_fraction": 0.5 if system == "multi" else None}
            g["diagnosis_checks"] = {"culprit_service": True, "justified_uncertainty": False}
            store.save_json(prefix + "/grade.json", g)
        prefix = f"suites/paired/case-02-{system}"
        r = store.json(prefix + "/result.json")
        r["status"] = "failed"
        store.save_json(prefix + "/result.json", r)
    heads = [
        {
            "system": system,
            "suite_kind": "primary",
            "models": {"all": "scripted"},
            "runs": 3,
            "execution_failures": 1,
            "accuracy": 1 / 3,
            "mean_grounding": 0.625,
            "grounding_measured": 2,
            "grounding_total": 3,
            "mean_model_calls": 4,
            "mean_tool_calls": 5,
            "mean_cost": None,
            "mean_known_cost": 0.01,
            "mean_wall_seconds": 12,
            "faults": {"case-02": {"n": 3, "correct": 1}},
        }
        for system in ("multi", "single")
    ]
    store.save_json("suites/paired/leaderboard.json", {"models": heads})
    store.write("suites/paired/leaderboard.md", b"# Retained leaderboard\n")
    repo.runs.refresh()
    suite = repo.runs.suites()[0]
    runs = repo.runs.runs({"suite": suite["id"]})
    with TestClient(create_console(repo)) as client:
        page = client.get(f"/suites/{suite['id']}?k=2").text
        assert page.index("System summary") < page.index("Primary accuracy")
        assert page.count("0.62") + page.count("0.63") >= 2  # Recorded 0.625, not recomputed 0.5.
        assert "(2/3)" in page and "at least 0.010000 (some calls unmeasured)" in page
        assert "1 selected primary incident cases" in page
        assert "Execution failures" in page and "0.50 (1/1)" in page and "unknown (0/1)" in page
        assert "0.667" in page and "0.000" in page  # Mean pass@2 / pass^2 per incident.
        assert (
            client.get(f"/suites/{suite['id']}/leaderboard.md").text == "# Retained leaderboard\n"
        )
        assert client.get("/suites/missing/leaderboard.md").status_code == 404
        reliability_table = page.split("<h2>Reliability</h2>")[1].split("</table>")[0]
        for row in re.findall(r"<tr>(.*?)</tr>", reliability_table, re.S):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            if cells:
                if "reliability-01" in cells[0]:
                    assert cells[-1].strip() == "-"
                else:
                    assert all(cell.strip() == "-" for cell in cells[2:5])
        for run in runs:
            grade_page = client.get(f"/runs/{run['id']}?tab=grade").text
            checks = re.findall(r'<div class="check-row">(.*?)</div>', grade_page, re.S)
            assert any("justified_uncertainty" in check for check in checks) == (
                run["fault"] == "reliability-02"
            )


def test_live_system_summary_is_provisional_without_a_leaderboard(repo):
    populate(repo)
    repo.artifacts.save_json("jobs/live/job.json", {"status": "running"})
    suite = repo.runs.suites()[0]
    runs = repo.runs.runs({"suite": suite["id"]})
    heads = system_metrics({}, runs, 2)
    assert len(heads) == 4  # Both systems; primary and variants remain separate.
    assert all(h["mean_grounding"] is None and h["grounding_measured"] == 0 for h in heads)
    assert all(h["execution_failures"] == 0 for h in heads)
    single = next(h for h in heads if h["system"] == "single" and h["suite_kind"] == "primary")
    assert single["mean_cost"] is None and single["mean_known_cost"] == 0.01
    with TestClient(create_console(repo)) as client:
        page = client.get(f"/suites/{suite['id']}").text
        assert "Provisional: the suite is incomplete or still running" in page
        assert "at least 0.010000 (some calls unmeasured)" in page
        assert "Open leaderboard.md" not in page


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
