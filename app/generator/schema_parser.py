"""Parse OpenAPI 3.x documents (JSON or YAML) into a flat list of endpoint descriptions.

Only the parts of the spec needed for edge-case generation are kept: parameters,
the JSON request body's top-level fields, and their constraints (type, required,
enum, numeric bounds, string lengths, nullability).
"""

from __future__ import annotations

import json
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
MAX_REF_DEPTH = 20


class SpecError(ValueError):
    """Raised when a document is not a usable OpenAPI 3.x spec."""


class FieldSpec(BaseModel):
    """Constraints for one value (a parameter or a top-level body field)."""

    name: str
    type: str = "string"  # string | integer | number | boolean | array | object | any
    required: bool = False
    nullable: bool = False
    enum: list[Any] | None = None
    minimum: float | None = None
    maximum: float | None = None
    exclusive_minimum: bool = False
    exclusive_maximum: bool = False
    min_length: int | None = None
    max_length: int | None = None
    format: str | None = None
    items_type: str | None = None
    default: Any = None
    example: Any = None
    description: str | None = None


class ParamSpec(BaseModel):
    """A path, query or header parameter."""

    location: Literal["path", "query", "header", "cookie"]
    field: FieldSpec


class BodySpec(BaseModel):
    """A JSON request body."""

    required: bool = False
    content_type: str = "application/json"
    type: str = "object"
    fields: list[FieldSpec] = Field(default_factory=list)


class Endpoint(BaseModel):
    """One operation (method + path) of the API."""

    method: str
    path: str
    operation_id: str | None = None
    summary: str | None = None
    params: list[ParamSpec] = Field(default_factory=list)
    body: BodySpec | None = None

    @property
    def key(self) -> str:
        """Stable identifier such as ``"POST /items"``."""
        return f"{self.method} {self.path}"

    def params_in(self, location: str) -> list[FieldSpec]:
        """Return the fields of all parameters in ``location``."""
        return [p.field for p in self.params if p.location == location]


class ApiSpec(BaseModel):
    """The parsed spec."""

    title: str
    version: str
    openapi: str
    endpoints: list[Endpoint]


