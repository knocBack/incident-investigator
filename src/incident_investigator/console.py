"""Loopback-only evaluation console; all artifact access goes through repository ports."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from incident_investigator.console_jobs import Jobs, RunRequest, display_command, graph_variants
from incident_investigator.console_views import (
    markdown,
    observations,
    pretty,
    suite_metrics,
    summary,
)
from incident_investigator.contracts import PROFILES
from incident_investigator.storage import Repositories, repositories

DIMENSIONS = ("Diagnosis", "Support", "Contradictions", "Uncertainty", "Actions")


class RubricEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    score: str = Field(pattern=r"^(pass|partial|fail|unreviewed)$")
    reason: str = Field(default="", max_length=10000)


class Rubric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimensions: dict[str, RubricEntry]


def create_console(repo: Repositories | None = None, *, jobs: Jobs | None = None) -> FastAPI:
    repo = repo or repositories()
    if jobs is None and repo.execution_root is not None:
        jobs = Jobs(repo, repo.execution_root)
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if jobs:
            await jobs.close()

    app = FastAPI(title="Incident Investigator Console", lifespan=lifespan)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver", "[::1]"]
    )
    assets = Path(__file__).parent / "web"
    templates = Jinja2Templates(directory=str(assets))

    def safe_phoenix_url(value):
        from incident_investigator.telemetry.tracing import local_url

        try:
            return local_url(value) if isinstance(value, str) else None
        except ValueError:
            return None

    templates.env.filters.update(pretty=pretty, phoenix_url=safe_phoenix_url)
    templates.env.globals.update(dimensions=DIMENSIONS)
    app.mount("/static", StaticFiles(directory=str(assets)), name="static")

    @app.middleware("http")
    async def local_mutations(request: Request, call_next):
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return HTMLResponse("Cross-origin writes are disabled", status_code=403)
            if not hmac.compare_digest(request.headers.get("x-console-token", ""), token):
                return HTMLResponse("Console token required", status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    def render(request, name, **context):
        return templates.TemplateResponse(
            request=request,
            name=name,
            context={"token": token, "home": repo.artifacts.location("index.json"), **context},
        )

    def refresh():
        repo.runs.refresh()

    def get_run(ident):
        run = repo.runs.get(ident)
        if run is None:
            raise HTTPException(404, "Unknown run")
        return run

    def artifact(run, name):
        return repo.artifacts.json(run["prefix"] + "/" + name)

    @app.get("/", response_class=HTMLResponse)
    def runs(request: Request):
        refresh()
        filters = {
            k: request.query_params.get(k, "")
            for k in ("fault", "model", "graph", "verdict", "suite")
        }
        rows = repo.runs.runs(filters, request.query_params.get("sort", "created"))
        options = {k: sorted({r[k] for r in repo.runs.runs() if r[k]}) for k in filters}
        return render(
            request,
            "runs.html",
            runs=rows,
            filters=filters,
            options=options,
            suites=repo.runs.suites(),
            stats=summary(repo.runs.runs()),
        )

    @app.get("/runs/{ident}", response_class=HTMLResponse)
    def run_detail(request: Request, ident: str, tab: str = "report"):
        refresh()
        run = get_run(ident)
        if tab not in {"report", "grade", "rubric", "evidence"}:
            raise HTTPException(404, "Unknown tab")
        result = artifact(run, "result.json")
        state = result.get("state", {})
        refs = {**state.get("evidence", {}), **state.get("tool_receipts", {})}
        text = (repo.artifacts.read(run["prefix"] + "/report.md") or b"No report retained.").decode(
            errors="replace"
        )
        frozen = repo.artifacts.read(run["prefix"] + "/snapshot/observations.jsonl")
        rows = (
            repo.artifacts.lines(run["prefix"] + "/snapshot/observations.jsonl")
            if frozen is not None
            else list(state.get("evidence", {}).values())
        )
        if frozen is not None:
            retrieved = {
                (row.get("source"), row.get("source_record_id")): key
                for key, row in state.get("evidence", {}).items()
            }
            rows = [
                {
                    **row,
                    "evidence_id": retrieved.get((row.get("source"), row.get("source_record_id"))),
                }
                for row in rows
            ]
        review = repo.artifacts.json(f"reviews/{ident}/human-rubric.json")
        trace = artifact(run, "phoenix.json")
        if trace.get("url"):
            from incident_investigator.telemetry.tracing import local_url

            try:
                local_url(trace["url"])
            except (ValueError, TypeError):
                trace["url"] = None
        return render(
            request,
            "run.html",
            run=run,
            tab=tab,
            result=result,
            report=markdown(text, ident, refs),
            grade=run["grade"],
            expected=repo.catalog.expected(run["fault"]),
            review=review,
            telemetry=observations(rows, request.query_params),
            frozen=frozen is not None,
            snapshot=artifact(run, "snapshot/manifest.json"),
            trace=trace,
        )

    @app.get("/runs/{ident}/evidence/{evidence_id}", response_class=HTMLResponse)
    def evidence(request: Request, ident: str, evidence_id: str):
        refresh()
        run = get_run(ident)
        state = artifact(run, "result.json").get("state", {})
        record = state.get("evidence", {}).get(evidence_id)
        kind = "Observation"
        if record is None:
            record, kind = state.get("tool_receipts", {}).get(evidence_id), "Tool receipt"
        if record is None:
            raise HTTPException(404, "Unknown evidence")
        return render(
            request, "evidence.html", run=run, record=record, evidence_id=evidence_id, kind=kind
        )

    @app.post("/api/runs/{ident}/rubric")
    def rubric(ident: str, review: Rubric):
        refresh()
        get_run(ident)
        if set(review.dimensions) != set(DIMENSIONS):
            raise HTTPException(422, "Complete all five rubric dimensions")
        repo.artifacts.save_json(f"reviews/{ident}/human-rubric.json", review.model_dump())
        return {"saved": True}

    @app.get("/suites/{ident}", response_class=HTMLResponse)
    def suite(request: Request, ident: str, k: int = Query(4, ge=1, le=100)):
        refresh()
        selected = next((s for s in repo.runs.suites() if s["id"] == ident), None)
        if selected is None:
            raise HTTPException(404, "Unknown suite")
        rows = repo.runs.runs({"suite": ident})
        return render(
            request,
            "suite.html",
            suite=selected,
            runs=rows,
            stats=summary(rows),
            k=k,
            metrics=suite_metrics(rows, k),
        )

    @app.get("/compare", response_class=HTMLResponse)
    def compare(request: Request, left: str = "", right: str = "", kind: str = "runs"):
        refresh()
        if kind not in {"runs", "suites"}:
            raise HTTPException(422, "Compare runs or suites")
        options = repo.runs.runs() if kind == "runs" else repo.runs.suites()
        values = []
        for ident in (left, right):
            if kind == "runs":
                values.append(repo.runs.get(ident))
            else:
                selected = next((s for s in options if s["id"] == ident), None)
                values.append(
                    {**selected, **summary(repo.runs.runs({"suite": ident}))} if selected else None
                )
        fields = (
            ("verdict", "status", "graph", "model", "calls", "events", "cost", "wall_seconds")
            if kind == "runs"
            else ("runs", "correct", "failures", "calls", "events", "cost", "wall_seconds")
        )
        return render(
            request,
            "compare.html",
            options=options,
            values=values,
            kind=kind,
            left=left,
            right=right,
            fields=fields,
        )

    @app.get("/run", response_class=HTMLResponse)
    def panel(request: Request):
        refresh()
        return render(
            request,
            "panel.html",
            faults=repo.catalog.faults(),
            runs=repo.runs.runs(),
            graphs=graph_variants(),
            profiles=PROFILES,
            enabled=jobs is not None,
        )

    @app.post("/api/preview")
    def preview(body: RunRequest):
        if jobs is None:
            raise HTTPException(409, "Local process launcher unavailable for this storage backend")
        refresh()
        try:
            argv, _ = jobs.preview(body, "preview")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"command": display_command(argv)}

    @app.post("/api/jobs")
    async def launch(body: RunRequest):
        if jobs is None:
            raise HTTPException(409, "Local process launcher unavailable for this storage backend")
        refresh()
        ident = uuid4().hex
        try:
            jobs.start(body, ident)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"id": ident, "url": f"/jobs/{ident}"}

    @app.get("/jobs/{ident}", response_class=HTMLResponse)
    def job(request: Request, ident: str):
        if not jobs or not jobs.get(ident):
            raise HTTPException(404, "Unknown job")
        return render(request, "job.html", job=jobs.get(ident))

    @app.get("/api/jobs/{ident}/events")
    async def job_events(ident: str):
        if not jobs or not jobs.get(ident):
            raise HTTPException(404, "Unknown job")

        async def events():
            while True:
                record = jobs.get(ident)
                yield "data: " + json.dumps(record) + "\n\n"
                if record["status"] != "running":
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(events(), media_type="text/event-stream")

    return app
