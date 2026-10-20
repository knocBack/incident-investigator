"""Retained interruption metadata must describe one cancelled comparison, not two pairs."""

import asyncio
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from incident_investigator.console import create_console
from incident_investigator.console_jobs import Jobs
from incident_investigator.console_views import (
    paired_metrics,
    present_run,
    suite_metrics,
    system_metrics,
)
from incident_investigator.storage import repositories


@pytest.fixture(params=["filesystem", "memory"])
def repo(request, tmp_path):
    return repositories(root=tmp_path / "home", backend=request.param)


def interrupted_pair(repo):
    store = repo.artifacts
    store.save_json("jobs/cancel/job.json", {"id": "cancel", "status": "cancelled"})
    store.save_json(
        "suites/pair/batch.json",
        {
            "launch_id": "cancel",
            "systems": ["multi", "single"],
            "faults": ["case-02"],
            "models": ["scripted"],
            "repeats": 2,
        },
    )
    for system in ("multi", "single"):
        prefix = f"suites/pair/{system}"
        grade = {
            "system": system,
            "models": {} if system == "single" else {"all": "scripted"},
            "suite_attempt": {"fault": "case-02", "model": "scripted", "system": system},
            "verdict": "no_report" if system == "single" else "correct",
            "process": {"execution_status": "interrupted" if system == "single" else "completed"},
        }
        store.save_json(prefix + "/grade.json", grade)
        if system == "single":
            store.save_json(
                prefix + "/attempt-error.json",
                {
                    "exception_type": "CancelledError",
                    "launch_id": "cancel",
                    "reason": "interrupted",
                },
            )
        else:
            store.save_json(
                prefix + "/result.json",
                {
                    "run_id": "multi",
                    "system": system,
                    "models": {"all": "scripted"},
                    "launch_id": "cancel",
                    "status": "completed",
                },
            )
    # A legacy board split this interruption into its own empty-model aggregate.
    store.save_json("suites/pair/leaderboard.json", {"models": [{"models": {}}]})
    repo.runs.refresh()


async def test_attempt_error_only_stays_in_one_pair_and_cancelled_suite_is_explicit(repo):
    interrupted_pair(repo)
    store = repo.artifacts
    saved = store.json("suites/pair/single/grade.json")
    runs = [present_run(r, store) for r in repo.runs.runs()]
    single = next(r for r in runs if r["system"] == "single")
    assert single["status"] == "interrupted" and single["model"] == "scripted"
    assert single["execution_label"] == "cancelled in flight"
    assert single["outcome"].startswith("cancelled in flight")
    metrics = suite_metrics(runs, 1)
    pair = paired_metrics(metrics, provisional=False)
    assert len(pair) == 1
    assert "Single did not finish (cancelled in flight)" in pair[0]["outcome"]
    assert "no retained attempt" not in pair[0]["outcome"]
    # Also recover identity when a caller supplies older indexed rows directly.
    single["model"] = ""
    assert {m["model"] for m in suite_metrics(runs, 1)} == {"scripted"}
    single["model"] = "scripted"
    aggregates = system_metrics(store.json("suites/pair/leaderboard.json"), runs, 1)
    assert all(len(row["outcomes"]) == 1 for row in aggregates)
    suite_id = repo.runs.suites()[0]["id"]
    jobs = Jobs(repo, Path("unused"))
    job = jobs.get("cancel")
    assert "runs failed" not in job["outcome"] and "1 cancelled in flight" in job["outcome"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_console(repo)), base_url="http://testserver"
    ) as client:
        page = (await client.get("/suites/" + suite_id)).text
    assert "Cancelled by user." in page
    assert "2 of 4 planned attempts ran" in page and "2 planned attempts never started" in page
    assert "cancelled in flight" in page and "What do the verdicts mean?" in page
    assert store.json("suites/pair/single/grade.json") == saved


@pytest.mark.parametrize("command", ["eval", "eval-suite"])
def test_cli_sigterm_finalizes_without_a_traceback(tmp_path, command):
    # A real child and SIGTERM exercise asyncio.run's cancellation boundary for both commands.
    script = """
import asyncio, sys
from incident_investigator import cli
from incident_investigator.grading import suite
async def evaluate(args):
    print("ready", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.sleep(0)
        args.out.mkdir()
        (args.out / "finalized.txt").write_text("retained")
suite.execute = evaluate
sys.argv = ["incident-investigator", sys.argv[1], "--out", sys.argv[2]]
sys.argv += ["unused"] if sys.argv[1] == "eval" else ["--models", "scripted"]
cli.main()
"""
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", script, command, str(tmp_path / "out")],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        child.terminate()
        output, _ = child.communicate(timeout=10)
        assert child.returncode == 143
        assert output.strip() == "Cancelled; retained results finalized"
        assert (tmp_path / "out/finalized.txt").read_text() == "retained"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


async def test_cancel_racing_natural_success_keeps_completed_status(repo, tmp_path, monkeypatch):
    from incident_investigator import console_jobs

    exited = asyncio.Event()
    signals = []

    class Child:
        returncode = None

        @property
        def stdout(self):
            return self

        async def read(self, size):
            await exited.wait()
            return b""

        async def wait(self):
            await exited.wait()
            return self.returncode

        def terminate(self):
            signals.append("SIGTERM")

    child = Child()

    async def spawn(*args, **kwargs):
        return child

    original_wait = asyncio.wait

    async def race(tasks, **kwargs):
        child.returncode = 0
        exited.set()
        # Cancel arrives while execute is waiting, after the child has exited successfully.
        assert jobs.cancel("race")["status"] == "cancelling"
        return await original_wait(tasks, **kwargs)

    monkeypatch.setattr(console_jobs.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(console_jobs.asyncio, "wait", race)
    repo.artifacts.save_json(
        "jobs/race/job.json",
        {
            "id": "race",
            "status": "running",
            "plan": {"timeout_seconds": 30, "termination_grace_seconds": 1},
        },
    )
    jobs = Jobs(repo, tmp_path)
    jobs.tasks["race"] = asyncio.create_task(jobs.execute("race", ["child"], tmp_path / "output"))
    await jobs.tasks["race"]
    record = jobs.get("race")
    assert record["status"] == "completed" and record["exit_code"] == 0
    assert record.get("stop_reason") is None and signals == []
    with pytest.raises(ValueError, match="Only a running job"):
        jobs.cancel("race")


async def test_identical_incident_comparisons_are_grouped_once(repo):
    store = repo.artifacts
    store.save_json(
        "suites/repeated/batch.json",
        {
            "systems": ["multi", "single"],
            "faults": ["case-02", "case-04"],
            "models": ["scripted"],
            "repeats": 1,
        },
    )
    store.save_json("suites/repeated/leaderboard.json", {"models": []})
    for fault in ("case-02", "case-04"):
        for system in ("multi", "single"):
            prefix = f"suites/repeated/{fault}-{system}"
            store.save_json(
                prefix + "/result.json",
                {
                    "run_id": fault + system,
                    "status": "completed",
                    "models": {"all": "scripted"},
                    "system": system,
                },
            )
            store.save_json(prefix + "/grade.json", {"family": fault, "verdict": "inconclusive"})
    repo.runs.refresh()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_console(repo)), base_url="http://testserver"
    ) as client:
        text = (await client.get("/suites/" + repo.runs.suites()[0]["id"])).text
    assert text.count("Neither system achieved a correct final verdict.") == 1
    assert "4 of 4 planned attempts ran" in text
    assert "case-02 · scripted · primary; case-04 · scripted · primary" in text
