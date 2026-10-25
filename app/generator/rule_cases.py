"""Deterministic edge-case generation from the parsed schema, without an LLM.

For each endpoint we build one valid baseline request and then mutate one value
at a time: nulls, missing required fields, wrong types, numeric boundaries
(min-1 / min / max / max+1), empty and over-long strings, and invalid enum values.
The same spec always produces the same cases (and payload hashes), which is what
makes run-over-run comparison of "newly failing" cases possible.
"""

from __future__ import annotations

import copy
from typing import Any

from app.generator.models import Expected, TestCase
from app.generator.schema_parser import Endpoint, FieldSpec

VERY_LONG_STRING_LENGTH = 10_000
HUGE_INTEGER = 2**63  # one past int64 max - a classic overflow trigger
INVALID_ENUM_VALUE = "__sentinel_invalid_enum__"

_FORMAT_EXAMPLES: dict[str, str] = {
    "email": "user@example.com",
    "date": "2024-01-01",
    "date-time": "2024-01-01T00:00:00Z",
    "uuid": "123e4567-e89b-12d3-a456-426614174000",
    "uri": "https://example.com",
    "url": "https://example.com",
    "ipv4": "192.0.2.1",
}

_WRONG_TYPE_VALUES: dict[str, Any] = {
    "string": 12345,
    "integer": "not-a-number",
    "number": "not-a-number",
    "boolean": "not-a-bool",
    "array": "not-an-array",
    "object": "not-an-object",
}


def valid_value(field: FieldSpec) -> Any:
    """Return a value that satisfies the field's declared constraints."""
    if field.enum:
        return field.enum[0]
    for candidate in (field.example, field.default):
        if candidate is not None and _satisfies(field, candidate):
            return candidate
    if field.type in {"integer", "number"}:
        return _valid_number(field)
    if field.type == "boolean":
        return True
    if field.type == "array":
        item = FieldSpec(name="item", type=field.items_type or "string")
        return [valid_value(item)]
    if field.type == "object":
        return {}
    return _valid_string(field)


def _valid_number(field: FieldSpec) -> int | float:
    is_int = field.type == "integer"
    step: int | float = 1 if is_int else 0.5
    low = high = None
    if field.minimum is not None:
        low = field.minimum + step if field.exclusive_minimum else field.minimum
    if field.maximum is not None:
        high = field.maximum - step if field.exclusive_maximum else field.maximum
    if low is not None and high is not None and low > high:
        value: float = (field.minimum or 0) / 2 + (field.maximum or 0) / 2
    elif low is not None:
        value = max(low, 1) if high is None or high >= 1 else low
    elif high is not None:
        value = min(high, 1)
    else:
        value = 1
    return int(value) if is_int else float(value)


def _valid_string(field: FieldSpec) -> str:
    base = _FORMAT_EXAMPLES.get(field.format or "", "test")
    if field.min_length is not None and len(base) < field.min_length:
        base = base + "x" * (field.min_length - len(base))
    if field.max_length is not None and len(base) > field.max_length:
        base = "x" * max(field.max_length, field.min_length or 0)
    return base


def _satisfies(field: FieldSpec, value: Any) -> bool:
    if isinstance(value, int | float) and not isinstance(value, bool):
        if field.minimum is not None and (
            value < field.minimum or (field.exclusive_minimum and value == field.minimum)
        ):
            return False
        if field.maximum is not None and (
            value > field.maximum or (field.exclusive_maximum and value == field.maximum)
        ):
            return False
    if isinstance(value, str):
        if field.min_length is not None and len(value) < field.min_length:
            return False
        if field.max_length is not None and len(value) > field.max_length:
            return False
    return True


def ok_expectation(endpoint: Endpoint) -> Expected:
    """Expected outcome for a *valid* request.

    Endpoints with path parameters address a specific resource that may not
    exist, so a 404 is acceptable there - only a 5xx is a failure.
    """
    return "not_5xx" if endpoint.params_in("path") else "2xx"


