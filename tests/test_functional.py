from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.generator.models import TestCase
from app.runner.functional import OriginGuard, build_path, judge, run_functional
from app.runner.load import LoadTestError, run_load_test, select_targets


def case(**kwargs: object) -> TestCase:
    defaults: dict[str, object] = {
        "endpoint": "POST /items/{id}",
        "method": "POST",
        "path": "/items/{id}",
        "name": "n",
        "category": "c",
        "path_params": {"id": 1},
        "expected": "4xx",
    }
    return TestCase.model_validate({**defaults, **kwargs})


@pytest.mark.parametrize(
    ("expected", "status", "passed"),
    [
        ("2xx", 200, True),
        ("2xx", 201, True),
        ("2xx", 422, False),
        ("4xx", 422, True),
        ("4xx", 200, False),
        ("4xx", 500, False),
        ("not_5xx", 404, True),
        ("not_5xx", 307, True),
        ("not_5xx", 503, False),
    ],
)
def test_judge(expected: str, status: int, passed: bool) -> None:
    assert judge(expected, status)[0] is passed


def test_build_path_encodes_values() -> None:
    assert (
        build_path(case(path_params={"id": "../../etc/passwd"})) == "/items/..%2F..%2Fetc%2Fpasswd"
    )
    assert build_path(case(path_params={"id": "http://evil.com"})) == "/items/http%3A%2F%2Fevil.com"


def test_run_functional_sends_requests_and_records_results() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.content == b"null":
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(422, json={"detail": "bad"})

    cases = [
        case(name="null body", include_body=True, body=None, expected="4xx"),
        case(name="no body", include_body=False, query={"flag": True, "n": 3}, expected="4xx"),
    ]
    results = asyncio.run(
        run_functional(cases, "http://api.test", transport=httpx.MockTransport(handler))
    )
    null_result, no_body = results
    assert null_result.status_code == 500 and not null_result.passed
    assert null_result.failure_reason == "server error 500"
    assert null_result.request["body_json"] == "null"
    assert no_body.passed and no_body.latency_ms is not None
    assert seen[0].headers["content-type"] == "application/json"
    assert seen[1].content == b"" and seen[1].url.params["flag"] == "true"
    assert str(seen[1].url) == "http://api.test/items/1?flag=true&n=3"


def test_transport_errors_are_failures_not_crashes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    (res,) = asyncio.run(
        run_functional([case()], "http://api.test", transport=httpx.MockTransport(handler))
    )
    assert not res.passed and res.status_code is None
    assert res.failure_reason == "transport error: ConnectError"


def test_origin_guard_blocks_other_hosts() -> None:
    guard = OriginGuard("http://localhost:9000")
    guard.check(httpx.URL("http://localhost:9000/items"))
    for url in ("http://evil.com/x", "http://localhost:9001/x", "https://localhost:9000/x"):
        with pytest.raises(RuntimeError, match="refusing"):
            guard.check(httpx.URL(url))


def test_absolute_url_in_path_never_leaves_base_origin() -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(200)

    sneaky = case(path="http://evil.com/steal", path_params={}, expected="2xx")
    (res,) = asyncio.run(
        run_functional([sneaky], "http://localhost:9000", transport=httpx.MockTransport(handler))
    )
    assert sent == [] and not res.passed and "refusing" in (res.failure_reason or "")


def test_load_targets_only_safe_methods_by_default() -> None:
    get = case(method="GET", endpoint="GET /items/{id}", expected="not_5xx")
    post = case(include_body=True, body={"a": 1})
    assert [t["method"] for t in select_targets([get, post])] == ["GET"]
    targets = select_targets([get, post], include_unsafe=True)
    assert targets[0]["path"] == "/items/1"
    assert json.dumps(targets[1]["body"]) == '{"a": 1}'


def test_load_refuses_non_relative_targets() -> None:
    target = {"endpoint": "x", "method": "GET", "path": "//evil.com/x", "query": {}}
    with pytest.raises(LoadTestError, match="non-relative"):
        run_load_test("http://localhost:9000", [{**target, "include_body": False}], 1, 1)
