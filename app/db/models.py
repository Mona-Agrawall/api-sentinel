"""Pydantic models for the documents stored in MongoDB.

Collections:
  * ``runs``    - one :class:`RunDoc` per run (summary, load stats, flags).
  * ``results`` - one :class:`ResultDoc` per functional test case.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


def utcnow() -> datetime:
    """Timezone-aware 'now' (stored as UTC)."""
    return datetime.now(UTC)


class RunStatus(StrEnum):
    """Lifecycle of a run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class RunRequest(BaseModel):
    """Body of ``POST /runs``."""

    spec_url: HttpUrl
    base_url: HttpUrl
    users: int = Field(default=10, ge=1)
    duration_s: int = Field(default=10, ge=1)


class CaseResult(BaseModel):
    """Outcome of sending one test case."""

    endpoint: str
    method: str
    path: str
    case_name: str
    category: str
    source: Literal["rule", "llm"]
    expected: Literal["2xx", "4xx", "not_5xx"]
    payload_hash: str
    request: dict[str, Any] = Field(default_factory=dict)
    status_code: int | None = None
    latency_ms: float | None = None
    passed: bool
    failure_reason: str | None = None
    response_excerpt: str | None = None


class ResultDoc(CaseResult):
    """A :class:`CaseResult` as stored, tied to its run."""

    run_id: str
    timestamp: datetime = Field(default_factory=utcnow)


class EndpointLoadStats(BaseModel):
    """Locust statistics for one endpoint over a load test."""

    endpoint: str
    requests: int
    failures: int
    error_rate: float
    rps: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    avg_ms: float
    max_ms: float


class Flag(BaseModel):
    """Something the regression detector wants a human to look at."""

    kind: Literal["latency_regression", "new_5xx", "newly_failing"]
    endpoint: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class GenerationInfo(BaseModel):
    """How the test cases for a run were produced."""

    total_cases: int = 0
    rule_cases: int = 0
    llm_cases: int = 0
    llm_invalid_dropped: int = 0
    llm_attempted: bool = False
    llm_fallback: bool = False
    llm_errors: dict[str, str] = Field(default_factory=dict)
    model: str | None = None
    prompt_version: str | None = None


class RunSummary(BaseModel):
    """Pass/fail counts for the functional part of a run."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    server_errors: int = 0


class RunDoc(BaseModel):
    """A single API Sentinel run."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    status: RunStatus = RunStatus.PENDING
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    spec_url: str
    base_url: str
    users: int
    duration_s: int
    spec_title: str | None = None
    endpoint_count: int = 0
    generation: GenerationInfo = Field(default_factory=GenerationInfo)
    summary: RunSummary = Field(default_factory=RunSummary)
    load: list[EndpointLoadStats] = Field(default_factory=list)
    load_error: str | None = None
    flags: list[Flag] = Field(default_factory=list)
    error: str | None = None
