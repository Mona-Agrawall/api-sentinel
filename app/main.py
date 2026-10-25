"""FastAPI application: trigger runs and read their results."""

from __future__ import annotations

import logging
import math
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Security,
    status,
)
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.db.models import ResultDoc, RunDoc, RunRequest
from app.db.mongo_client import create_client
from app.db.repo import RunRepository
from app.generator.llm_client import LLMClient
from app.pipeline import execute_run
from app.rate_limit import TokenBucketLimiter

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
    app.state.run_limiter = TokenBucketLimiter(settings.run_rate_limit_per_minute)
    logger.info("LLM available: %s (model %s)", settings.llm_available, settings.groq_model)
    if settings.api_key is None:
        logger.warning(
            "API_KEY is not set: POST /runs accepts unauthenticated requests. "
            "Set API_KEY for anything other than local development."
        )
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

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)


def authenticate(
    request: Request,
    settings: SettingsDep,
    api_key: Annotated[str | None, Security(api_key_header)],
    bearer: Annotated[HTTPAuthorizationCredentials | None, Security(bearer_scheme)],
) -> str:
    """Dependency for mutating endpoints: check the API key, return the rate-limit key.

    The key is accepted as ``X-API-Key: <key>`` or ``Authorization: Bearer <key>``. Without a
    configured ``API_KEY`` every caller is let through and rate-limited by client address.
    """
    expected = settings.api_key
    if expected is None:
        return f"ip:{request.client.host if request.client else 'unknown'}"
    candidates = [c for c in (api_key, bearer.credentials if bearer else None) if c]
    secret = expected.get_secret_value().encode()
    if not any(secrets.compare_digest(c.encode(), secret) for c in candidates):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing or invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return "api-key"


def limit_run_starts(request: Request, caller: Annotated[str, Depends(authenticate)]) -> None:
    """Dependency: allow at most ``RUN_RATE_LIMIT_PER_MINUTE`` run starts per caller."""
    limiter: TokenBucketLimiter = request.app.state.run_limiter
    retry_after = limiter.acquire(caller)
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many runs started; retry later",
            headers={"Retry-After": str(math.ceil(retry_after))},
        )


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


@app.post(
    "/runs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RunCreated,
    dependencies=[Depends(limit_run_starts)],
)
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