def baseline_request(endpoint: Endpoint) -> tuple[dict[str, Any], dict[str, Any], Any]:
    """Return ``(path_params, query, body)`` for a fully valid request."""
    path_params = {f.name: valid_value(f) for f in endpoint.params_in("path")}
    query = {f.name: valid_value(f) for f in endpoint.params_in("query") if f.required}
    body: Any = None
    if endpoint.body is not None:
        if endpoint.body.type == "object":
            body = {f.name: valid_value(f) for f in endpoint.body.fields}
        else:
            body = valid_value(FieldSpec(name="body", type=endpoint.body.type))
    return path_params, query, body


def baseline_case(endpoint: Endpoint) -> TestCase:
    """The single valid request for an endpoint (also used by the load test)."""
    path_params, query, body = baseline_request(endpoint)
    return TestCase(
        endpoint=endpoint.key,
        method=endpoint.method,
        path=endpoint.path,
        name="valid baseline request",
        category="valid",
        path_params=path_params,
        query=query,
        include_body=endpoint.body is not None,
        body=body,
        expected=ok_expectation(endpoint),
        rationale="All values satisfy the schema.",
    )


def generate_rule_cases(endpoint: Endpoint, max_cases: int = 60) -> list[TestCase]:
    """Generate deterministic edge cases for one endpoint (baseline first)."""
    base = baseline_case(endpoint)
    ok = ok_expectation(endpoint)
    cases: list[TestCase] = [base]

    def variant(
        name: str,
        category: str,
        expected: Expected,
        *,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: Any = ...,
        include_body: bool | None = None,
        rationale: str = "",
    ) -> None:
        cases.append(
            base.model_copy(
                update={
                    "name": name,
                    "category": category,
                    "expected": expected,
                    "path_params": base.path_params if path_params is None else path_params,
                    "query": base.query if query is None else query,
                    "body": base.body if body is ... else body,
                    "include_body": base.include_body if include_body is None else include_body,
                    "rationale": rationale,
                },
                deep=True,
            )
        )

    # --- path parameters --------------------------------------------------------
    for field in endpoint.params_in("path"):
        for name, category, value, expected in _scalar_mutations(field, ok, on_wire=True):
            if category == "empty_string":
                continue  # "/items/" is a different route (often a redirect), not an edge case
            variant(
                f"path {field.name}: {name}",
                category,
                expected,
                path_params={**base.path_params, field.name: value},
            )

    # --- query parameters -------------------------------------------------------
    for field in endpoint.params_in("query"):
        if field.required:
            query = dict(base.query)
            query.pop(field.name, None)
            variant(
                f"query {field.name}: missing required",
                "missing_required",
                "4xx",
                query=query,
            )
        for name, category, value, expected in _scalar_mutations(field, ok, on_wire=True):
            variant(
                f"query {field.name}: {name}",
                category,
                expected,
                query={**base.query, field.name: value},
            )

    # --- request body -----------------------------------------------------------
    body_spec = endpoint.body
    if body_spec is not None:
        has_required = any(f.required for f in body_spec.fields)
        if body_spec.required:
            variant("body: missing entirely", "missing_required", "4xx", include_body=False)
            variant("body: JSON null", "null", "4xx", body=None)
        if body_spec.type == "object":
            if has_required:
                variant("body: empty object", "missing_required", "4xx", body={})
            variant("body: JSON array instead of object", "wrong_type", "4xx", body=[])
            optional = [f for f in body_spec.fields if not f.required]
            if optional:
                minimal = {
                    k: v for k, v in base.body.items() if k not in {f.name for f in optional}
                }
                variant(
                    "body: only required fields",
                    "valid",
                    ok,
                    body=minimal,
                    rationale="Optional fields omitted; the API must apply defaults.",
                )

            for field in body_spec.fields:
                if field.required:
                    body = copy.deepcopy(base.body)
                    body.pop(field.name, None)
                    variant(f"{field.name}: missing required", "missing_required", "4xx", body=body)
                variant(
                    f"{field.name}: null",
                    "null",
                    ok if field.nullable else "4xx",
                    body={**base.body, field.name: None},
                    rationale="Field is nullable." if field.nullable else "Field is not nullable.",
                )
                for name, category, value, expected in _scalar_mutations(field, ok, on_wire=False):
                    variant(
                        f"{field.name}: {name}",
                        category,
                        expected,
                        body={**base.body, field.name: value},
                    )

    return cases[:max_cases]


