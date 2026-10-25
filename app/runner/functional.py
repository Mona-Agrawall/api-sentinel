"""Send each test case to the target API and judge the response.

A case passes when the status code matches its expectation:
  * ``2xx``     -> 200-299
  * ``4xx``     -> 400-499
  * ``not_5xx`` -> anything below 500
A 5xx response or a transport error is always a failure.

Every request goes through :class:`OriginGuard`: anything not addressed to the run's
``base_url`` origin is refused before it leaves the process.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import quote

import httpx

from app.db.models import CaseResult
from app.generator.models import TestCase

RESPONSE_EXCERPT_CHARS = 300
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1


class ForeignHostError(RuntimeError):
    """Raised when a request would leave the allowed origin."""


class OriginGuard:
    """httpx request hook that only lets requests to one origin through."""

    def __init__(self, base_url: str) -> None:
        url = httpx.URL(base_url)
        self.origin = (url.scheme, url.host, url.port)

    def check(self, url: httpx.URL) -> None:
        """Raise :class:`ForeignHostError` if ``url`` is not on the allowed origin."""
        if (url.scheme, url.host, url.port) != self.origin:
            raise ForeignHostError(f"refusing to send test traffic to {url.scheme}://{url.host}")

    async def __call__(self, request: httpx.Request) -> None:
        self.check(request.url)


def build_path(case: TestCase) -> str:
    """Fill path-template placeholders with URL-encoded values."""
    path = case.path
    for name, value in case.path_params.items():
        path = path.replace("{" + name + "}", quote(str(value), safe=""))
    return path


def judge(expected: str, status_code: int) -> tuple[bool, str | None]:
    """Return ``(passed, reason)`` for a status code against an expectation."""
    if status_code >= 500:
        return False, f"server error {status_code}"
    if expected == "2xx" and not 200 <= status_code < 300:
        return False, f"expected 2xx, got {status_code}"
    if expected == "4xx" and not 400 <= status_code < 500:
        return False, f"expected 4xx (invalid input should be rejected), got {status_code}"
    return True, None


async def run_case(client: httpx.AsyncClient, case: TestCase) -> CaseResult:
    """Send one case and return its result. Never raises for request failures."""
    content: bytes | None = None
    headers: dict[str, str] = {}
    if case.include_body:
        content = json.dumps(case.body, default=str).encode("utf-8")
        headers["Content-Type"] = "application/json"

    base = CaseResult(
        endpoint=case.endpoint,
        method=case.method,
        path=case.path,
        case_name=case.name,
        category=case.category,
        source=case.source,
        expected=case.expected,
        payload_hash=case.payload_hash,
        request={
            "path_params": _bson_safe(case.path_params),
            "query": _bson_safe(case.query),
            "include_body": case.include_body,
            # Stored as a string: generated bodies may contain keys such as "$where"
            # that MongoDB does not accept as field names.
            "body_json": content.decode("utf-8") if content is not None else None,
        },
        passed=False,
    )
    start = time.perf_counter()
    try:
        response = await client.request(
            case.method,
            build_path(case),
            params={k: _query_value(v) for k, v in case.query.items()},
            content=content,
            headers=headers,
        )
    except ForeignHostError as exc:
        base.failure_reason = str(exc)
        return base
    except httpx.HTTPError as exc:
        base.latency_ms = round((time.perf_counter() - start) * 1000, 2)
        base.failure_reason = f"transport error: {type(exc).__name__}"
        return base

    base.latency_ms = round((time.perf_counter() - start) * 1000, 2)
    base.status_code = response.status_code
    base.passed, base.failure_reason = judge(case.expected, response.status_code)
    base.response_excerpt = response.text[:RESPONSE_EXCERPT_CHARS]
    return base


def _bson_safe(value: Any) -> Any:
    """Make a JSON value storable in MongoDB: ints beyond int64 become strings."""
    if isinstance(value, dict):
        return {str(k): _bson_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_bson_safe(v) for v in value]
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and not INT64_MIN <= value <= INT64_MAX
    ):
        return str(value)
    return value


def _query_value(value: object) -> str | list[str]:
    if isinstance(value, list):
        return [_query_value(v) for v in value]  # type: ignore[misc]
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


async def run_functional(
    cases: list[TestCase],
    base_url: str,
    *,
    timeout_s: float = 10.0,
    concurrency: int = 10,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[CaseResult]:
    """Run all cases against ``base_url`` with bounded concurrency (order preserved)."""
    guard = OriginGuard(base_url)
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout_s,
        transport=transport,
        follow_redirects=False,
        event_hooks={"request": [guard]},
    ) as client:

        async def bounded(case: TestCase) -> CaseResult:
            async with semaphore:
                return await run_case(client, case)

        return list(await asyncio.gather(*(bounded(c) for c in cases)))
