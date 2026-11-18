"""Structured extraction: data from a page, shaped by a JSON Schema."""

import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_SCHEMA_BYTES = 32 * 1024
MAX_SCHEMA_DEPTH = 12


def _depth(value: Any, level: int = 0) -> int:
    if isinstance(value, dict):
        items: list[Any] = list(value.values())  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    elif isinstance(value, list):
        items = list(value)  # pyright: ignore[reportUnknownArgumentType]
    else:
        return level
    return max((_depth(item, level + 1) for item in items), default=level)


class StructureSpec(BaseModel):
    """What to extract. Used with the `structured` output."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, validate_by_name=True, serialize_by_alias=True
    )

    json_schema: dict[str, Any] = Field(
        alias="schema",
        examples=[{"type": "object", "properties": {"price": {"type": "number"}}}],
    )
    """A JSON Schema (draft 2020-12) for the result. The top level must be an object."""
    instruction: str = Field(default="", max_length=4_000)
    """What to extract, in plain words, for example "the price and currency of the
    product"."""

    @field_validator("json_schema")
    @classmethod
    def _schema_is_usable(cls, schema: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(schema)) > MAX_SCHEMA_BYTES:
            raise ValueError(f"the schema is larger than {MAX_SCHEMA_BYTES} bytes")
        if _depth(schema) > MAX_SCHEMA_DEPTH:
            raise ValueError(f"the schema is nested deeper than {MAX_SCHEMA_DEPTH} levels")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as error:
            raise ValueError(f"not a valid JSON Schema: {error.message}") from error
        if schema.get("type") != "object":
            raise ValueError('the top level of the schema must be {"type": "object"}')
        return schema
