from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import app.main as main_module
from app.config import Settings
from app.db.models import RunDoc, RunStatus
from app.db.repo import RunRepository


@contextmanager
def _started_app(
    repo: RunRepository, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Run the app's lifespan with ``repo`` and ``settings`` and a recording execute_run."""
    calls: list[tuple[Any, ...]] = []

    async def fake_execute_run(run_id: str, *args: Any, **kwargs: Any) -> None:
        calls.append((run_id, *args))

    monkeypatch.setattr(main_module, "execute_run", fake_execute_run)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    main_module.app.state.repo = repo
    try:
        with TestClient(main_module.app) as test_client:
            test_client.calls = calls
            yield test_client
    finally:
        del main_module.app.state.repo


@pytest.fixture
def client(
    repo: RunRepository, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    with _started_app(repo, settings, monkeypatch) as test_client:
        yield test_client


T0 = datetime(2026, 1, 1, tzinfo=UTC)
VALID = {
    "spec_url": "http://localhost:9000/openapi.json",
    "base_url": "http://localhost:9000/",
    "users": 5,
    "duration_s": 3,
}


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok", "database": True}


def test_create_run_stores_pending_run_and_schedules_it(
    client: TestClient, repo: RunRepository
) -> None:
    response = client.post("/runs", json=VALID)
    assert response.status_code == 202
    run_id = response.json()["id"]
    assert response.json()["status"] == "pending"
    stored = repo.get_run(run_id)
    assert stored is not None and stored.base_url == "http://localhost:9000"
    assert [c[0] for c in client.calls] == [run_id]


@pytest.mark.parametrize(
    ("override", "detail"),
    [
        ({"base_url": "http://example.com"}, "base_url host is not in TARGET_ALLOWLIST"),
        ({"spec_url": "http://169.254.169.254/latest"}, "spec_url host is not in"),
        ({"users": 100_000}, "users must be <="),
        ({"duration_s": 100_000}, "duration_s must be <="),
    ],
)
def test_create_run_rejects_unsafe_or_oversized(
    client: TestClient, override: dict[str, Any], detail: str
) -> None:
    response = client.post("/runs", json={**VALID, **override})
    assert response.status_code == 422
    assert detail in response.json()["detail"]
    assert client.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"base_url": "http://localhost"},
        {**VALID, "users": 0},
        {**VALID, "spec_url": "not a url"},
        {**VALID, "base_url": "ftp://localhost"},
    ],
)
def test_create_run_validates_body(client: TestClient, body: dict[str, Any]) -> None:
    assert client.post("/runs", json=body).status_code == 422


def test_list_and_get_runs(client: TestClient, repo: RunRepository) -> None:
    old = repo.create_run(
        RunDoc(spec_url="s", base_url="http://a", users=1, duration_s=1, created_at=T0)
    )
    new = repo.create_run(
        RunDoc(
            spec_url="s",
            base_url="http://b",
            users=1,
            duration_s=1,
            status=RunStatus.COMPLETED,
            created_at=T0 + timedelta(seconds=1),
        )
    )
    listed = client.get("/runs").json()
    assert [r["id"] for r in listed] == [new.id, old.id]
    assert [r["id"] for r in client.get("/runs", params={"base_url": "http://a"}).json()] == [
        old.id
    ]
    assert len(client.get("/runs", params={"limit": 1}).json()) == 1

    detail = client.get(f"/runs/{new.id}").json()
    assert detail["run"]["status"] == "completed" and detail["results"] == []
    assert client.get("/runs/does-not-exist").status_code == 404


def test_startup_fails_runs_interrupted_by_a_restart(
    repo: RunRepository, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def seed(status: RunStatus) -> str:
        return repo.create_run(
            RunDoc(spec_url="s", base_url="http://a", users=1, duration_s=1, status=status)
        ).id

    running, pending = seed(RunStatus.RUNNING), seed(RunStatus.PENDING)
    completed, failed = seed(RunStatus.COMPLETED), seed(RunStatus.FAILED)

    with _started_app(repo, settings, monkeypatch):
        pass

    for run_id in (running, pending):
        run = repo.get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.FAILED
        assert run.error == "interrupted: process restarted before run completed"
        assert run.finished_at is not None
    for run_id, expected in ((completed, RunStatus.COMPLETED), (failed, RunStatus.FAILED)):
        run = repo.get_run(run_id)
        assert run is not None and run.status == expected and run.error is None


API_KEY = "test-api-key-123"


@pytest.fixture
def secured_client(
    repo: RunRepository, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    secured = settings.model_copy(update={"api_key": SecretStr(API_KEY)})
    with _started_app(repo, secured, monkeypatch) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-API-Key": "wrong"},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Basic {API_KEY}"},
    ],
    ids=["missing", "wrong-header", "wrong-bearer", "wrong-scheme"],
)
def test_create_run_rejects_missing_or_wrong_key(
    secured_client: TestClient, repo: RunRepository, headers: dict[str, str]
) -> None:
    response = secured_client.post("/runs", json=VALID, headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert secured_client.calls == []
    assert repo.list_runs() == []


@pytest.mark.parametrize(
    "headers",
    [{"X-API-Key": API_KEY}, {"Authorization": f"Bearer {API_KEY}"}],
    ids=["x-api-key", "bearer"],
)
def test_create_run_accepts_correct_key(
    secured_client: TestClient, headers: dict[str, str]
) -> None:
    response = secured_client.post("/runs", json=VALID, headers=headers)
    assert response.status_code == 202
    assert [c[0] for c in secured_client.calls] == [response.json()["id"]]


def test_auth_is_checked_before_body_validation(secured_client: TestClient) -> None:
    assert secured_client.post("/runs", json={}).status_code == 401


def test_read_endpoints_stay_open_with_a_key_configured(secured_client: TestClient) -> None:
    assert secured_client.get("/health").status_code == 200
    assert secured_client.get("/runs").status_code == 200


def test_startup_warns_when_api_key_unset(
    repo: RunRepository,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING), _started_app(repo, settings, monkeypatch):
        pass
    assert "API_KEY is not set" in caplog.text


def test_api_key_never_logged(
    repo: RunRepository,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secured = settings.model_copy(update={"api_key": SecretStr(API_KEY)})
    with caplog.at_level(logging.DEBUG), _started_app(repo, secured, monkeypatch) as client:
        client.post("/runs", json=VALID, headers={"X-API-Key": API_KEY})
        client.post("/runs", json=VALID, headers={"X-API-Key": "wrong"})
    assert "API_KEY is not set" not in caplog.text
    assert API_KEY not in caplog.text


def test_rapid_run_starts_are_rate_limited(
    repo: RunRepository, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    limited = settings.model_copy(
        update={"api_key": SecretStr(API_KEY), "run_rate_limit_per_minute": 2}
    )
    headers = {"X-API-Key": API_KEY}
    with _started_app(repo, limited, monkeypatch) as client:
        statuses = [client.post("/runs", json=VALID, headers=headers) for _ in range(3)]
        assert [r.status_code for r in statuses] == [202, 202, 429]
        assert int(statuses[-1].headers["Retry-After"]) > 0
        assert len(client.calls) == 2
        # Rejected (unauthenticated) requests do not use up the caller's budget.
        assert client.post("/runs", json=VALID).status_code == 401
