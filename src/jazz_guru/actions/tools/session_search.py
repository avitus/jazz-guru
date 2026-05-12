"""``session_search`` — keyword search across prior sessions' turns.

Uses a Postgres trigram GIN index on ``turns.content::text`` (see alembic
revision ``0003``) so ``ILIKE '%query%'`` is fast. Optionally summarizes the
top hits via the LLM (``summarize=true``), which collapses dozens of hits
into a single readable paragraph before they hit the agent's context.
"""
from __future__ import annotations

import uuid as uuid_mod
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, Field
from sqlalchemy import cast, select

from jazz_guru.actions.registry import registry
from jazz_guru.db import session_scope
from jazz_guru.memory import summarize_history
from jazz_guru.state import Session as SessionRow
from jazz_guru.state import Turn


class SessionSearchInput(BaseModel):
    query: str = Field(..., description="Substring to search for (case-insensitive).")
    k: int = Field(default=10, ge=1, le=100, description="Max number of hits to return.")
    session_id: str | None = Field(
        default=None, description="Restrict the search to a single session UUID."
    )
    days: int | None = Field(
        default=None,
        ge=1,
        description="Restrict to turns started within the last N days.",
    )
    role: str | None = Field(
        default=None,
        description="Restrict to 'user' or 'assistant' turns only.",
    )
    summarize: bool = Field(
        default=False,
        description=(
            "If true, the hit list is also summarized into a short paragraph "
            "via the LLM. Saves context vs. raw hit dumps."
        ),
    )


async def _search_turns(
    query: str,
    *,
    k: int,
    session_id: uuid_mod.UUID | None,
    days: int | None,
    role: str | None,
) -> list[dict[str, Any]]:
    """Run the SQL search. Factored out so unit tests can monkeypatch it."""
    # Escape ILIKE wildcards in user input so a literal '%' or '_' in the
    # query doesn't blow up the match set (or accidentally match everything).
    escaped = (
        query.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    pattern = f"%{escaped}%"
    async with session_scope() as s:
        # cast(Turn.content, sa.Text) matches the indexed expression in the
        # 0003 migration, so the GIN index can satisfy this query.
        content_text = cast(Turn.content, sa.Text)
        stmt = (
            select(Turn, SessionRow.title)
            .join(SessionRow, SessionRow.id == Turn.session_id)
            .where(content_text.ilike(pattern, escape="\\"))
            .order_by(Turn.started_at.desc())
            .limit(k)
        )
        if session_id is not None:
            stmt = stmt.where(Turn.session_id == session_id)
        if days is not None:
            cutoff = datetime.now(UTC) - timedelta(days=days)
            stmt = stmt.where(Turn.started_at >= cutoff)
        if role is not None:
            stmt = stmt.where(Turn.role == role)
        rows = (await s.execute(stmt)).all()
    hits: list[dict[str, Any]] = []
    for t, title in rows:
        if isinstance(t.content, dict):
            text = str(t.content.get("text") or "")
        else:
            text = str(t.content or "")
        hits.append(
            {
                "turn_id": str(t.id),
                "session_id": str(t.session_id),
                "session_title": title,
                "idx": t.idx,
                "role": t.role,
                "text": text[:400],
                "started_at": t.started_at.isoformat() if t.started_at else None,
            }
        )
    return hits


@registry.register(
    "session_search",
    description=(
        "Keyword search across prior sessions' turns (uses a Postgres pg_trgm "
        "GIN index, so substring matches are fast). Returns up to k hits with "
        "session title, role, and a 400-char excerpt. Set summarize=true to "
        "also get an LLM-generated paragraph summarizing the hits."
    ),
    input_model=SessionSearchInput,
    tags=("memory",),
)
async def session_search(
    query: str,
    k: int = 10,
    session_id: str | None = None,
    days: int | None = None,
    role: str | None = None,
    summarize: bool = False,
) -> dict[str, Any]:
    if not query.strip():
        return {"ok": False, "error": "query must not be empty"}
    sid: uuid_mod.UUID | None
    if session_id:
        try:
            sid = uuid_mod.UUID(session_id)
        except ValueError:
            return {"ok": False, "error": f"invalid session_id: {session_id!r}"}
    else:
        sid = None
    try:
        hits = await _search_turns(query, k=k, session_id=sid, days=days, role=role)
    except Exception as e:
        return {"ok": False, "error": f"search failed: {type(e).__name__}: {e}"}
    out: dict[str, Any] = {"ok": True, "query": query, "hits": hits, "count": len(hits)}
    if summarize and hits:
        try:
            out["summary"] = await summarize_history(
                [{"role": h["role"], "content": h["text"]} for h in hits]
            )
        except Exception as e:
            out["summary_error"] = f"{type(e).__name__}: {e}"
    return out
