"""``todo`` — per-session task list backed by ``Session.meta['todos']``.

Lightweight planning surface so the agent can break work into steps it can
track. Persists in the existing ``sessions.meta`` JSON column — no new table.
The list is per-session, not shared across sessions; for cross-session memory
of recurring tasks, the playbook + skills serve that role.
"""
from __future__ import annotations

import time
import uuid as uuid_mod
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from jazz_guru.actions.context import current
from jazz_guru.actions.registry import registry
from jazz_guru.db import session_scope
from jazz_guru.state import Session as SessionRow

_STATUSES = ("open", "in_progress", "done")


class TodoInput(BaseModel):
    action: str = Field(
        ...,
        description=(
            "One of: 'list' (show current todos), 'add' (text=...), 'set' "
            "(items=[{text, status?}]), 'start' (id=...), 'complete' (id=...), "
            "'remove' (id=...), 'clear'."
        ),
    )
    text: str | None = Field(default=None, description="For 'add': the task description.")
    items: list[dict[str, Any]] | None = Field(
        default=None,
        description="For 'set': new list of items as [{text: str, status?: str}].",
    )
    id: str | None = Field(default=None, description="For 'start'/'complete'/'remove': target id.")


def _new_todo(text: str, status: str = "open") -> dict[str, Any]:
    if status not in _STATUSES:
        status = "open"
    now = time.time()
    return {
        "id": uuid_mod.uuid4().hex[:8],
        "text": text,
        "status": status,
        "created_at": now,
        "updated_at": now,
    }


async def _load_todos(session_id: uuid_mod.UUID) -> list[dict[str, Any]]:
    async with session_scope() as s:
        row = (
            await s.execute(select(SessionRow).where(SessionRow.id == session_id))
        ).scalar_one_or_none()
        if row is None or not isinstance(row.meta, dict):
            return []
        todos = row.meta.get("todos", [])
        return [t for t in todos if isinstance(t, dict)]


async def _save_todos(session_id: uuid_mod.UUID, todos: list[dict[str, Any]]) -> None:
    async with session_scope() as s:
        row = (
            await s.execute(select(SessionRow).where(SessionRow.id == session_id))
        ).scalar_one_or_none()
        if row is None:
            return
        meta = dict(row.meta or {})
        meta["todos"] = todos
        row.meta = meta


async def _mutate_todos(
    session_id: uuid_mod.UUID,
    mutator: Callable[[list[dict[str, Any]]], tuple[list[dict[str, Any]], Any]],
) -> tuple[list[dict[str, Any]] | None, Any]:
    """Read-modify-write in ONE transaction with row locking.

    Without ``with_for_update`` two concurrent ``todo add`` calls could each
    read the same starting list and both write back -- one update is then
    silently lost. This serializes mutations per session.

    ``mutator`` receives the current todos list (a fresh copy) and returns a
    ``(new_todos, extra)`` tuple. ``extra`` is opaque to the helper and just
    bubbled back to the caller (typically the new/affected item the caller
    wants to return).
    """
    async with session_scope() as s:
        row = (
            await s.execute(
                select(SessionRow)
                .where(SessionRow.id == session_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            # Surface as an explicit failure rather than a silent no-op write —
            # otherwise add/set/clear would report success without persisting.
            return None, None
        meta = dict(row.meta or {})
        current_list = meta.get("todos", [])
        todos = [t for t in current_list if isinstance(t, dict)]
        new_todos, extra = mutator(list(todos))
        meta["todos"] = new_todos
        row.meta = meta
        return new_todos, extra


@registry.register(
    "todo",
    description=(
        "Per-session task list. Use to plan multi-step work and track progress. "
        "Actions: list, add (text=...), set (items=[{text,status?}]), start (id=...), "
        "complete (id=...), remove (id=...), clear. Statuses: open|in_progress|done."
    ),
    input_model=TodoInput,
    tags=("control",),
)
async def todo(
    action: str,
    text: str | None = None,
    items: list[dict[str, Any]] | None = None,
    id: str | None = None,
) -> dict[str, Any]:
    ctx = current()
    if ctx.session_id is None:
        return {"ok": False, "error": "todo requires an active session"}
    try:
        sid = uuid_mod.UUID(ctx.session_id)
    except ValueError:
        return {"ok": False, "error": f"invalid session_id: {ctx.session_id!r}"}

    if action == "list":
        todos = await _load_todos(sid)
        return {"ok": True, "todos": todos, "count": len(todos)}

    if action == "add":
        if not text or not text.strip():
            return {"ok": False, "error": "add requires non-empty 'text'"}
        item = _new_todo(text.strip())

        def _mut_add(todos: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            todos.append(item)
            return todos, item

        new_todos, added = await _mutate_todos(sid, _mut_add)
        if new_todos is None:
            return {"ok": False, "error": f"session not found: {sid}"}
        return {"ok": True, "added": added, "count": len(new_todos)}

    if action == "set":
        if items is None:
            return {"ok": False, "error": "set requires 'items'"}
        materialized: list[dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            t = (it.get("text") or "").strip()
            if not t:
                continue
            status = str(it.get("status") or "open")
            materialized.append(_new_todo(t, status=status))

        def _mut_set(_todos: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], None]:
            return list(materialized), None

        new_todos, _ = await _mutate_todos(sid, _mut_set)
        if new_todos is None:
            return {"ok": False, "error": f"session not found: {sid}"}
        return {"ok": True, "todos": new_todos, "count": len(new_todos)}

    if action in ("start", "complete", "remove"):
        if not id:
            return {"ok": False, "error": f"{action} requires 'id'"}

        def _mut_id(todos: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
            idx = next((i for i, t in enumerate(todos) if t.get("id") == id), -1)
            if idx < 0:
                return todos, None
            if action == "remove":
                removed = todos.pop(idx)
                return todos, removed
            todos[idx]["status"] = "in_progress" if action == "start" else "done"
            todos[idx]["updated_at"] = time.time()
            return todos, todos[idx]

        new_todos, target = await _mutate_todos(sid, _mut_id)
        if new_todos is None:
            return {"ok": False, "error": f"session not found: {sid}"}
        if target is None:
            return {"ok": False, "error": f"no such id: {id}"}
        if action == "remove":
            return {"ok": True, "removed": target, "count": len(new_todos)}
        return {"ok": True, "todo": target}

    if action == "clear":
        def _mut_clear(todos: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
            return [], len(todos)

        new_todos, prev_count = await _mutate_todos(sid, _mut_clear)
        if new_todos is None:
            return {"ok": False, "error": f"session not found: {sid}"}
        return {"ok": True, "cleared": prev_count}

    return {"ok": False, "error": f"unknown action: {action!r}"}
