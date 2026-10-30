from __future__ import annotations

import json
import math

from jsonschema import Draft202012Validator, SchemaError, ValidationError, validators


_KEYS = {"type", "properties", "required", "additionalProperties", "items", "enum", "const",
         "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems",
         "description", "title", "$schema", "default", "anyOf", "oneOf", "allOf"}


def _const(validator, expected, value, schema):
    if type(value) is not type(expected) or value != expected:
        yield ValidationError("Strict const mismatch")


def _enum(validator, expected, value, schema):
    if not any(type(value) is type(item) and value == item for item in expected):
        yield ValidationError("Strict enum mismatch")


_Validator = validators.extend(
    Draft202012Validator, {"const": _const, "enum": _enum},
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine_many({
        "integer": lambda _, value: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda _, value: isinstance(value, (int, float))
        and not isinstance(value, bool) and math.isfinite(value),
    }),
)


def _check_subset(schema):

    if not isinstance(schema, dict) or set(schema) - _KEYS:
        raise ValueError("UNSUPPORTED_AGENT_SCHEMA")
    if schema.get("enum") == []:
        raise ValueError("INVALID_AGENT_SCHEMA")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and type(schema[key]) is not int:
            raise ValueError("INVALID_AGENT_SCHEMA")

    checked = {key: value for key, value in schema.items() if key != "$schema"}
    if "properties" in schema:
        checked["properties"] = {key: _check_subset(child) for key, child in schema["properties"].items()}
    if "items" in schema:
        checked["items"] = _check_subset(schema["items"])
    if isinstance(schema.get("additionalProperties"), dict):
        checked["additionalProperties"] = _check_subset(schema["additionalProperties"])
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            checked[key] = [_check_subset(child) for child in schema[key]]
    return checked


def check_schema(schema):

    if not isinstance(schema, dict):
        raise ValueError("UNSUPPORTED_AGENT_SCHEMA")
    try:
        Draft202012Validator.check_schema(schema)
        json.dumps(schema, allow_nan=False)
    except (SchemaError, TypeError, ValueError):
        raise ValueError("INVALID_AGENT_SCHEMA") from None
    return _check_subset(schema)


def validate_output(value, validator):
    try:

        json.dumps(value, allow_nan=False)
        validator.validate(value)
    except (ValidationError, TypeError, ValueError):
        raise ValueError("AGENT_SCHEMA_MISMATCH") from None


def compile_schema(schema):
    return _Validator(check_schema(schema))
