from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.config import Settings
from app.db.models import RunDoc, RunStatus
from app.db.repo import RunRepository


@pytest.fixture
def client(
    repo: RunRepository, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    calls: list[tuple[Any, ...]] = []

    async def fake_execute_run(run_id: str, *args: Any, **kwargs: Any) -> None:
        calls.append((run_id, *args))

    monkeypatch.setattr(main_module, "execute_run", fake_execute_run)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    main_module.app.state.repo = repo
    with TestClient(main_module.app) as test_client:
        test_client.calls = calls
        yield test_client
    del main_module.app.state.repo


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
