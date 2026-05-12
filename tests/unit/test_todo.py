from __future__ import annotations

import uuid

import pytest

from jazz_guru.actions.context import ToolContext, reset_tool_context, set_tool_context
from jazz_guru.actions.registry import register_all, registry
from jazz_guru.actions.tools import todo as todo_mod


@pytest.fixture
def in_memory_todos(monkeypatch: pytest.MonkeyPatch):
    """Replace the DB-backed load/mutate paths with an in-memory dict.

    Mutations now go through ``_mutate_todos`` (which uses
    ``SELECT ... FOR UPDATE`` to serialize concurrent updates); reads use
    ``_load_todos``. Tests stub both to stay independent of Postgres.
    """
    store: dict[uuid.UUID, list[dict]] = {}

    async def _load(sid):
        return list(store.get(sid, []))

    async def _save(sid, todos):
        store[sid] = list(todos)

    async def _mutate(sid, mutator):
        # Simulate the locking single-transaction path: read current, hand
        # to mutator, write back atomically.
        current = list(store.get(sid, []))
        new_todos, extra = mutator(list(current))
        store[sid] = list(new_todos)
        return new_todos, extra

    monkeypatch.setattr(todo_mod, "_load_todos", _load)
    monkeypatch.setattr(todo_mod, "_save_todos", _save)
    monkeypatch.setattr(todo_mod, "_mutate_todos", _mutate)

    register_all()
    sid = uuid.uuid4()
    tok = set_tool_context(ToolContext(session_id=str(sid), turn_idx=0))
    yield sid, store
    reset_tool_context(tok)


async def test_todo_add_then_list(in_memory_todos) -> None:
    out = await registry.invoke("todo", {"action": "add", "text": "generate a ii-V-I"})
    assert out["ok"] is True
    assert out["added"]["text"] == "generate a ii-V-I"
    assert out["count"] == 1
    listed = await registry.invoke("todo", {"action": "list"})
    assert listed["count"] == 1
    assert listed["todos"][0]["status"] == "open"


async def test_todo_set_replaces_list(in_memory_todos) -> None:
    await registry.invoke("todo", {"action": "add", "text": "old item"})
    out = await registry.invoke(
        "todo",
        {
            "action": "set",
            "items": [
                {"text": "step 1"},
                {"text": "step 2", "status": "in_progress"},
            ],
        },
    )
    assert out["count"] == 2
    listed = await registry.invoke("todo", {"action": "list"})
    statuses = [t["status"] for t in listed["todos"]]
    assert statuses == ["open", "in_progress"]


async def test_todo_start_and_complete(in_memory_todos) -> None:
    out = await registry.invoke("todo", {"action": "add", "text": "render"})
    tid = out["added"]["id"]
    started = await registry.invoke("todo", {"action": "start", "id": tid})
    assert started["todo"]["status"] == "in_progress"
    completed = await registry.invoke("todo", {"action": "complete", "id": tid})
    assert completed["todo"]["status"] == "done"


async def test_todo_remove(in_memory_todos) -> None:
    out = await registry.invoke("todo", {"action": "add", "text": "x"})
    tid = out["added"]["id"]
    removed = await registry.invoke("todo", {"action": "remove", "id": tid})
    assert removed["ok"] is True
    assert removed["count"] == 0
    listed = await registry.invoke("todo", {"action": "list"})
    assert listed["count"] == 0


async def test_todo_clear(in_memory_todos) -> None:
    await registry.invoke("todo", {"action": "add", "text": "a"})
    await registry.invoke("todo", {"action": "add", "text": "b"})
    out = await registry.invoke("todo", {"action": "clear"})
    assert out["cleared"] == 2
    listed = await registry.invoke("todo", {"action": "list"})
    assert listed["count"] == 0


async def test_todo_rejects_missing_id(in_memory_todos) -> None:
    out = await registry.invoke("todo", {"action": "start", "id": "nope"})
    assert out["ok"] is False
    assert "no such id" in out["error"]


async def test_todo_add_requires_text(in_memory_todos) -> None:
    out = await registry.invoke("todo", {"action": "add"})
    assert out["ok"] is False
    out2 = await registry.invoke("todo", {"action": "add", "text": "   "})
    assert out2["ok"] is False


async def test_todo_unknown_action(in_memory_todos) -> None:
    out = await registry.invoke("todo", {"action": "frobnicate"})
    assert out["ok"] is False
    assert "unknown action" in out["error"]


async def test_todo_set_skips_empty_items(in_memory_todos) -> None:
    out = await registry.invoke(
        "todo",
        {
            "action": "set",
            "items": [
                {"text": "real"},
                {"text": "  "},
                {},
                {"not_text": "x"},
                {"text": "also real"},
            ],
        },
    )
    assert out["count"] == 2


async def test_todo_rejects_missing_session(monkeypatch: pytest.MonkeyPatch) -> None:
    register_all()
    tok = set_tool_context(ToolContext(session_id=None, turn_idx=0))
    try:
        out = await registry.invoke("todo", {"action": "list"})
    finally:
        reset_tool_context(tok)
    assert out["ok"] is False
    assert "active session" in out["error"]