def load_document(text: str | bytes) -> dict[str, Any]:
    """Load a JSON or YAML document into a dict."""
    if isinstance(text, bytes):
        text = text.decode("utf-8")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise SpecError(f"spec is neither valid JSON nor YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise SpecError("spec must be a JSON/YAML object")
    return doc


def parse_spec(source: str | bytes | dict[str, Any]) -> ApiSpec:
    """Parse an OpenAPI 3.x document (text or already-loaded dict) into an :class:`ApiSpec`."""
    doc = source if isinstance(source, dict) else load_document(source)
    version = str(doc.get("openapi", ""))
    if not version.startswith("3."):
        raise SpecError(f"only OpenAPI 3.x is supported (got openapi={version or 'missing'!r})")
    paths = doc.get("paths")
    if not isinstance(paths, dict):
        raise SpecError("spec has no 'paths' object")

    resolver = _Resolver(doc)
    endpoints: list[Endpoint] = []
    for path, path_item in paths.items():
        if not isinstance(path, str) or not path.startswith("/"):
            raise SpecError(f"invalid path {path!r}: paths must start with '/'")
        path_item = resolver.resolve(path_item)
        if not isinstance(path_item, dict):
            continue
        shared_params = path_item.get("parameters", [])
        for method in HTTP_METHODS:
            op = path_item.get(method)
            if not isinstance(op, dict):
                continue
            endpoints.append(_parse_operation(resolver, method.upper(), path, op, shared_params))

    info = doc.get("info") or {}
    return ApiSpec(
        title=str(info.get("title", "untitled")),
        version=str(info.get("version", "")),
        openapi=version,
        endpoints=endpoints,
    )


def _parse_operation(
    resolver: _Resolver,
    method: str,
    path: str,
    op: dict[str, Any],
    shared_params: list[Any],
) -> Endpoint:
    params: dict[tuple[str, str], ParamSpec] = {}
    # Operation-level parameters override path-level ones with the same (in, name).
    for raw in [*shared_params, *op.get("parameters", [])]:
        param = resolver.resolve(raw)
        if not isinstance(param, dict) or "name" not in param or "in" not in param:
            continue
        location = param["in"]
        if location not in {"path", "query", "header", "cookie"}:
            continue
        schema = resolver.resolve(param.get("schema") or {})
        field = _field_from_schema(
            resolver,
            str(param["name"]),
            schema,
            required=bool(param.get("required", location == "path")),
        )
        if field.description is None and param.get("description"):
            field.description = str(param["description"])
        params[(location, field.name)] = ParamSpec(location=location, field=field)

    return Endpoint(
        method=method,
        path=path,
        operation_id=op.get("operationId"),
        summary=op.get("summary") or op.get("description"),
        params=list(params.values()),
        body=_parse_body(resolver, op.get("requestBody")),
    )


def _parse_body(resolver: _Resolver, raw: Any) -> BodySpec | None:
    body = resolver.resolve(raw)
    if not isinstance(body, dict):
        return None
    content = body.get("content") or {}
    content_type = next((ct for ct in content if "json" in ct), None)
    if content_type is None:
        return None
    schema = _flatten(resolver, (content[content_type] or {}).get("schema") or {})
    spec = BodySpec(
        required=bool(body.get("required", False)),
        content_type=content_type,
        type=_schema_type(schema),
    )
    if spec.type == "object":
        required = set(schema.get("required", []))
        for name, prop in (schema.get("properties") or {}).items():
            spec.fields.append(
                _field_from_schema(resolver, str(name), prop, required=name in required)
            )
    return spec


def _field_from_schema(
    resolver: _Resolver, name: str, raw_schema: Any, *, required: bool
) -> FieldSpec:
    schema = _flatten(resolver, raw_schema)
    nullable = bool(schema.get("nullable", False)) or schema.get("_nullable", False)
    field = FieldSpec(
        name=name,
        type=_schema_type(schema),
        required=required,
        nullable=bool(nullable),
        enum=schema.get("enum"),
        min_length=schema.get("minLength"),
        max_length=schema.get("maxLength"),
        format=schema.get("format"),
        default=schema.get("default"),
        example=schema.get("example", (schema.get("examples") or [None])[0]),
        description=schema.get("description"),
    )
    if field.type == "array":
        field.items_type = _schema_type(_flatten(resolver, schema.get("items") or {}))

    field.minimum = schema.get("minimum")
    field.maximum = schema.get("maximum")
    # OpenAPI 3.0: exclusiveMinimum is a boolean modifier; 3.1: it is the bound itself.
    ex_min, ex_max = schema.get("exclusiveMinimum"), schema.get("exclusiveMaximum")
    if isinstance(ex_min, bool):
        field.exclusive_minimum = ex_min
    elif isinstance(ex_min, int | float):
        field.minimum, field.exclusive_minimum = ex_min, True
    if isinstance(ex_max, bool):
        field.exclusive_maximum = ex_max
    elif isinstance(ex_max, int | float):
        field.maximum, field.exclusive_maximum = ex_max, True
    return field


def _flatten(resolver: _Resolver, raw: Any) -> dict[str, Any]:
    """Resolve refs and collapse ``allOf`` / nullable ``anyOf``/``oneOf`` into one schema."""
    resolved = resolver.resolve(raw)
    if not isinstance(resolved, dict):
        return {}
    schema: dict[str, Any] = dict(resolved)

    if "allOf" in schema:
        merged: dict[str, Any] = {}
        required: list[str] = []
        props: dict[str, Any] = {}
        for part in schema.pop("allOf"):
            sub = _flatten(resolver, part)
            props.update(sub.pop("properties", {}) or {})
            required.extend(sub.pop("required", []) or [])
            merged.update(sub)
        merged.update(schema)
        if props:
            merged["properties"] = {**props, **(schema.get("properties") or {})}
        if required:
            merged["required"] = sorted(set(required) | set(schema.get("required", [])))
        schema = merged

    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [_flatten(resolver, o) for o in schema[key]]
            non_null = [o for o in options if o.get("type") != "null"]
            if len(non_null) < len(options):
                schema["_nullable"] = True
            if len(non_null) == 1:
                rest = {k: v for k, v in schema.items() if k != key}
                schema = {**non_null[0], **rest}

    type_value = schema.get("type")
    if isinstance(type_value, list):  # OpenAPI 3.1: type: ["string", "null"]
        non_null_types = [t for t in type_value if t != "null"]
        if len(non_null_types) < len(type_value):
            schema["_nullable"] = True
        schema["type"] = non_null_types[0] if len(non_null_types) == 1 else "any"
    return schema


def _schema_type(schema: dict[str, Any]) -> str:
    type_value = schema.get("type")
    if isinstance(type_value, str):
        return type_value
    if "properties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    if schema.get("enum"):
        return _python_type_name(schema["enum"][0])
    return "any"


def _python_type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


class _Resolver:
    """Resolves local JSON references (``#/components/...``)."""

    def __init__(self, doc: dict[str, Any]) -> None:
        self._doc = doc

    def resolve(self, node: Any) -> Any:
        seen: set[str] = set()
        depth = 0
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/"):
                raise SpecError(f"only local $refs are supported (got {ref!r})")
            if ref in seen or depth > MAX_REF_DEPTH:
                raise SpecError(f"circular $ref detected at {ref}")
            seen.add(ref)
            depth += 1
            node = self._lookup(ref)
        return node

    def _lookup(self, ref: str) -> Any:
        node: Any = self._doc
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                raise SpecError(f"unresolvable $ref {ref}")
            node = node[part]
        return node
