from __future__ import annotations

import textwrap

import pytest

from jazz_guru.actions.dynamic import (
    DynamicRegistry,
    DynamicSpec,
    ToolValidationError,
    hash_source,
    invoke,
    validate_name,
    validate_schema,
    validate_source,
)


def test_validate_name_ok_and_reject() -> None:
    assert validate_name("my_tool_42") == "my_tool_42"
    with pytest.raises(ToolValidationError):
        validate_name("Bad-Name")
    with pytest.raises(ToolValidationError):
        validate_name("python_exec")  # reserved
    with pytest.raises(ToolValidationError):
        validate_name("_leading_underscore")


def test_validate_schema_normalizes() -> None:
    s = validate_schema(None)
    assert s["type"] == "object"
    assert s["properties"] == {}
    s2 = validate_schema({"properties": {"x": {"type": "string"}}})
    assert s2["type"] == "object"
    assert s2["additionalProperties"] is False


def test_validate_source_requires_run() -> None:
    with pytest.raises(ToolValidationError):
        validate_source("")
    with pytest.raises(ToolValidationError):
        validate_source("def other():\n    pass\n")
    validate_source("def run(**kw):\n    return {'ok': True}\n")


def test_validate_source_rejects_syntax_errors() -> None:
    with pytest.raises(ToolValidationError):
        validate_source("def run(:\n  pass\n")


@pytest.mark.asyncio
async def test_invoke_subprocess_round_trip() -> None:
    src = textwrap.dedent(
        """
        def run(a, b):
            return {"sum": a + b}
        """
    )
    spec = DynamicSpec(
        name="adder",
        description="add",
        input_schema={"type": "object"},
        source=src,
        sha256=hash_source(src),
        execution="subprocess",
    )
    out = await invoke(spec, {"a": 2, "b": 3})
    assert out == {"sum": 5}


@pytest.mark.asyncio
async def test_invoke_subprocess_reports_error() -> None:
    src = textwrap.dedent(
        """
        def run():
            raise ValueError("nope")
        """
    )
    spec = DynamicSpec(
        name="boom", description="b", input_schema={"type": "object"},
        source=src, sha256=hash_source(src), execution="subprocess",
    )
    out = await invoke(spec, {})
    assert "__error__" in out
    assert "ValueError" in out["__error__"]


@pytest.mark.asyncio
async def test_invoke_inproc_async_run() -> None:
    src = textwrap.dedent(
        """
        async def run(x):
            return {"doubled": x * 2}
        """
    )
    spec = DynamicSpec(
        name="dbl", description="d", input_schema={"type": "object"},
        source=src, sha256=hash_source(src), execution="inproc",
    )
    out = await invoke(spec, {"x": 21})
    assert out == {"doubled": 42}


def test_dynamic_registry_basic() -> None:
    r = DynamicRegistry()
    s = DynamicSpec(name="t", description="d", input_schema={"type": "object"},
                    source="def run(): return {}", sha256="x")
    assert "t" not in r
    r.add(s)
    assert "t" in r
    assert r.names() == ["t"]
    assert r.get("t") is s
    assert r.remove("t") is True
    assert r.remove("t") is False
