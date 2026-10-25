"""FastAPI application: trigger runs and read their results."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, status
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.db.models import ResultDoc, RunDoc, RunRequest
from app.db.mongo_client import create_client
from app.db.repo import RunRepository
from app.generator.llm_client import LLMClient
from app.pipeline import execute_run

logger = logging.getLogger(__name__)

INTERRUPTED_MESSAGE = "interrupted: process restarted before run completed"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the DB repository and LLM client once per process."""
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if not hasattr(app.state, "repo"):  # tests inject their own
        client = create_client(settings.mongodb_uri)
        app.state.repo = RunRepository(client[settings.mongodb_db])
    app.state.repo.ensure_indexes()
    interrupted = app.state.repo.fail_interrupted_runs(INTERRUPTED_MESSAGE)
    if interrupted:
        logger.warning("marked %d interrupted run(s) as failed", interrupted)
    app.state.settings = settings
    app.state.llm = LLMClient(settings)
    logger.info("LLM available: %s (model %s)", settings.llm_available, settings.groq_model)
    yield


app = FastAPI(
    title="API Sentinel",
    version="0.1.0",
    description="LLM-generated edge-case tests, load tests and latency regression detection.",
    lifespan=lifespan,
)


def get_repo(request: Request) -> RunRepository:
    """Dependency: the run repository."""
    repo: RunRepository = request.app.state.repo
    return repo


def get_app_settings(request: Request) -> Settings:
    """Dependency: settings."""
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    return settings


RepoDep = Annotated[RunRepository, Depends(get_repo)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


class RunCreated(BaseModel):
    """Response of ``POST /runs``."""

    id: str
    status: str


class RunDetail(BaseModel):
    """Response of ``GET /runs/{id}``."""

    run: RunDoc
    results: list[ResultDoc]


@app.get("/health")
def health(repo: RepoDep) -> dict[str, Any]:
    """Liveness plus database connectivity."""
    db_ok = repo.ping()
    return {"status": "ok" if db_ok else "degraded", "database": db_ok}


@app.post("/runs", status_code=status.HTTP_202_ACCEPTED, response_model=RunCreated)
async def create_run(
    body: RunRequest,
    background: BackgroundTasks,
    request: Request,
    repo: RepoDep,
    settings: SettingsDep,
) -> RunCreated:
    """Start a run in the background and return its id."""
    spec_url, base_url = str(body.spec_url), str(body.base_url).rstrip("/")
    for label, url in (("spec_url", spec_url), ("base_url", base_url)):
        if not settings.host_allowed(url):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{label} host is not in TARGET_ALLOWLIST ({', '.join(settings.target_allowlist)})",
            )
    if body.users > settings.max_users:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"users must be <= {settings.max_users}"
        )
    if body.duration_s > settings.max_duration_s:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"duration_s must be <= {settings.max_duration_s}",
        )

    run = repo.create_run(
        RunDoc(spec_url=spec_url, base_url=base_url, users=body.users, duration_s=body.duration_s)
    )
    background.add_task(execute_run, run.id, repo, settings, request.app.state.llm)
    return RunCreated(id=run.id, status=run.status.value)


@app.get("/runs", response_model=list[RunDoc])
def list_runs(
    repo: RepoDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    base_url: str | None = None,
) -> list[RunDoc]:
    """List runs, newest first."""
    return repo.list_runs(limit=limit, base_url=base_url)


@app.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str, repo: RepoDep) -> RunDetail:
    """One run with all of its case results."""
    run = repo.get_run(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    return RunDetail(run=run, results=repo.get_results(run_id))
