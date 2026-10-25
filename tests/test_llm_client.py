from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.generator.generate import generate_all
from app.generator.llm_client import LLMClient
from app.generator.schema_parser import Endpoint, parse_spec

SECRET = "gsk_super_secret_value_123"


@pytest.fixture
def llm_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"groq_api_key": SecretStr(SECRET)})


def _chat_response(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _client(settings: Settings, handler: Callable[[httpx.Request], httpx.Response]) -> LLMClient:
    return LLMClient(settings, transport=httpx.MockTransport(handler))


def _order_endpoint(shop_yaml: str) -> Endpoint:
    return next(e for e in parse_spec(shop_yaml).endpoints if e.key == "GET /orders/{order_id}")


GOOD_CASE: dict[str, Any] = {
    "name": "unicode in expand",
    "category": "unicode",
    "path_params": {"order_id": 7},
    "query": {"expand": "ü"},
    "include_body": False,
    "body": None,
    "expected": "4xx",
    "rationale": "non-boolean",
}


def test_valid_response_is_converted_to_cases(llm_settings: Settings, shop_yaml: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _chat_response(json.dumps({"cases": [GOOD_CASE]}))

    result = _client(llm_settings, handler).generate_cases(_order_endpoint(shop_yaml))
    assert result.error is None and result.attempts == 1
    (case,) = result.cases
    assert case.source == "llm" and case.path_params == {"order_id": 7}
    assert case.endpoint == "GET /orders/{order_id}" and case.method == "GET"

    body = json.loads(seen[0].content)
    assert body["response_format"] == {"type": "json_object"}
    assert body["model"] == llm_settings.groq_model
    assert seen[0].headers["Authorization"] == f"Bearer {SECRET}"


def test_invalid_cases_are_dropped_and_counted(llm_settings: Settings, shop_yaml: str) -> None:
    bad = [
        {**GOOD_CASE, "expected": "maybe"},  # not an allowed expectation
        {**GOOD_CASE, "path_params": {}},  # missing path param
        {**GOOD_CASE, "path_params": {"order_id": 1, "host": "evil"}},  # invented param
        {**GOOD_CASE, "query": {"unknown": 1}},  # unknown query param
        {"name": ""},  # garbage
    ]
    payload = {"cases": [GOOD_CASE, *bad]}
    client = _client(llm_settings, lambda _r: _chat_response(json.dumps(payload)))
    result = client.generate_cases(_order_endpoint(shop_yaml))
    assert len(result.cases) == 1
    assert result.invalid_dropped == len(bad)


def test_retries_once_after_malformed_json(llm_settings: Settings, shop_yaml: str) -> None:
    responses = iter(
        [_chat_response("not json {"), _chat_response(json.dumps({"cases": [GOOD_CASE]}))]
    )
    result = _client(llm_settings, lambda _r: next(responses)).generate_cases(
        _order_endpoint(shop_yaml)
    )
    assert result.attempts == 2 and result.error is None and len(result.cases) == 1


@pytest.mark.parametrize(
    ("handler", "error"),
    [
        (lambda _r: httpx.Response(500), "LLM HTTP 500"),
        (lambda _r: httpx.Response(429), "LLM HTTP 429"),
        (lambda _r: _chat_response('{"nope": []}'), "malformed JSON"),
        (lambda _r: _chat_response('{"cases": "x"}'), "malformed JSON"),
        (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)), "timed out"),
        (lambda r: (_ for _ in ()).throw(httpx.ConnectError("down", request=r)), "ConnectError"),
    ],
)
def test_failures_never_raise_and_try_twice(
    llm_settings: Settings,
    shop_yaml: str,
    handler: Callable[[httpx.Request], httpx.Response],
    error: str,
) -> None:
    calls = 0

    def counting(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return handler(request)

    result = _client(llm_settings, counting).generate_cases(_order_endpoint(shop_yaml))
    assert calls == 2 and result.attempts == 2
    assert result.cases == [] and result.error is not None and error in result.error


def test_api_key_is_never_logged(
    llm_settings: Settings, shop_yaml: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client = _client(llm_settings, lambda _r: httpx.Response(401, text=f"bad key {SECRET}"))
    result = client.generate_cases(_order_endpoint(shop_yaml))
    assert result.error == "LLM HTTP 401"
    assert SECRET not in caplog.text
    assert SECRET not in repr(llm_settings) and SECRET not in str(llm_settings.model_dump())


def test_no_key_means_rule_fallback_recorded(settings: Settings, shop_yaml: str) -> None:
    def explode(_r: httpx.Request) -> httpx.Response:
        raise AssertionError("LLM must not be called without a key")

    spec = parse_spec(shop_yaml)
    summary = generate_all(spec, settings, _client(settings, explode))
    assert summary.llm_fallback and not summary.llm_attempted
    assert summary.llm_errors == {"*": "GROQ_API_KEY not set"}
    assert summary.llm_cases == 0 and summary.rule_cases == len(summary.cases) > 0


def test_llm_failure_per_endpoint_falls_back_and_merges(
    llm_settings: Settings, shop_yaml: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][1]["content"]
        if '"path": "/orders/{order_id}"' in prompt and '"method": "GET"' in prompt:
            return _chat_response(json.dumps({"cases": [GOOD_CASE]}))
        return httpx.Response(503)

    summary = generate_all(parse_spec(shop_yaml), llm_settings, _client(llm_settings, handler))
    assert summary.llm_attempted and summary.llm_fallback
    assert set(summary.llm_errors) == {"POST /orders", "DELETE /orders/{order_id}"}
    assert summary.llm_cases == 1
    assert summary.prompt_version == "v1"
    hashes = [c.payload_hash for c in summary.cases]
    assert len(hashes) == len(set(hashes))
