"""Versioned prompt templates for LLM test generation.

Each run records the prompt version it used, so a change in generated cases can be
traced back to a prompt change. Add a new version instead of editing an old one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.generator.schema_parser import Endpoint


@dataclass(frozen=True)
class PromptTemplate:
    """A system prompt plus a user-prompt template with ``{placeholders}``."""

    version: str
    system: str
    user: str


_V1 = PromptTemplate(
    version="v1",
    system=(
        "You are a meticulous API test engineer. You design edge-case HTTP requests that "
        "expose input-validation bugs and server crashes. You reply with a single JSON "
        "object and nothing else."
    ),
    user="""Design up to {max_cases} edge-case test requests for this API endpoint.

Endpoint (parsed from the OpenAPI spec):
{endpoint_json}

A valid baseline request for this endpoint is:
{baseline_json}

Focus on cases a rule-based generator would miss: semantically odd but schema-valid
values, unicode and special characters, injection-style strings, combinations of
boundary values, numeric precision, whitespace-only strings, duplicate list items.

Rules:
- Only use the path, method and parameter names shown above. Never use absolute URLs.
- "path_params" MUST contain a value for every path parameter.
- "expected" is "2xx" if the API should accept the request, "4xx" if it should reject
  it as invalid input, or "not_5xx" if either is reasonable (but it must not crash).
- "include_body" is false only when the test omits the request body entirely.

Respond with JSON exactly in this shape:
{{"cases": [{{"name": "short description", "category": "one word e.g. unicode",
  "path_params": {{}}, "query": {{}}, "include_body": true, "body": {{}},
  "expected": "4xx", "rationale": "why this might break the API"}}]}}""",
)

PROMPTS: dict[str, PromptTemplate] = {_V1.version: _V1}
CURRENT_VERSION = "v1"


def get_prompt(version: str = CURRENT_VERSION) -> PromptTemplate:
    """Return a prompt template by version."""
    try:
        return PROMPTS[version]
    except KeyError as exc:
        raise ValueError(f"unknown prompt version {version!r}") from exc


def render_user_prompt(
    endpoint: Endpoint,
    baseline: dict[str, object],
    max_cases: int,
    version: str = CURRENT_VERSION,
) -> str:
    """Render the user prompt for one endpoint."""
    template = get_prompt(version)
    endpoint_json = endpoint.model_dump_json(exclude_none=True, indent=1)
    return template.user.format(
        max_cases=max_cases,
        endpoint_json=endpoint_json,
        baseline_json=json.dumps(baseline, default=str),
    )
