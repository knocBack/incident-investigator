"""Console cancellation keeps the existing graceful child-finalization boundary."""

import asyncio
import re

import httpx
import pytest

from incident_investigator.console import create_console
from incident_investigator.console_jobs import Jobs, RunRequest, suite_budget_warning
from incident_investigator.console_views import paired_metrics, present_run, suite_metrics
from incident_investigator.storage import repositories


@pytest.fixture(autouse=True)
def mocked_operational_preflight(monkeypatch):
    async def operational(self, request, repo, info):
        return {"message": "Mocked provider preflight"}

    monkeypatch.setattr(
        "incident_investigator.console_preflight.LaunchPreflight.operational", operational
    )


@pytest.fixture(params=["filesystem", "memory"])
def repo(request, tmp_path):
    return repositories(root=tmp_path / "registry", backend=request.param)


@pytest.mark.parametrize("mode", ["graceful", "force", "spawning", "closed_stdout"])
async def test_cancel_endpoint_finalizes_or_escalates_and_rejects_finished_jobs(
    repo, tmp_path, monkeypatch, mode
):
    from incident_investigator import console_jobs

    root = tmp_path / "child-home"
    output = root / "suites" / "child"
    spawned, release_spawn, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
    signals = []

    class Child:
        returncode = None
        delivered = False

        @property
        def stdout(self):
            return self

        async def read(self, size):
            if mode == "closed_stdout":
                return b""
            await exited.wait()
            if self.delivered:
                return b""
            self.delivered = True
            return b"retained final output\n"

        async def wait(self):
            await exited.wait()
            return self.returncode

        def terminate(self):
            signals.append("SIGTERM")
            if mode == "force":
                return

            def finalize():
                output.mkdir(parents=True)
                for name in ("leaderboard.json", "budget.json", "spend-after.json"):
                    (output / name).write_text('{"finalized":true}')
                self.returncode = -15
                exited.set()

            # Final files appear on a later loop turn, so ingesting before wait() fails.
            asyncio.get_running_loop().call_soon(finalize)

        def kill(self):
            signals.append("SIGKILL")
            self.returncode = -9
            exited.set()

    child = Child()

    async def spawn(*args, **kwargs):
        spawned.set()
        if mode == "spawning":
            await release_spawn.wait()
        return child

    monkeypatch.setattr(console_jobs.asyncio, "create_subprocess_exec", spawn)
    repo.artifacts.save_json(
        "jobs/child/job.json",
        {
            "id": "child",
            "status": "running",
            "plan": {"timeout_seconds": 60, "termination_grace_seconds": 0.01},
        },
    )
    jobs = Jobs(repo, root)
    jobs.tasks["child"] = asyncio.create_task(jobs.execute("child", ["child"], output))
    await spawned.wait()
    app = create_console(repo, jobs=jobs)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        form = (await client.get("/run")).text
        token = {"X-Console-Token": re.search(r'name="console-token" content="([^"]+)"', form)[1]}
        assert (
            await client.post("/api/jobs/child/cancel", json={"confirm": True})
        ).status_code == 403
        assert (
            await client.post("/api/jobs/child/cancel", json={"confirm": False}, headers=token)
        ).status_code == 422
        assert (
            await client.post("/api/jobs/missing/cancel", json={"confirm": True}, headers=token)
        ).status_code == 404
        response = await client.post(
            "/api/jobs/child/cancel", json={"confirm": True}, headers=token
        )
        assert response.status_code == 200 and response.json()["status"] == "cancelling"
        assert not response.json()["can_cancel"]
        assert (
            await client.post("/api/jobs/child/cancel", json={"confirm": True}, headers=token)
        ).status_code == 409
        release_spawn.set()
        await asyncio.wait_for(jobs.tasks["child"], timeout=2)
        record = jobs.get("child")
        assert record["status"] == "cancelled" and record["heading"] == "cancelled by user"
        assert record["stop_reason"] == "cancelled by user" and not record["can_cancel"]
        assert (
            await client.post("/api/jobs/child/cancel", json={"confirm": True}, headers=token)
        ).status_code == 409
        events = await client.get("/api/jobs/child/events")
        assert '"status": "cancelled"' in events.text
    assert signals == (["SIGTERM", "SIGKILL"] if mode == "force" else ["SIGTERM"])
    if mode != "force":
        for name in ("leaderboard.json", "budget.json", "spend-after.json"):
            assert repo.artifacts.json("suites/child/" + name) == {"finalized": True}
        if mode != "closed_stdout":
            assert "retained final output" in record["output"]
    else:
        assert "exports may be incomplete" in record["outcome"]
    assert "child" not in jobs.cancel_events


