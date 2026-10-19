"""The same repository contract runs against both adapters; no provider calls."""

import asyncio
import json
from argparse import Namespace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from incident_investigator import cli
from incident_investigator.console import DIMENSIONS, create_console
from incident_investigator.console_jobs import RunRequest, build_command, display_command
from incident_investigator.console_views import suite_metrics
from incident_investigator.grading.grader import grade_run
from incident_investigator.storage import FileEvalCatalog, repositories


@pytest.fixture(scope="module")
def scripted_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("console-run")
    directory = asyncio.run(
        cli.demo(
            Namespace(
                model="scripted",
                script=Path("tests/scripted-demo.json"),
                fault="bad_configuration",
                out=root,
            )
        )
    )
    grade_run(directory)
    return directory


@pytest.fixture(params=["filesystem", "memory"])
def repo(request, tmp_path):
    return repositories(root=tmp_path / "workspace", backend=request.param)


def test_repository_contract(repo, scripted_run):
    # Import preserves source files and repeat imports/overlapping roots are idempotent.
    before = {p: p.read_bytes() for p in scripted_run.rglob("*") if p.is_file()}
    repo.runs.import_roots([scripted_run, scripted_run.parent, scripted_run])
    runs = repo.runs.runs()
    assert len(runs) == 1
    run = runs[0]
    assert run["fault"] == "bad_configuration" and run["verdict"] == "inconclusive"
    assert run["status"] == "completed"
    assert repo.runs.runs({"fault": "missing"}) == []
    assert repo.runs.get(run["id"]) == run
    assert repo.artifacts.read(run["prefix"] + "/report.md")
    assert repo.artifacts.read(run["prefix"] + "/snapshot/observations.jsonl")
    assert repo.artifacts.read("does-not-exist") is None
    with pytest.raises(ValueError, match="read-only"):
        repo.artifacts.write(run["prefix"] + "/grade.json", b"changed")
    for key in ("../outside", "/absolute", "nested/../../outside", "bad\\key"):
        with pytest.raises(ValueError):
            repo.artifacts.write(key, b"escape")
    repo.artifacts.write("reviews/example/note.md", b"review")
    assert repo.artifacts.read("reviews/example/note.md") == b"review"
    assert "reviews/example/note.md" in repo.artifacts.list_keys("reviews/")
    assert before == {p: p.read_bytes() for p in before}


def test_suites_unknown_usage_and_separate_grade_overlay(repo):
    store = repo.artifacts
    store.save_json("suites/batch/batch.json", {"repeats": 4})
    for index, verdict in enumerate(("correct", "wrong", "correct", "no_report")):
        store.save_json(
            f"suites/batch/r{index}/result.json",
            {
                "run_id": f"r{index}",
                "case_id": "case",
                "graph": "explicit",
                "status": "completed",
                "models": {"critic": "test/model"},
                "usage": {"critic": {"cost": "unknown"}},
            },
        )
        store.save_json(
            f"suites/batch/r{index}/grade.json", {"family": "fault", "verdict": verdict}
        )
    repo.runs.refresh()
    assert len(repo.runs.suites()) == 1
    rows = repo.runs.runs()
    assert all(r["cost"] is None for r in rows)
    metric = suite_metrics(rows, 2)[0]
    assert metric["pass1"] == 0.5
    assert metric["passk"] == pytest.approx(5 / 6)
    assert metric["powerk"] == pytest.approx(1 / 6)
    store.save_json("suites/grade-later/r0/grade.json", {"run_id": "r0", "verdict": "partial"})
    repo.runs.refresh()
    assert next(r for r in repo.runs.runs() if r["run_id"] == "r0")["verdict"] == "partial"


def token(client):
    import re

    return {
        "X-Console-Token": re.search(
            r'name="console-token" content="([^"]+)"', client.get("/").text
        )[1]
    }


