"""Presentation fixes keep saved results and evaluator outcomes intact."""

import re

import pytest
from fastapi.testclient import TestClient

from incident_investigator.console import create_console
from incident_investigator.console_jobs import Jobs
from incident_investigator.console_views import markdown, paired_metrics, present_run, suite_metrics
from incident_investigator.storage import repositories


@pytest.fixture(params=["filesystem", "memory"])
def repo(request, tmp_path):
    return repositories(root=tmp_path / "home", backend=request.param)


def retained_suite(repo, status="completed"):
    store = repo.artifacts
    store.save_json("jobs/job/job.json", {"id": "job", "status": status})
    store.save_json(
        "suites/polish/batch.json",
        {
            "launch_id": "job",
            "faults": ["case-02"],
            "models": ["scripted"],
            "systems": ["multi", "single"],
            "repeats": 2,
            "skipped": [{"system": "single", "reason": "suite cancelled before admission"}],
        },
    )
    store.save_json(
        "suites/polish/multi/result.json",
        {
            "run_id": "multi",
            "launch_id": "job",
            "status": "interrupted" if status != "completed" else "completed",
            "system": "multi",
            "models": {"all": "scripted"},
            "diagnosis": "inconclusive",
            "hypotheses": [{"statement": "A tentative cause", "confidence": "high"}],
            "usage": {"multi": {"model_calls": 8, "tool_calls": 3}},
            "state": {
                "calls": {
                    **{f"m{i}": {"kind": "model"} for i in range(8)},
                    **{f"t{i}": {"kind": "tool"} for i in range(3)},
                }
            },
        },
    )
    grade = {
        "run_id": "multi",
        "family": "case-02",
        "verdict": "inconclusive",
        "grounding": {"hypothesis_claim_fraction": 0},
    }
    store.save_json("suites/polish/multi/grade.json", grade)
    # A persisted leaderboard may still include the interruption as a failure.
    store.save_json(
        "suites/polish/leaderboard.json",
        {
            "models": [
                {
                    "models": {"all": "scripted"},
                    "system": "multi",
                    "runs": 1,
                    "execution_failures": 1,
                    "mean_grounding": 0,
                    "grounding_measured": 1,
                    "mean_model_calls": 8,
                    "mean_tool_calls": 3,
                }
            ]
        },
    )
    repo.runs.refresh()
    return repo.runs.suites()[0], repo.runs.runs()[0], grade


@pytest.mark.parametrize("status", ["cancelled", "interrupted"])
def test_cancelled_counts_and_shutdown_banner_use_launch_status(repo, tmp_path, status):
    suite, run, grade = retained_suite(repo, status)
    with TestClient(create_console(repo, jobs=Jobs(repo, tmp_path))) as client:
        landing = client.get("/").text
        page = client.get(f"/suites/{suite['id']}").text
        cancelled = int(status == "cancelled")
        assert re.search(r"Execution failures</label>\s*<strong>" + str(1 - cancelled), landing)
        assert re.search(r"Cancelled in flight</label>\s*<strong>" + str(cancelled), landing)
        assert ("Cancelled by user." in page) == bool(cancelled)
        assert ("Stopped before finishing." in page) == (not cancelled)
        assert f"<span>Execution failures</span><span>{1 - cancelled}</span>" in page
        assert f"<span>Cancelled in flight</span><span>{cancelled}</span>" in page
        if cancelled:
            job = client.get("/jobs/job").text
            assert "1 of 4 planned attempts ran" in job
            assert "1 cancelled in flight" in job
    assert repo.artifacts.json(run["prefix"] + "/grade.json") == grade
    assert (
        repo.artifacts.json(suite["prefix"] + "/leaderboard.json")["models"][0][
            "execution_failures"
        ]
        == 1
    )


def test_tentative_cause_calls_grounding_and_suite_navigation(repo, tmp_path):
    suite, run, _ = retained_suite(repo)
    with TestClient(create_console(repo, jobs=Jobs(repo, tmp_path))) as client:
        pages = [
            client.get(f"/suites/{suite['id']}").text,
            client.get(f"/runs/{run['id']}").text,
            client.get("/jobs/job").text,
        ]
        for page in pages:
            assert "tentative cause found but not committed; see Grade" in page
            assert "8 model / 3 tool calls" in page
            assert "retained a high-confidence hypothesis" not in page
        assert "No ranked hypothesis has both valid supporting references" in pages[0]
        landing = client.get("/").text
        options = re.search(r'<select name="suite">(.*?)</select>', landing, re.S)[1]
        assert 'value="' + suite["id"] + '"' in options
        assert "case-02 · scripted" in options
        assert ">Suites</h2>" in landing
        assert 'id="job-report-links" open' in pages[2]
        assert "1 retained run reports" in pages[2]
        form = client.get("/run").text
        assert "multi / single (both, eval-suite)" in form
        assert "Scripted capture records replay steps" in form
        assert "under this Suite cap" in form
        actions = re.search(
            r'<div class="actions">\s*<button[^>]*id="retry-preview".*?</div>', form, re.S
        )[0]
        assert 'id="launch-job"' in actions and 'id="launch-status"' in actions


def test_missing_side_summary_keeps_retained_cancellation(repo):
    _, run, _ = retained_suite(repo, "cancelled")
    row = present_run(run, repo.artifacts)
    pair = paired_metrics(
        suite_metrics([row], 1),
        provisional=False,
        skipped=[
            {
                "fault": "case-02",
                "model": "scripted",
                "system": "single",
                "reason": "cancelled before admission",
            }
        ],
        systems=["multi", "single"],
    )[0]
    assert "Multi: 1 of 1 attempts did not finish (cancelled in flight)" in pair["outcome"]
    assert "Single has no retained attempt" in pair["outcome"]


def test_report_explains_empty_hypothesis_support_and_formats_observation_json():
    text = """### Supporting evidence (0)

None recorded.

### Contradicting evidence (0)

None recorded.

## Facts

- A separately cited fact
  - `obs-1` — logs: {"service": "inventory", "message": "<script>alert(1)</script>"}
"""
    rendered = markdown(
        text,
        "run",
        {
            "obs-1": {
                "source": "logs",
                "data": {"service": "inventory", "message": "<script>alert(1)</script>"},
            }
        },
    )
    assert "No supporting references were attached to this ranked hypothesis" in rendered
    assert "No contradicting references were attached to this ranked hypothesis" in rendered
    assert "those citations do not count as hypothesis support in Grade" in rendered
    assert '<a class="ref" href="/runs/run/evidence/obs-1">obs-1</a>' in rendered
    assert "<summary>Observation JSON</summary><pre>{\n" in rendered
    assert "&lt;script&gt;" in rendered and "<script>" not in rendered
    assert "None recorded." not in rendered
