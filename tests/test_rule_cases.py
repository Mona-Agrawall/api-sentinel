from __future__ import annotations

from app.generator.models import TestCase, merge_cases
from app.generator.rule_cases import (
    HUGE_INTEGER,
    INVALID_ENUM_VALUE,
    VERY_LONG_STRING_LENGTH,
    generate_rule_cases,
    valid_value,
)
from app.generator.schema_parser import Endpoint, FieldSpec, parse_spec


def _cases(shop_yaml: str, key: str) -> dict[str, TestCase]:
    endpoint = next(e for e in parse_spec(shop_yaml).endpoints if e.key == key)
    return {c.name: c for c in generate_rule_cases(endpoint)}


def test_valid_value_respects_constraints() -> None:
    assert valid_value(FieldSpec(name="x", type="integer", minimum=5, maximum=9)) == 5
    assert valid_value(FieldSpec(name="x", type="integer", minimum=0, exclusive_minimum=True)) == 1
    assert valid_value(FieldSpec(name="x", type="number", maximum=-3)) == -3.0
    assert valid_value(FieldSpec(name="x", type="string", min_length=10)) == "testxxxxxx"
    assert valid_value(FieldSpec(name="x", type="string", max_length=2)) == "xx"
    assert valid_value(FieldSpec(name="x", type="string", enum=["b", "c"])) == "b"
    assert valid_value(FieldSpec(name="x", type="string", format="email")) == "user@example.com"
    assert valid_value(FieldSpec(name="x", type="array", items_type="integer")) == [1]
    # an example that violates the constraints is ignored
    assert valid_value(FieldSpec(name="x", type="integer", minimum=10, example=3)) == 10


def test_baseline_comes_first_and_is_valid(shop_yaml: str) -> None:
    endpoint = next(e for e in parse_spec(shop_yaml).endpoints if e.key == "POST /orders")
    cases = generate_rule_cases(endpoint)
    base = cases[0]
    assert base.category == "valid" and base.expected == "2xx"
    assert base.body["customer"] == "test" and base.body["currency"] == "EUR"
    assert 0 < base.body["amount"] <= 500


def test_boundaries_missing_null_enum_and_types(shop_yaml: str) -> None:
    cases = _cases(shop_yaml, "POST /orders")

    # exclusive minimum: 0 is invalid, 0.01 is valid; inclusive maximum: 500 ok, 501 not
    assert cases["amount: at exclusive minimum (0.0)"].body["amount"] == 0
    assert cases["amount: at exclusive minimum (0.0)"].expected == "4xx"
    assert cases["amount: just above minimum (0.01)"].expected == "2xx"
    assert cases["amount: at maximum (500.0)"].expected == "2xx"
    assert cases["amount: above maximum (501.0)"].expected == "4xx"

    assert "customer" not in cases["customer: missing required"].body
    assert cases["customer: missing required"].expected == "4xx"
    assert cases["customer: length 2 (below minLength)"].body["customer"] == "xx"
    assert cases["customer: length 21 (above maxLength)"].expected == "4xx"
    long_case = cases[f"customer: very long string ({VERY_LONG_STRING_LENGTH} chars)"]
    assert len(long_case.body["customer"]) == VERY_LONG_STRING_LENGTH
    assert cases["customer: empty string"].expected == "4xx"

    assert cases["currency: invalid enum value"].body["currency"] == INVALID_ENUM_VALUE
    assert cases["currency: null"].expected == "4xx"
    assert cases["note: null"].expected == "2xx"  # nullable
    assert cases["note: empty string"].expected == "not_5xx"  # no minLength: either is fine
    assert cases["amount: wrong type (str)"].body["amount"] == "not-a-number"

    assert cases["body: missing entirely"].include_body is False
    assert cases["body: JSON null"].include_body and cases["body: JSON null"].body is None
    assert cases["body: empty object"].body == {}
    only_required = cases["body: only required fields"].body
    assert set(only_required) == {"customer", "amount", "currency"}


def test_path_and_query_params(shop_yaml: str) -> None:
    cases = _cases(shop_yaml, "GET /orders/{order_id}")
    assert cases["valid baseline request"].expected == "not_5xx"  # resource may not exist
    assert cases["path order_id: below minimum (0)"].path_params == {"order_id": 0}
    assert cases["path order_id: below minimum (0)"].expected == "4xx"
    assert cases["path order_id: huge value (2**63)"].path_params["order_id"] == HUGE_INTEGER
    assert cases["query expand: wrong type (str)"].query == {"expand": "not-a-bool"}
    assert not any("empty string" in name for name in cases if name.startswith("path"))


def test_endpoint_without_inputs_gets_only_baseline() -> None:
    cases = generate_rule_cases(Endpoint(method="GET", path="/health"))
    assert [c.name for c in cases] == ["valid baseline request"]


def test_generation_is_deterministic_and_capped(shop_yaml: str) -> None:
    endpoint = next(e for e in parse_spec(shop_yaml).endpoints if e.key == "POST /orders")
    first = [c.payload_hash for c in generate_rule_cases(endpoint)]
    second = [c.payload_hash for c in generate_rule_cases(endpoint)]
    assert first == second
    assert len(set(first)) == len(first)
    assert len(generate_rule_cases(endpoint, max_cases=5)) == 5


def test_merge_dedupes_by_payload_hash() -> None:
    a = TestCase(endpoint="GET /x", method="GET", path="/x", name="a", category="c", expected="2xx")
    b = a.model_copy(update={"name": "same payload, other name", "source": "llm"})
    c = a.model_copy(update={"query": {"q": 1}})
    assert a.payload_hash == b.payload_hash != c.payload_hash
    assert [m.name for m in merge_cases([a], [b, c])] == ["a", "a"]
    assert merge_cases([a], [b, c])[1].query == {"q": 1}
