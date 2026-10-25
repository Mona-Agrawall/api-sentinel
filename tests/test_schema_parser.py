from __future__ import annotations

import json

import pytest

from app.generator.schema_parser import SpecError, load_document, parse_spec
from target_api.app import app as target_app


def test_parses_openapi30_yaml_with_refs_and_allof(shop_yaml: str) -> None:
    spec = parse_spec(shop_yaml)
    assert (spec.title, spec.version, spec.openapi) == ("Shop", "2.1", "3.0.3")
    assert sorted(e.key for e in spec.endpoints) == [
        "DELETE /orders/{order_id}",
        "GET /orders/{order_id}",
        "POST /orders",
    ]

    create = next(e for e in spec.endpoints if e.key == "POST /orders")
    assert create.body is not None and create.body.required
    fields = {f.name: f for f in create.body.fields}
    assert set(fields) == {"customer", "amount", "currency", "note", "items"}
    assert fields["customer"].required and fields["customer"].min_length == 3
    assert fields["amount"].minimum == 0 and fields["amount"].exclusive_minimum
    assert fields["amount"].maximum == 500 and not fields["amount"].exclusive_maximum
    assert fields["currency"].enum == ["EUR", "USD"]
    assert fields["note"].nullable and not fields["note"].required
    assert fields["items"].type == "array" and fields["items"].items_type == "integer"


def test_path_level_parameters_are_inherited(shop_yaml: str) -> None:
    spec = parse_spec(shop_yaml)
    get = next(e for e in spec.endpoints if e.key == "GET /orders/{order_id}")
    (order_id,) = get.params_in("path")
    assert order_id.name == "order_id" and order_id.type == "integer"
    assert order_id.required and order_id.minimum == 1
    assert [f.name for f in get.params_in("query")] == ["expand"]
    delete = next(e for e in spec.endpoints if e.method == "DELETE")
    assert [f.name for f in delete.params_in("path")] == ["order_id"]


def test_parses_openapi31_json_from_fastapi() -> None:
    spec = parse_spec(json.dumps(target_app.openapi()))
    assert spec.openapi.startswith("3.1")
    create = next(e for e in spec.endpoints if e.key == "POST /items")
    assert create.body is not None
    fields = {f.name: f for f in create.body.fields}
    # 3.1 numeric exclusiveMinimum, and anyOf [string, null] -> nullable
    assert fields["price"].minimum == 0 and fields["price"].exclusive_minimum
    assert fields["description"].nullable and fields["description"].type == "string"
    assert fields["description"].max_length == 500
    # $ref'd enum
    assert fields["category"].enum == ["books", "electronics", "toys"]
    list_items = next(e for e in spec.endpoints if e.key == "GET /items")
    category = next(f for f in list_items.params_in("query") if f.name == "category")
    assert category.nullable and category.enum == ["books", "electronics", "toys"]


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        ({"swagger": "2.0", "paths": {}}, "only OpenAPI 3.x"),
        ({"openapi": "3.0.0"}, "no 'paths'"),
        ({"openapi": "3.0.0", "paths": {"items": {}}}, "must start with '/'"),
    ],
)
def test_rejects_unsupported_documents(doc: dict[str, object], message: str) -> None:
    with pytest.raises(SpecError, match=message):
        parse_spec(doc)


def test_rejects_circular_and_remote_refs() -> None:
    circular = {
        "openapi": "3.0.0",
        "paths": {"/a": {"$ref": "#/components/x"}},
        "components": {"x": {"$ref": "#/components/x"}},
    }
    with pytest.raises(SpecError, match="circular"):
        parse_spec(circular)
    remote = {"openapi": "3.0.0", "paths": {"/a": {"$ref": "https://evil.example/x.json"}}}
    with pytest.raises(SpecError, match="only local"):
        parse_spec(remote)


def test_load_document_rejects_garbage() -> None:
    with pytest.raises(SpecError):
        load_document("- just\n- a list\n")
    with pytest.raises(SpecError):
        load_document("{not: valid: yaml: [")
