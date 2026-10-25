"""Orchestrates one run: fetch spec -> generate cases -> functional -> load -> store -> detect."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import httpx

from app.config import Settings
from app.db.models import EndpointLoadStats, GenerationInfo, RunDoc, RunStatus, RunSummary, utcnow
from app.db.repo import RunRepository
from app.generator.generate import generate_all
from app.generator.llm_client import LLMClient
from app.generator.rule_cases import baseline_case
from app.generator.schema_parser import ApiSpec, parse_spec
from app.regression.detector import detect_functional_changes, detect_latency_regressions
from app.runner.functional import OriginGuard, run_functional
from app.runner.load import LoadTestError, run_load_test, select_targets

logger = logging.getLogger(__name__)

LoadRunner = Callable[[str, list[dict[str, object]], int, int], list[EndpointLoadStats]]


class RunError(RuntimeError):
    """A run could not complete (bad spec, unreachable target, ...)."""


async def fetch_spec(
    spec_url: str, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> ApiSpec:
    """Download and parse the spec. Only allow-listed hosts are contacted."""
    if not settings.host_allowed(spec_url):
        raise RunError("spec_url host is not in TARGET_ALLOWLIST")
    guard = OriginGuard(spec_url)
    async with httpx.AsyncClient(
        timeout=settings.request_timeout_s, transport=transport, event_hooks={"request": [guard]}
    ) as client:
        try:
            async with client.stream("GET", spec_url) as response:
                if response.status_code != 200:
                    raise RunError(f"fetching spec returned HTTP {response.status_code}")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > settings.max_spec_bytes:
                        raise RunError(f"spec is larger than {settings.max_spec_bytes} bytes")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise RunError(f"could not fetch spec: {type(exc).__name__}") from exc
    return parse_spec(b"".join(chunks))


async def execute_run(
    run_id: str,
    repo: RunRepository,
    settings: Settings,
    llm: LLMClient | None,
    *,
    spec_transport: httpx.AsyncBaseTransport | None = None,
    target_transport: httpx.AsyncBaseTransport | None = None,
    load_runner: LoadRunner = run_load_test,
) -> RunDoc:
    """Execute a pending run end to end and persist every stage. Never raises."""
    run = repo.get_run(run_id)
    if run is None:
        raise RunError(f"run {run_id} not found")
    run.status = RunStatus.RUNNING
    run.started_at = utcnow()
    repo.save_run(run)

    try:
        if not settings.host_allowed(run.base_url):
            raise RunError("base_url host is not in TARGET_ALLOWLIST")

        spec = await fetch_spec(run.spec_url, settings, spec_transport)
        run.spec_title = f"{spec.title} {spec.version}".strip()
        run.endpoint_count = len(spec.endpoints)
        if not spec.endpoints:
            raise RunError("spec contains no endpoints")

        generation = await asyncio.to_thread(generate_all, spec, settings, llm)
        run.generation = GenerationInfo(
            total_cases=len(generation.cases),
            **generation.model_dump(exclude={"cases"}),
        )
        if generation.llm_fallback:
            logger.info(
                "run %s: using rule-based cases (LLM fallback): %s", run.id, generation.llm_errors
            )
        repo.save_run(run)

        results = await run_functional(
            generation.cases,
            run.base_url,
            timeout_s=settings.request_timeout_s,
            concurrency=settings.functional_concurrency,
            transport=target_transport,
        )
        repo.insert_results(run, results)
        run.summary = RunSummary(
            total=len(results),
            passed=sum(r.passed for r in results),
            failed=sum(not r.passed for r in results),
            server_errors=sum(1 for r in results if r.status_code and r.status_code >= 500),
        )
        repo.save_run(run)

        targets = select_targets(
            [baseline_case(e) for e in spec.endpoints], settings.load_include_unsafe_methods
        )
        try:
            run.load = await asyncio.to_thread(
                load_runner, run.base_url, targets, run.users, run.duration_s
            )
        except LoadTestError as exc:
            run.load_error = str(exc)
            logger.warning("run %s: load test failed: %s", run.id, exc)

        history = repo.previous_completed_runs(run, settings.regression_baseline_runs)
        previous_results = repo.get_results(history[0].id) if history else None
        run.flags = [
            *detect_latency_regressions(
                run.load,
                [h.load for h in history],
                settings.regression_pct_threshold,
                settings.regression_ms_threshold,
            ),
            *detect_functional_changes(results, previous_results),
        ]
        run.status = RunStatus.COMPLETED
    except Exception as exc:
        logger.exception("run %s failed", run.id)
        run.status = RunStatus.FAILED
        run.error = str(exc) or type(exc).__name__
    finally:
        run.finished_at = utcnow()
        repo.save_run(run)
    return run
