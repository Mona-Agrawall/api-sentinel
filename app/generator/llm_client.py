"""Groq (LLaMA-3) client that turns an endpoint description into validated test cases.

Design rules:
  * JSON-mode output, a request timeout, and exactly one retry.
  * Every generated case is validated with Pydantic; invalid ones are dropped and counted.
  * This module never raises on LLM problems - callers get a result with ``error`` set
    and fall back to rule-based cases.
  * The API key is only ever placed in the Authorization header; it is never logged.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.config import Settings
from app.generator.models import Expected, TestCase
from app.generator.prompts import CURRENT_VERSION, get_prompt, render_user_prompt
from app.generator.rule_cases import baseline_case
from app.generator.schema_parser import Endpoint

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2  # one try + one retry


class LLMCase(BaseModel):
    """Schema each LLM-generated case must satisfy."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=200)
    category: str = Field(default="llm", max_length=50)
    path_params: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    include_body: bool = True
    body: Any = None
    expected: Literal["2xx", "4xx", "not_5xx"]
    rationale: str = Field(default="", max_length=1000)

    @field_validator("query")
    @classmethod
    def _scalar_query_values(cls, value: dict[str, Any]) -> dict[str, Any]:
        for key, item in value.items():
            if isinstance(item, dict):
                raise ValueError(f"query param {key!r} must not be an object")
        return value


class LLMResponse(BaseModel):
    """Top-level JSON object returned by the model."""

    cases: list[dict[str, Any]]


class GenerationResult(BaseModel):
    """Outcome of asking the LLM for cases for one endpoint."""

    endpoint: str
    cases: list[TestCase] = Field(default_factory=list)
    invalid_dropped: int = 0
    attempts: int = 0
    error: str | None = None


class LLMClient:
    """Thin Groq chat-completions client (OpenAI-compatible API)."""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport
        self.prompt_version = CURRENT_VERSION

    @property
    def model(self) -> str:
        """Configured model name."""
        return self._settings.groq_model

    def generate_cases(self, endpoint: Endpoint) -> GenerationResult:
        """Ask the LLM for edge cases for ``endpoint``. Never raises for LLM failures."""
        result = GenerationResult(endpoint=endpoint.key)
        if not self._settings.llm_available:
            result.error = "LLM disabled or GROQ_API_KEY not set"
            return result

        base = baseline_case(endpoint)
        prompt = render_user_prompt(
            endpoint,
            {"path_params": base.path_params, "query": base.query, "body": base.body},
            self._settings.llm_max_cases_per_endpoint,
            self.prompt_version,
        )
        last_error = "unknown error"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            result.attempts = attempt
            try:
                content = self._complete(prompt)
                raw_cases = LLMResponse.model_validate(json.loads(content)).cases
            except httpx.TimeoutException:
                last_error = "LLM request timed out"
            except httpx.HTTPStatusError as exc:
                last_error = f"LLM HTTP {exc.response.status_code}"
            except httpx.HTTPError as exc:
                last_error = f"LLM transport error: {type(exc).__name__}"
            except (json.JSONDecodeError, ValidationError, KeyError, IndexError, TypeError):
                last_error = "LLM returned malformed JSON"
            else:
                cases, dropped = self._validate_cases(endpoint, raw_cases)
                result.cases = cases
                result.invalid_dropped = dropped
                if not cases:
                    result.error = "LLM returned no valid cases"
                return result
            logger.warning(
                "LLM attempt %d/%d failed for %s: %s",
                attempt,
                MAX_ATTEMPTS,
                endpoint.key,
                last_error,
            )
        result.error = last_error
        return result

    def _complete(self, user_prompt: str) -> str:
        assert self._settings.groq_api_key is not None
        template = get_prompt(self.prompt_version)
        payload = {
            "model": self._settings.groq_model,
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": template.system},
                {"role": "user", "content": user_prompt},
            ],
        }
        headers = {"Authorization": f"Bearer {self._settings.groq_api_key.get_secret_value()}"}
        with httpx.Client(
            base_url=self._settings.groq_base_url,
            timeout=self._settings.llm_timeout_s,
            transport=self._transport,
        ) as client:
            response = client.post("/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        return str(data["choices"][0]["message"]["content"])

    def _validate_cases(
        self, endpoint: Endpoint, raw_cases: list[dict[str, Any]]
    ) -> tuple[list[TestCase], int]:
        path_names = {f.name for f in endpoint.params_in("path")}
        query_names = {f.name for f in endpoint.params_in("query")}
        valid: list[TestCase] = []
        dropped = 0
        for raw in raw_cases[: self._settings.llm_max_cases_per_endpoint]:
            try:
                case = LLMCase.model_validate(raw)
            except ValidationError:
                dropped += 1
                continue
            # The LLM may only fill in values for the spec's own parameters - this keeps it
            # from inventing paths or smuggling absolute URLs.
            if set(case.path_params) != path_names or not set(case.query) <= query_names:
                dropped += 1
                continue
            expected: Expected = case.expected
            valid.append(
                TestCase(
                    endpoint=endpoint.key,
                    method=endpoint.method,
                    path=endpoint.path,
                    name=case.name,
                    category=case.category.lower().replace(" ", "_") or "llm",
                    path_params=case.path_params,
                    query=case.query,
                    include_body=case.include_body and endpoint.body is not None,
                    body=case.body if endpoint.body is not None else None,
                    expected=expected,
                    source="llm",
                    rationale=case.rationale,
                )
            )
        return valid, dropped
