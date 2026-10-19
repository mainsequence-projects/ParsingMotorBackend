"""Validate portable Pydantic JSON Schemas without remote reference resolution."""

from __future__ import annotations

import hashlib
import json

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry


def schema_hash(canonical_schema: dict, output_schema: dict) -> str:
    # Exact encoding used by backoffice.schemas.catalog.document_schema.
    encoded = json.dumps(
        {"canonical_schema": canonical_schema, "draft_schema": output_schema},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validator(schema: dict) -> Draft202012Validator:
    # An empty registry has no network retrieval callback.
    return Draft202012Validator(schema, format_checker=FormatChecker(), registry=Registry())


def validate_schema(schema: dict, document_type: str) -> None:
    """Accept bounded, acyclic Draft 2020-12 schemas with local JSON Pointer refs."""
    if len(json.dumps(schema, ensure_ascii=False, allow_nan=False).encode()) > 262144:
        raise ValueError("schema exceeds 256 KiB")
    if (
        schema.get("$schema", "https://json-schema.org/draft/2020-12/schema")
        != "https://json-schema.org/draft/2020-12/schema"
    ):
        raise ValueError("only JSON Schema Draft 2020-12 is supported")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ValueError("invalid JSON Schema") from exc
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        raise ValueError("schema root must be an object with additionalProperties=false")
    discriminator = schema.get("properties", {}).get("document_type", {})
    if not isinstance(discriminator, dict) or discriminator.get("const") != document_type:
        raise ValueError("schema document_type.const must match document_type")

    active, visited = set(), set()
    maps = ("$defs", "definitions", "properties", "patternProperties", "dependentSchemas")
    singles = (
        "additionalProperties",
        "unevaluatedProperties",
        "propertyNames",
        "items",
        "contains",
        "unevaluatedItems",
        "not",
        "if",
        "then",
        "else",
        "contentSchema",
    )
    arrays = ("allOf", "anyOf", "oneOf", "prefixItems")

    def visit(node, depth=0):
        if not isinstance(node, dict):
            return
        ident = id(node)
        if ident in active:
            raise ValueError("recursive schemas are not supported")
        if ident in visited:
            return
        if depth > 64 or len(visited) >= 4096:
            raise ValueError("schema complexity limit exceeded")
        active.add(ident)
        visited.add(ident)
        if any(k in node for k in ("$id", "$dynamicRef", "$dynamicAnchor", "$anchor")):
            raise ValueError("schema identifiers and dynamic references are not supported")
        if "format" in node and node["format"] not in FormatChecker.checkers:
            raise ValueError("unsupported JSON Schema format")
        if "$ref" in node:
            ref = node["$ref"]
            if not ref.startswith("#/"):
                raise ValueError("only local JSON Pointer schema references are supported")
            target = schema
            try:
                for part in ref[2:].split("/"):
                    key = part.replace("~1", "/").replace("~0", "~")
                    target = target[int(key)] if isinstance(target, list) else target[key]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ValueError("unresolved schema reference") from exc
            if not isinstance(target, (dict, bool)):
                raise ValueError("reference must target a schema")
            visit(target, depth + 1)
        for key in maps:
            for child in node.get(key, {}).values():
                visit(child, depth + 1)
        for key in singles:
            visit(node.get(key), depth + 1)
        for key in arrays:
            for child in node.get(key, []):
                visit(child, depth + 1)
        active.remove(ident)

    visit(schema)
