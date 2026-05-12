from __future__ import annotations

import json

from pydantic import BaseModel, Field

from jazz_guru.actions.dynamic import validate_schema
from jazz_guru.actions.registry import ToolRegistry
from jazz_guru.actions.schema import normalize_input_schema


def test_normalize_empty_input() -> None:
    assert normalize_input_schema(None) == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def test_normalize_strips_titles_recursively() -> None:
    raw = {
        "title": "Outer",
        "type": "object",
        "properties": {
            "x": {"title": "X field", "type": "integer"},
            "nested": {
                "title": "Nested",
                "type": "object",
                "properties": {"y": {"title": "Y", "type": "string"}},
            },
        },
        "required": ["x"],
    }
    out = normalize_input_schema(raw)
    assert "title" not in out
    assert "title" not in out["properties"]["x"]
    assert "title" not in out["properties"]["nested"]
    assert "title" not in out["properties"]["nested"]["properties"]["y"]
    assert out["required"] == ["x"]
    assert out["additionalProperties"] is False


def test_normalize_sorts_properties() -> None:
    raw = {"type": "object", "properties": {"b": {"type": "string"}, "a": {"type": "integer"}}}
    out = normalize_input_schema(raw)
    assert list(out["properties"].keys()) == ["a", "b"]


def test_static_and_dynamic_schemas_match_for_equivalent_inputs() -> None:
    class Inp(BaseModel):
        x: int = Field(..., description="the x")
        y: str = "hello"

    # Static path
    reg = ToolRegistry()

    @reg.register("t", description="t", input_model=Inp)
    async def _t(x: int, y: str = "hello") -> dict:
        return {"x": x, "y": y}

    static_schema = reg.get("t").input_schema

    # Dynamic path: hand-built equivalent schema, run through validate_schema
    dynamic_schema = validate_schema(
        {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "the x"},
                "y": {"type": "string", "default": "hello"},
            },
            "required": ["x"],
        }
    )

    # Byte-identical canonical JSON.
    assert json.dumps(static_schema, sort_keys=True) == json.dumps(
        dynamic_schema, sort_keys=True
    )


def test_preserves_defs_block() -> None:
    raw = {
        "type": "object",
        "properties": {"p": {"$ref": "#/$defs/Pt"}},
        "$defs": {"Pt": {"type": "object", "properties": {"x": {"type": "number"}}}},
    }
    out = normalize_input_schema(raw)
    assert "$defs" in out
    assert out["$defs"]["Pt"]["properties"]["x"]["type"] == "number"
