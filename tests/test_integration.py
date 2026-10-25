"""Integration tests against the demo target API (``target_api``)."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn

from app.config import Settings
from app.db.models import EndpointLoadStats, RunDoc, RunStatus
from app.db.repo import RunRepository
from app.generator.llm_client import LLMClient
from app.generator.rule_cases import baseline_case
from app.generator.schema_parser import parse_spec
from app.pipeline import execute_run
from app.runner.load import run_load_test, select_targets
from target_api.app import app as target_app

BASE = "http://target.test"


def _fake_load(p95_by_endpoint: dict[str, float]):  # type: ignore[no-untyped-def]
    def runner(
        base_url: str, targets: list[dict[str, object]], users: int, duration_s: int
    ) -> list[EndpointLoadStats]:
        return [
            EndpointLoadStats(
                endpoint=str(t["endpoint"]),
                requests=100,
                failures=0,
                error_rate=0,
                rps=50,
                p50_ms=p95_by_endpoint.get(str(t["endpoint"]), 5) / 2,
                p95_ms=p95_by_endpoint.get(str(t["endpoint"]), 5),
                p99_ms=p95_by_endpoint.get(str(t["endpoint"]), 5),
                avg_ms=3,
                max_ms=10,
            )
            for t in targets
        ]

    return runner


def _run(repo: RunRepository, settings: Settings, p95: dict[str, float]) -> RunDoc:
    run = repo.create_run(
        RunDoc(spec_url=f"{BASE}/openapi.json", base_url=BASE, users=2, duration_s=1)
    )
    transport = httpx.ASGITransport(app=target_app, raise_app_exceptions=False)
    return asyncio.run(
        execute_run(
            run.id,
            repo,
            settings,
            LLMClient(settings),
            spec_transport=transport,
            target_transport=transport,
            load_runner=_fake_load(p95),
        )
    )


def test_pipeline_finds_null_bug_and_latency_regression(
    repo: RunRepository, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TARGET_API_FAULTS", raising=False)
    healthy = _run(repo, settings, {"GET /items/{item_id}": 6})
    assert healthy.status == RunStatus.COMPLETED, healthy.error
    assert healthy.generation.llm_fallback and healthy.generation.rule_cases > 0
    assert healthy.summary.failed == 0, [
        (r.case_name, r.status_code) for r in repo.get_results(healthy.id) if not r.passed
    ]
    assert healthy.flags == []
    assert {s.endpoint for s in healthy.load} == {
        "GET /health",
        "GET /items",
        "GET /items/{item_id}",
        "GET /search",
    }

    time.sleep(0.01)  # distinct created_at
    monkeypatch.setenv("TARGET_API_FAULTS", "1")
    broken = _run(repo, settings, {"GET /items/{item_id}": 210})
    assert broken.status == RunStatus.COMPLETED, broken.error

    failing = {r.case_name for r in repo.get_results(broken.id) if not r.passed}
    assert failing == {"description: null", "body: only required fields"}
    assert broken.summary.server_errors == 2

    by_kind: dict[str, list[str]] = {}
    for flag in broken.flags:
        by_kind.setdefault(flag.kind, []).append(flag.endpoint)
    assert by_kind == {
        "latency_regression": ["GET /items/{item_id}"],
        "new_5xx": ["POST /items", "POST /items"],
    }


def test_pipeline_records_failure_for_bad_spec(repo: RunRepository, settings: Settings) -> None:
    run = repo.create_run(
        RunDoc(spec_url=f"{BASE}/nope.json", base_url=BASE, users=1, duration_s=1)
    )
    transport = httpx.ASGITransport(app=target_app, raise_app_exceptions=False)
    result = asyncio.run(
        execute_run(
            run.id, repo, settings, None, spec_transport=transport, target_transport=transport
        )
    )
    assert result.status == RunStatus.FAILED
    assert result.error == "fetching spec returned HTTP 404"
    assert result.finished_at is not None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def live_target() -> Iterator[str]:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(target_app, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "target API did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.mark.integration
def test_real_locust_run_measures_injected_delay(
    live_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = parse_spec(target_app.openapi())
    endpoint = next(e for e in spec.endpoints if e.key == "GET /items/{item_id}")
    targets = select_targets([baseline_case(endpoint)])

    monkeypatch.delenv("TARGET_API_FAULTS", raising=False)
    (fast,) = run_load_test(live_target, targets, users=3, duration_s=2)
    monkeypatch.setenv("TARGET_API_FAULTS", "1")
    (slow,) = run_load_test(live_target, targets, users=3, duration_s=2)

    assert fast.endpoint == slow.endpoint == "GET /items/{item_id}"
    assert fast.requests > 0 and slow.requests > 0
    assert fast.error_rate == slow.error_rate == 0
    assert fast.p95_ms < 150
    assert slow.p95_ms >= 200
