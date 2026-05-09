from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from jazz_guru.db import session_scope
from jazz_guru.state import Session as SessionRow
from jazz_guru.state import Turn


@dataclass
class SessionHandle:
    id: uuid.UUID
    goal_profile: str = "default"
    title: str | None = None
    next_turn_idx: int = 0
    history: list[dict[str, object]] = field(default_factory=list)


def row_to_api_message(role: str, content: Any) -> dict[str, Any] | None:
    """Reshape a persisted Turn into an Anthropic-API message dict.

    The DB stores ``Turn.content`` as a JSON object like ``{"text": "..."}``
    for record-keeping; the API expects ``content`` to be a string or a list
    of content blocks. Returns None for rows we cannot or should not replay
    (non-user/assistant roles, missing text, etc.). Tool_use / tool_result
    chains from prior turns are not persisted in API shape, so they aren't
    replayed; the agent's cross-turn memory of tool calls is the snapshot,
    playbook, and memory store, not the message history.
    """
    if role not in ("user", "assistant"):
        return None
    d = content if isinstance(content, dict) else {}
    text = str(d.get("text", "")).strip()
    if not text:
        return None
    return {"role": role, "content": text}


class SessionManager:
    """Thin facade over the sessions/turns tables."""

    async def create(self, *, goal_profile: str = "default", title: str | None = None) -> SessionHandle:
        async with session_scope() as s:
            row = SessionRow(goal_profile=goal_profile, title=title)
            s.add(row)
            await s.flush()
            return SessionHandle(id=row.id, goal_profile=row.goal_profile, title=row.title)

    async def load(self, session_id: uuid.UUID) -> SessionHandle:
        async with session_scope() as s:
            row = (await s.execute(select(SessionRow).where(SessionRow.id == session_id))).scalar_one()
            turns = (
                await s.execute(
                    select(Turn).where(Turn.session_id == session_id).order_by(Turn.idx.asc())
                )
            ).scalars().all()
            handle = SessionHandle(
                id=row.id,
                goal_profile=row.goal_profile,
                title=row.title,
                next_turn_idx=(turns[-1].idx + 1) if turns else 0,
            )
            for t in turns:
                msg = row_to_api_message(t.role, t.content)
                if msg:
                    handle.history.append(msg)
            return handle