def test_grade_overlay_uses_newest_timestamp(repo):
    # Random job keys must not decide which re-grade the viewer displays.
    store = repo.artifacts
    store.save_json("runs/original/result.json", {"run_id": "regraded"})
    store.save_json("runs/original/grade.json", {"graded_at": 15, "verdict": "partial"})
    for name, timestamp, verdict in (("a", 30, "correct"), ("z", 20, "wrong")):
        store.save_json(
            f"suites/{name}/grade.json",
            {"run_id": "regraded", "graded_at": timestamp, "verdict": verdict},
        )
    repo.runs.refresh()
    assert repo.runs.runs()[0]["verdict"] == "correct"
    store.save_json("runs/original/grade.json", {"graded_at": 40, "verdict": "inconclusive"})
    repo.runs.refresh()
    assert repo.runs.runs()[0]["verdict"] == "inconclusive"


@pytest.mark.parametrize("command", [["demo"], ["investigate", "--case", "missing"]])
def test_cli_rejects_missing_script_before_creating_artifacts(
    command, monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("II_HOME", str(tmp_path / "unused"))
    monkeypatch.setattr("sys.argv", ["incident-investigator", *command])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "scripted model requires --script" in capsys.readouterr().err
    assert not (tmp_path / "unused").exists()


def test_utf8_artifacts_and_grading_timestamp(scripted_run, tmp_path):
    from incident_investigator.workspace import read_json, read_lines, write_json

    text = "診断: délai élevé"
    path = tmp_path / "unicode.json"
    write_json(path, {"note": text})
    assert text.encode("utf-8") in path.read_bytes()
    assert read_json(path) == {"note": text}
    path.write_text(json.dumps({"note": text}, ensure_ascii=False) + "\n", encoding="utf-8")
    assert list(read_lines(path)) == [{"note": text}]
    first = grade_run(scripted_run, output=tmp_path / "first")
    second = grade_run(scripted_run, output=tmp_path / "second")
    assert 0 < first["graded_at"] <= second["graded_at"]
    assert read_json(tmp_path / "second" / "grade.json")["graded_at"] == second["graded_at"]


def test_retained_observations_use_utf8_on_legacy_locale(tmp_path, monkeypatch):
    from dataclasses import replace

    from incident_investigator.reporting import retain_snapshot
    from incident_investigator.sources.loader import load_snapshot

    # Simulate a locale that cannot encode this evidence when encoding is omitted.
    original = Path.write_text

    def locale_write(path, text, encoding=None, **kwargs):
        return original(path, text, encoding=encoding or "cp1252", **kwargs)

    monkeypatch.setattr(Path, "write_text", locale_write)
    snapshot = load_snapshot("dev-00")
    text = "診断: délai élevé"
    row = snapshot.observations[0]
    snapshot = replace(
        snapshot, observations=(row.model_copy(update={"data": {**row.data, "note": text}}),)
    )
    retain_snapshot(snapshot, tmp_path)
    raw = (tmp_path / "snapshot" / "observations.jsonl").read_bytes()
    assert text.encode("utf-8") in raw
    assert json.loads(raw)["data"]["note"] == text


def test_all_run_tabs_and_rubric_save(repo, scripted_run):
    repo.runs.import_roots([scripted_run])
    ident = repo.runs.runs()[0]["id"]
    with TestClient(create_console(repo)) as client:
        headers = token(client)
        assert client.get("/").status_code == 200
        for tab in ("report", "grade", "rubric", "evidence"):
            page = client.get(f"/runs/{ident}?tab={tab}")
            assert page.status_code == 200, page.text
            assert "bad_configuration" in page.text
        review = {
            "dimensions": {
                d: {"score": "partial", "reason": "Needs evidence review <script>"}
                for d in DIMENSIONS
            }
        }
        assert client.post(f"/api/runs/{ident}/rubric", json=review).status_code == 403
        assert (
            client.post(f"/api/runs/{ident}/rubric", json=review, headers=headers).status_code
            == 200
        )
        assert repo.artifacts.json(f"reviews/{ident}/human-rubric.json") == review
        assert (
            "Needs evidence review &lt;script&gt;" in client.get(f"/runs/{ident}?tab=rubric").text
        )
        assert (
            client.post(
                f"/api/runs/{ident}/rubric",
                json=review,
                headers={**headers, "Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert client.get("/compare").status_code == 200
        assert client.get("/run").status_code == 200
        assert client.get("/runs/unknown").status_code == 404


def test_evidence_link_and_snapshot_gap(repo):
    repo.artifacts.save_json(
        "runs/r/result.json",
        {
            "state": {
                "evidence": {
                    "evidence-id": {
                        "source": "logs",
                        "data": {"service": "checkout", "body": "<script>"},
                    },
                }
            }
        },
    )
    repo.artifacts.write(
        "runs/r/report.md", b"# Report\n\nCitation `evidence-id`\n\n<script>alert(1)</script>"
    )
    repo.runs.refresh()
    ident = repo.runs.runs()[0]["id"]
    with TestClient(create_console(repo)) as client:
        report = client.get(f"/runs/{ident}").text
        assert f"/runs/{ident}/evidence/evidence-id" in report
        assert "<script>alert(1)</script>" not in report
        assert (
            "Historical full snapshot unavailable" in client.get(f"/runs/{ident}?tab=evidence").text
        )
        assert "&lt;script&gt;" in client.get(f"/runs/{ident}/evidence/evidence-id").text
        assert client.get(f"/runs/{ident}/evidence/absent").status_code == 404


def test_fault_discovery_and_catalog_contract(tmp_path):
    from incident_investigator.storage import MemoryEvalCatalog

    definitions = {"new-fault": {"version": "test.v2", "answer": "private"}}
    for name, definition in definitions.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(definition))
    for catalog in (
        FileEvalCatalog(tmp_path, injectable=["new-fault", "lab-only"]),
        MemoryEvalCatalog(definitions, injectable=["new-fault", "lab-only"]),
    ):
        assert catalog.faults() == [
            {"id": "lab-only", "injectable": True, "evaluable": False, "version": "lab registry"},
            {"id": "new-fault", "injectable": True, "evaluable": True, "version": "test.v2"},
        ]
        result = catalog.expected("new-fault")
        result["answer"] = "changed"
        assert catalog.expected("new-fault")["answer"] == "private"
        assert catalog.expected("../../etc/passwd") == {}


def test_run_panel_commands_and_paid_confirmation(repo, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    request = RunRequest(faults=["bad_configuration"])
    command, output = build_command(request, repo, "test-job", root=tmp_path)
    shown = display_command(command)
    assert shown.startswith("incident-investigator eval-suite ")
    assert "--concurrency 2" in shown and "--budget-usd 0" in shown
    assert output == tmp_path / "suites" / "test-job"
    assert Path(command[command.index("--script") + 1]).is_file()
    request = request.model_copy(update={"provider": "openrouter", "model_id": "test/model"})
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-test-key")
    with pytest.raises(ValueError, match="confirmation"):
        build_command(request, repo, "paid")
    request.confirm_paid = True
    command, _ = build_command(request, repo, "paid")
    assert "secret-test-key" not in display_command(command)
    with pytest.raises(ValueError, match="distinct"):
        build_command(RunRequest(faults=["invented"]), repo, "bad")
    for amount in ("NaN", "-1", "Infinity", "invalid"):
        with pytest.raises(ValueError):
            RunRequest(budget_usd=amount)


def test_pass_k_page(repo):
    for i in range(4):
        repo.artifacts.save_json(
            f"suites/b/r{i}/result.json", {"run_id": str(i), "graph": "current"}
        )
        repo.artifacts.save_json(
            f"suites/b/r{i}/grade.json", {"family": "f", "verdict": "correct" if i < 2 else "wrong"}
        )
    repo.artifacts.save_json("suites/b/batch.json", {"repeats": 4})
    repo.runs.refresh()
    suite = repo.runs.suites()[0]
    with TestClient(create_console(repo)) as client:
        response = client.get(f"/suites/{suite['id']}?k=2")
        assert response.status_code == 200
        assert "0.833" in response.text and "0.167" in response.text
        assert client.get(f"/suites/{suite['id']}?k=0").status_code == 422


def test_real_cli_job_stream_and_default_workspace(tmp_path):
    from incident_investigator.console_jobs import Jobs

    root = tmp_path / "home"
    repo = repositories(root=root, backend="filesystem")
    jobs = Jobs(repo, root)
    with TestClient(create_console(repo, jobs=jobs)) as client:
        headers = token(client)
        body = {
            "command": "eval-suite",
            "faults": ["bad_configuration"],
            "provider": "scripted",
            "limits_profile": "demo",
            "repeats": 1,
            "concurrency": 2,
            "budget_usd": "0",
        }
        preview = client.post("/api/preview", json=body, headers=headers)
        assert preview.status_code == 200
        assert "incident-investigator eval-suite" in preview.json()["command"]
        launched = client.post("/api/jobs", json=body, headers=headers)
        assert launched.status_code == 200, launched.text
        ident = launched.json()["id"]
        # SSE stays attached until completion; the real child makes no provider calls.
        stream = client.get(f"/api/jobs/{ident}/events")
        events = [
            json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")
        ]
        assert events[-1]["status"] == "completed", events[-1]
        assert events[-1]["exit_code"] == 0
        assert len(events[-1]["runs"]) == 1
        run = events[-1]["runs"][0]
        assert run["verdict"] == "inconclusive"
        assert run["launch_id"] == ident
        assert run["location"].startswith(str(root))
        assert client.get(f"/runs/{run['id']}?tab=evidence").status_code == 200
        assert client.get(f"/jobs/{ident}").status_code == 200
        # Regression: standalone eval --out lacked suite_attempt metadata and crashed
        # in leaderboard generation. Group by family without changing grading criteria.
        grading = client.post(
            "/api/jobs", json={"command": "eval", "run": run["id"]}, headers=headers
        )
        assert grading.status_code == 200
        stream = client.get(f"/api/jobs/{grading.json()['id']}/events")
        events = [
            json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: ")
        ]
        assert events[-1]["status"] == "completed"
        repo.runs.refresh()
        assert (
            len(repo.runs.runs()) == 1
        )  # Staged evaluator input must never be indexed as a new run.


def test_storage_publish_and_materialize_contract(repo, tmp_path):
    store = repo.artifacts
    source = tmp_path / "child-output"
    source.mkdir()
    (source / "report.md").write_text("A retained CLI report")
    store.ingest_tree(source, "runs/one")
    store.write("runs/one-extra/not-in-run.txt", b"sibling")
    target = tmp_path / "materialized"
    store.materialize("runs/one", target)
    assert (target / "report.md").read_text() == "A retained CLI report"
    assert list(target.iterdir()) == [target / "report.md"]
    for key in ("../outside", "/absolute"):
        with pytest.raises(ValueError):
            store.read(key)
    store.save_json("jobs/example/input/result.json", {"run_id": "staged-copy"})
    repo.runs.refresh()
    assert not repo.runs.runs()


def test_unfinished_run_keeps_source_and_metadata(repo):
    repo.artifacts.save_json(
        "runs/unfinished/run.json",
        {
            "run_id": "incomplete",
            "case_id": "frozen-case",
            "models": {"critic": "test/model"},
            "graph": "current",
            "created": 1000,
        },
    )
    repo.artifacts.write("runs/unfinished/snapshot/observations.jsonl", b"")
    repo.artifacts.save_json(
        "runs/unfinished/phoenix.json",
        {"status": "recording", "url": "http://127.0.0.1:6006/projects/project/traces/live-trace"},
    )
    repo.runs.refresh()
    run = repo.runs.runs()[0]
    assert run["case"] == "frozen-case" and run["model"] == "test/model"
    assert run["status"] == "unfinished" and run["verdict"] == "ungraded"
    assert run["created"] == 1000
    with TestClient(create_console(repo)) as client:
        assert "http://127.0.0.1:6006/projects/project/traces/live-trace" in client.get("/").text
        assert "Full frozen input retained" in client.get(f"/runs/{run['id']}?tab=evidence").text
