"""The test-case model shared by the rule-based and LLM generators."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field

Expected = Literal["2xx", "4xx", "not_5xx"]
Source = Literal["rule", "llm"]


class TestCase(BaseModel):
    """One concrete request to send, plus the outcome we expect.

    ``expected`` semantics:
      * ``2xx``      - valid input, the API should accept it.
      * ``4xx``      - invalid input, the API should reject it cleanly.
      * ``not_5xx``  - outcome depends on server state (e.g. unknown resource id),
                       but it must never be a server error.
    """

    __test__ = False  # stop pytest from collecting this class

    endpoint: str
    method: str
    path: str
    name: str
    category: str
    path_params: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    include_body: bool = False
    body: Any = None
    expected: Expected
    source: Source = "rule"
    rationale: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def payload_hash(self) -> str:
        """SHA-256 of the canonical request (method, path, params, body) - used for dedupe."""
        canonical = json.dumps(
            {
                "m": self.method,
                "p": self.path,
                "pp": self.path_params,
                "q": self.query,
                "b": self.body if self.include_body else "<no-body>",
            },
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def merge_cases(*groups: Iterable[TestCase]) -> list[TestCase]:
    """Concatenate case groups, keeping the first case for each payload hash."""
    seen: set[str] = set()
    merged: list[TestCase] = []
    for group in groups:
        for case in group:
            if case.payload_hash in seen:
                continue
            seen.add(case.payload_hash)
            merged.append(case)
    return merged
