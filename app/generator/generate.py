"""Combine rule-based and LLM-generated cases for a whole spec."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.config import Settings
from app.generator.llm_client import LLMClient
from app.generator.models import TestCase, merge_cases
from app.generator.rule_cases import generate_rule_cases
from app.generator.schema_parser import ApiSpec


class GenerationSummary(BaseModel):
    """All cases for a run, plus a record of how the LLM behaved."""

    cases: list[TestCase]
    rule_cases: int = 0
    llm_cases: int = 0
    llm_invalid_dropped: int = 0
    llm_attempted: bool = False
    llm_fallback: bool = False
    llm_errors: dict[str, str] = Field(default_factory=dict)
    model: str | None = None
    prompt_version: str | None = None


def generate_all(spec: ApiSpec, settings: Settings, llm: LLMClient | None) -> GenerationSummary:
    """Generate cases for every endpoint.

    Rule cases are always produced; LLM cases are added when the LLM is available.
    If the LLM fails for an endpoint, that endpoint silently uses rule cases only and
    the failure is recorded in ``llm_errors`` (``llm_fallback`` becomes True).
    """
    use_llm = llm is not None and settings.llm_available
    summary = GenerationSummary(
        cases=[],
        llm_attempted=use_llm,
        llm_fallback=not use_llm,
        model=llm.model if use_llm and llm else None,
        prompt_version=llm.prompt_version if use_llm and llm else None,
    )
    if not use_llm:
        summary.llm_errors["*"] = (
            "LLM disabled" if not settings.llm_enabled else "GROQ_API_KEY not set"
        )

    for endpoint in spec.endpoints:
        rule = generate_rule_cases(endpoint, settings.max_cases_per_endpoint)
        llm_cases: list[TestCase] = []
        if use_llm and llm is not None:
            result = llm.generate_cases(endpoint)
            llm_cases = result.cases
            summary.llm_invalid_dropped += result.invalid_dropped
            if result.error:
                summary.llm_errors[endpoint.key] = result.error
                summary.llm_fallback = True
        merged = merge_cases(rule, llm_cases)
        summary.rule_cases += sum(1 for c in merged if c.source == "rule")
        summary.llm_cases += sum(1 for c in merged if c.source == "llm")
        summary.cases.extend(merged)
    return summary