def _scalar_mutations(
    field: FieldSpec, ok: Expected, *, on_wire: bool
) -> list[tuple[str, str, Any, Expected]]:
    """Single-value mutations for a field: ``(name, category, value, expected)``.

    ``on_wire`` is True for path/query params, which are always strings in the
    URL - a "wrong type" there only makes sense for non-string types.
    """
    out: list[tuple[str, str, Any, Expected]] = []

    wrong = _WRONG_TYPE_VALUES.get(field.type)
    if wrong is not None and not (on_wire and field.type in {"string", "array", "object"}):
        out.append((f"wrong type ({type(wrong).__name__})", "wrong_type", wrong, "4xx"))

    if field.enum:
        out.append(("invalid enum value", "invalid_enum", INVALID_ENUM_VALUE, "4xx"))
        return out

    if field.type in {"integer", "number"}:
        is_int = field.type == "integer"
        if field.minimum is not None:
            m = _num(field.minimum, is_int)
            if field.exclusive_minimum:
                above = _num(m + (1 if is_int else 0.01), is_int)
                out.append((f"at exclusive minimum ({m})", "boundary", m, "4xx"))
                out.append((f"just above minimum ({above})", "boundary", above, ok))
            else:
                below = _num(m - 1, is_int)
                out.append((f"below minimum ({below})", "boundary", below, "4xx"))
                out.append((f"at minimum ({m})", "boundary", m, ok))
        if field.maximum is not None:
            m = _num(field.maximum, is_int)
            if field.exclusive_maximum:
                below = _num(m - (1 if is_int else 0.01), is_int)
                out.append((f"at exclusive maximum ({m})", "boundary", m, "4xx"))
                out.append((f"just below maximum ({below})", "boundary", below, ok))
            else:
                above = _num(m + 1, is_int)
                out.append((f"at maximum ({m})", "boundary", m, ok))
                out.append((f"above maximum ({above})", "boundary", above, "4xx"))
        else:
            out.append(("huge value (2**63)", "boundary", HUGE_INTEGER, "not_5xx"))
        if field.minimum is None:
            out.append(("negative value (-1)", "boundary", _num(-1, is_int), "not_5xx"))

    if field.type == "string" and field.format is None:
        min_len = field.min_length or 0
        out.append(("empty string", "empty_string", "", "4xx" if min_len >= 1 else "not_5xx"))
        if min_len > 1:
            out.append(
                (f"length {min_len - 1} (below minLength)", "boundary", "x" * (min_len - 1), "4xx")
            )
        if field.max_length is not None:
            n = field.max_length
            out.append((f"length {n} (at maxLength)", "boundary", "x" * n, ok))
            out.append((f"length {n + 1} (above maxLength)", "boundary", "x" * (n + 1), "4xx"))
        long_expected: Expected = (
            "4xx"
            if field.max_length is not None and field.max_length < VERY_LONG_STRING_LENGTH
            else "not_5xx"
        )
        out.append(
            (
                f"very long string ({VERY_LONG_STRING_LENGTH} chars)",
                "long_string",
                "A" * VERY_LONG_STRING_LENGTH,
                long_expected,
            )
        )
    return out


def _num(value: float, is_int: bool) -> int | float:
    return int(value) if is_int else round(float(value), 6)
