from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.db.models import RunDoc, RunStatus
from app.db.repo import RunRepository

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _completed(repo: RunRepository, minute: int, users: int, duration_s: int = 15) -> RunDoc:
    return repo.create_run(
        RunDoc(
            spec_url="s",
            base_url="http://a",
            users=users,
            duration_s=duration_s,
            status=RunStatus.COMPLETED,
            created_at=T0 + timedelta(minutes=minute),
        )
    )


def test_history_matches_load_profile_by_default(repo: RunRepository) -> None:
    ten_a = _completed(repo, 0, users=10)
    _completed(repo, 1, users=200)
    _completed(repo, 2, users=10, duration_s=60)
    ten_b = _completed(repo, 3, users=10)
    current = RunDoc(
        spec_url="s", base_url="http://a", users=10, duration_s=15, created_at=T0 + timedelta(1)
    )

    assert [r.id for r in repo.previous_completed_runs(current, 5)] == [ten_b.id, ten_a.id]


def test_history_across_profiles_when_requested(repo: RunRepository) -> None:
    _completed(repo, 0, users=10)
    heavy = _completed(repo, 1, users=200)
    current = RunDoc(
        spec_url="s", base_url="http://a", users=10, duration_s=15, created_at=T0 + timedelta(1)
    )

    assert [r.id for r in repo.previous_completed_runs(current, 1, same_load_profile=False)] == [
        heavy.id
    ]
    assert repo.previous_completed_runs(current, 5) != []
    assert heavy.id not in {r.id for r in repo.previous_completed_runs(current, 5)}