async def test_cancel_before_executor_starts_does_not_spawn_a_child(repo, tmp_path, monkeypatch):
    from incident_investigator import console_jobs

    async def forbidden(*args, **kwargs):
        raise AssertionError("cancelled before admission must not spawn")

    monkeypatch.setattr(console_jobs.asyncio, "create_subprocess_exec", forbidden)
    jobs = Jobs(repo, tmp_path / "home")
    jobs.start(RunRequest(faults=["case-02"]), "early")
    assert jobs.cancel("early")["status"] == "cancelling"
    await jobs.tasks["early"]
    assert jobs.get("early")["heading"] == "cancelled by user"


@pytest.mark.parametrize(
    "reason,status,label",
    [
        ("output_truncated", "failed", "output truncated"),
        ("OpenRouter HTTP 502", "failed", "provider HTTP 502"),
        (
            "ModelCallError: suite budget reservation refused call",
            "failed",
            "budget cap refused further calls",
        ),
        ("TimeoutError:", "failed", "request timed out"),
        ("interrupted", "interrupted", "interrupted"),
    ],
)
def test_incomplete_attempts_never_create_a_diagnostic_winner(repo, reason, status, label):
    store = repo.artifacts
    for system in ("multi", "single"):
        store.save_json(
            f"suites/pair/{system}/result.json",
            {
                "run_id": system,
                "system": system,
                "models": {system: "test/model"},
                "status": "completed" if system == "multi" else status,
                "stop_reason": None if system == "multi" else reason,
            },
        )
        store.save_json(
            f"suites/pair/{system}/grade.json",
            {
                "run_id": system,
                "family": "case-04",
                "verdict": "correct" if system == "multi" else "no_report",
            },
        )
    repo.runs.refresh()
    rows = [present_run(r, store) for r in repo.runs.runs()]
    text = paired_metrics(suite_metrics(rows, 1), provisional=False)[0]["outcome"]
    assert "Multi: correct" in text and f"Single did not finish ({label})" in text
    assert "not a like-for-like comparison" in text and "higher correct-verdict rate" not in text
    assert "Per retained attempt" not in text


def test_whole_suite_cap_guidance_uses_planned_allowances_without_blocking_launch():
    request = RunRequest(
        provider="openrouter", model_id="test/model", budget_usd="0.45", run_budget_usd="0.08"
    )
    warning = suite_budget_warning(request, {"attempts": 10})
    assert "10 attempts × per-run cap USD 0.08 = USD 0.80 vs suite cap USD 0.45" in warning
    assert "later attempts may be refused" in warning and "not expected spend" in warning
    assert suite_budget_warning(request, {"attempts": 1}) == ""
    assert (
        suite_budget_warning(request.model_copy(update={"budget_usd": "0.80"}), {"attempts": 10})
        == ""
    )
    assert (
        suite_budget_warning(request.model_copy(update={"provider": "scripted"}), {"attempts": 10})
        == ""
    )


async def test_budget_warning_preview_is_display_only_and_finished_jobs_refuse_cancel(
    repo, tmp_path, monkeypatch
):
    jobs = Jobs(repo, tmp_path)

    async def catalogue():
        return {
            "test/model": {
                "context_length": 128000,
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                "supported_parameters": [],
            }
        }

    monkeypatch.setattr(jobs.preflight, "catalogue", catalogue)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_console(repo, jobs=jobs)),
        base_url="http://testserver",
    ) as client:
        token = {
            "X-Console-Token": re.search(
                r'name="console-token" content="([^"]+)"', (await client.get("/run")).text
            )[1]
        }
        response = await client.post(
            "/api/preview",
            headers=token,
            json={
                "provider": "openrouter",
                "model_id": "test/model",
                "faults": ["case-02", "case-04", "case-06", "case-08", "case-10"],
                "system": "both",
                "budget_usd": "0.45",
                "run_budget_usd": "0.08",
                "confirm_paid": True,
            },
        )
        assert response.status_code == 200 and response.json()["launch_allowed"]
        assert (
            "10 attempts × per-run cap USD 0.08 = USD 0.80 vs suite cap USD 0.45"
            in response.json()["budget_warning"]
        )
        for status in ("completed", "failed", "stopped", "interrupted"):
            repo.artifacts.save_json(f"jobs/{status}/job.json", {"id": status, "status": status})
            assert (
                await client.post(
                    f"/api/jobs/{status}/cancel", headers=token, json={"confirm": True}
                )
            ).status_code == 409
            assert repo.artifacts.json(f"jobs/{status}/job.json")["status"] == status
