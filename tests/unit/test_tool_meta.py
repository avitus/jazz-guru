from __future__ import annotations

import pytest

from jazz_guru.actions.context import ToolContext, reset_tool_context, set_tool_context
from jazz_guru.actions.dynamic import DynamicRegistry
from jazz_guru.actions.registry import register_all, registry
from jazz_guru.actions.tools import tool_meta


@pytest.fixture
def attach_dyn():
    register_all()
    dyn = DynamicRegistry()
    registry.attach_dynamic(dyn)
    tok = set_tool_context(ToolContext(session_id=None, turn_idx=0))
    yield dyn
    registry.detach_dynamic()
    reset_tool_context(tok)


@pytest.mark.asyncio
async def test_create_then_call_through_registry(attach_dyn) -> None:
    out = await registry.invoke(
        "tool_create",
        {
            "name": "echo_args",
            "description": "echo back",
            "input_schema": {
                "type": "object",
                "properties": {"msg": {"type": "string"}},
                "required": ["msg"],
            },
            "source": "def run(msg): return {'echo': msg}",
        },
    )
    assert out["ok"] is True
    assert "echo_args" in registry
    res = await registry.invoke("echo_args", {"msg": "hello"})
    assert res == {"echo": "hello"}


@pytest.mark.asyncio
async def test_create_rejects_reserved_name(attach_dyn) -> None:
    out = await registry.invoke(
        "tool_create",
        {
            "name": "python_exec",
            "description": "x",
            "source": "def run(): return {}",
        },
    )
    assert out["ok"] is False
    assert "reserved" in out["error"]


@pytest.mark.asyncio
async def test_create_rejects_bad_source(attach_dyn) -> None:
    out = await registry.invoke(
        "tool_create",
        {
            "name": "no_run",
            "description": "x",
            "source": "def something_else(): pass",
        },
    )
    assert out["ok"] is False


@pytest.mark.asyncio
async def test_remove_session_tool(attach_dyn) -> None:
    await registry.invoke(
        "tool_create",
        {
            "name": "tmp_thing",
            "description": "x",
            "source": "def run(): return {'ok': True}",
        },
    )
    assert "tmp_thing" in registry
    out = await registry.invoke("tool_remove", {"name": "tmp_thing"})
    assert out["session_removed"] is True
    assert "tmp_thing" not in registry


@pytest.mark.asyncio
async def test_list_and_inspect(attach_dyn) -> None:
    await registry.invoke(
        "tool_create",
        {
            "name": "describe_me",
            "description": "self-describing",
            "source": "def run(): return {'ok': True}",
        },
    )
    listing = await registry.invoke("tool_list_dynamic", {})
    names = [i["name"] for i in listing["items"]]
    assert "describe_me" in names
    detail = await registry.invoke("tool_inspect", {"name": "describe_me"})
    assert detail["ok"] is True
    assert "def run" in detail["source"]


@pytest.mark.asyncio
async def test_emits_tool_proposed_event(attach_dyn) -> None:
    seen: list[tuple[str, dict]] = []
    tool_meta.set_event_sink(lambda n, p: seen.append((n, p)))
    try:
        await registry.invoke(
            "tool_create",
            {
                "name": "with_event",
                "description": "x",
                "source": "def run(): return {}",
            },
        )
    finally:
        tool_meta.set_event_sink(None)
    kinds = [n for (n, _) in seen]
    assert "tool_proposed" in kinds


@pytest.mark.asyncio
async def test_overwrites_existing_session_tool(attach_dyn) -> None:
    await registry.invoke(
        "tool_create",
        {
            "name": "to_replace",
            "description": "v1",
            "source": "def run(): return {'v': 1}",
        },
    )
    out1 = await registry.invoke("to_replace", {})
    assert out1 == {"v": 1}
    await registry.invoke(
        "tool_create",
        {
            "name": "to_replace",
            "description": "v2",
            "source": "def run(): return {'v': 2}",
        },
    )
    out2 = await registry.invoke("to_replace", {})
    assert out2 == {"v": 2}
