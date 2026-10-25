"""Environment-driven settings for API Sentinel.

All configuration comes from environment variables (optionally a ``.env`` file).
Secrets are held as :class:`pydantic.SecretStr` so they never appear in logs or reprs.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Validated application settings."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM
    groq_api_key: SecretStr | None = None
    groq_model: str = "llama-3.3-70b-versatile"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    llm_enabled: bool = True
    llm_timeout_s: float = Field(default=30.0, gt=0, le=300)
    llm_max_cases_per_endpoint: int = Field(default=12, ge=1, le=50)

    # Storage
    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_db: str = "api_sentinel"

    # Safety
    target_allowlist: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "target_api"]
    )
    max_spec_bytes: int = Field(default=5_000_000, gt=0)

    # Runner
    request_timeout_s: float = Field(default=10.0, gt=0, le=120)
    functional_concurrency: int = Field(default=10, ge=1, le=100)
    max_cases_per_endpoint: int = Field(default=60, ge=1, le=500)
    max_users: int = Field(default=200, ge=1, le=5000)
    max_duration_s: int = Field(default=600, ge=1, le=3600)
    load_include_unsafe_methods: bool = False

    # Regression detection
    regression_baseline_runs: int = Field(default=5, ge=1, le=50)
    regression_pct_threshold: float = Field(default=20.0, ge=0)
    regression_ms_threshold: float = Field(default=50.0, ge=0)

    log_level: str = "INFO"

    @field_validator("target_allowlist", mode="before")
    @classmethod
    def _split_allowlist(cls, value: object) -> object:
        if isinstance(value, str):
            return [h.strip().lower() for h in value.split(",") if h.strip()]
        return value

    @field_validator("groq_api_key", mode="before")
    @classmethod
    def _empty_key_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("mongodb_uri")
    @classmethod
    def _check_mongo_uri(cls, value: str) -> str:
        if not value.startswith(("mongodb://", "mongodb+srv://", "mongomock://")):
            raise ValueError(
                "MONGODB_URI must start with mongodb://, mongodb+srv:// or mongomock://"
            )
        return value

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid LOG_LEVEL {value!r}")
        return level

    @model_validator(mode="after")
    def _check_allowlist(self) -> Settings:
        if not self.target_allowlist:
            raise ValueError("TARGET_ALLOWLIST must contain at least one host")
        return self

    @property
    def llm_available(self) -> bool:
        """True when the LLM is enabled and an API key is configured."""
        return self.llm_enabled and self.groq_api_key is not None

    def host_allowed(self, url: str) -> bool:
        """Return True if ``url`` is http(s) and its host is in the target allow-list."""
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return False
        return parts.hostname.lower() in self.target_allowlist


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings (cached)."""
    return Settings()
