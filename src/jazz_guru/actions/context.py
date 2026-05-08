from __future__ import annotations

import contextvars
from dataclasses import dataclass


@dataclass
class ToolContext:
    session_id: str | None = None
    turn_idx: int | None = None


_current: contextvars.ContextVar[ToolContext | None] = contextvars.ContextVar(
    "_jazz_guru_tool_ctx", default=None
)


def set_tool_context(ctx: ToolContext) -> contextvars.Token[ToolContext | None]:
    return _current.set(ctx)


def reset_tool_context(token: contextvars.Token[ToolContext | None]) -> None:
    _current.reset(token)


def current() -> ToolContext:
    ctx = _current.get()
    if ctx is None:
        return ToolContext()
    return ctx
