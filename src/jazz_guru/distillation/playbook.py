from __future__ import annotations

from typing import Any

from sqlalchemy import select

from jazz_guru.db import session_scope
from jazz_guru.state.schema import PlaybookEntry


async def upsert_entry(scope: str, text: str, *, score: float = 0.0, meta: dict[str, Any] | None = None) -> None:
    async with session_scope() as s:
        existing = (
            await s.execute(
                select(PlaybookEntry).where(
                    PlaybookEntry.scope == scope, PlaybookEntry.text == text
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            s.add(PlaybookEntry(scope=scope, text=text, score=score, meta=meta or {}))
        else:
            existing.score = max(existing.score, score)
            existing.meta = {**existing.meta, **(meta or {})}


async def top_entries(*, scope: str | None = None, k: int = 16) -> list[PlaybookEntry]:
    async with session_scope() as s:
        stmt = select(PlaybookEntry).order_by(PlaybookEntry.score.desc()).limit(k)
        if scope:
            stmt = stmt.where(PlaybookEntry.scope == scope)
        return list((await s.execute(stmt)).scalars().all())
