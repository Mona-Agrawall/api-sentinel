"""Shared fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.db.mongo_client import create_client
from app.db.repo import RunRepository

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def settings() -> Settings:
    """Hermetic settings: no .env file, no LLM key, in-memory DB."""
    return Settings(
        _env_file=None,
        groq_api_key=None,
        api_key=None,
        mongodb_uri="mongomock://test",
        target_allowlist="localhost,127.0.0.1,target.test",
        regression_baseline_runs=5,
        regression_pct_threshold=20,
        regression_ms_threshold=50,
    )


@pytest.fixture
def repo(settings: Settings) -> RunRepository:
    """A repository backed by mongomock."""
    repository = RunRepository(create_client(settings.mongodb_uri)[settings.mongodb_db])
    repository.ensure_indexes()
    return repository


@pytest.fixture
def shop_yaml() -> str:
    """OpenAPI 3.0 YAML fixture using $ref, allOf, nullable and boolean exclusiveMinimum."""
    return (FIXTURES / "shop_openapi30.yaml").read_text(encoding="utf-8")
