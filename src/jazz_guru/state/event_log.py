from __future__ import annotations

import uuid
from typing import Any

from jazz_guru.db import session_scope
from jazz_guru.state.schema import Event


async def log_event(
    *,
    session_id: uuid.UUID,
    type: str,
    payload: dict[str, Any],
    turn_id: uuid.UUID | None = None,
) -> uuid.UUID:
    async with session_scope() as s:
        ev = Event(session_id=session_id, turn_id=turn_id, type=type, payload=payload)
        s.add(ev)
        await s.flush()
        return ev.id
