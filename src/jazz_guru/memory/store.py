from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select

from jazz_guru.db import session_scope
from jazz_guru.memory.embeddings import EmbeddingProvider, get_embeddings
from jazz_guru.state.schema import MemoryItem


@dataclass
class MemoryRecord:
    id: uuid.UUID
    kind: str
    text: str
    score: float
    meta: dict[str, Any]


class MemoryStore(Protocol):
    async def write(
        self,
        *,
        text: str,
        kind: str = "note",
        session_id: uuid.UUID | None = None,
        meta: dict[str, Any] | None = None,
        score: float = 0.0,
    ) -> uuid.UUID: ...

    async def search(
        self,
        query: str,
        *,
        k: int = 5,
        session_id: uuid.UUID | None = None,
        kinds: list[str] | None = None,
    ) -> list[MemoryRecord]: ...


class PgvectorMemoryStore:
    def __init__(self, embeddings: EmbeddingProvider | None = None) -> None:
        self._emb = embeddings or get_embeddings()

    async def write(
        self,
        *,
        text: str,
        kind: str = "note",
        session_id: uuid.UUID | None = None,
        meta: dict[str, Any] | None = None,
        score: float = 0.0,
    ) -> uuid.UUID:
        vec = (await self._emb.embed([text]))[0]
        async with session_scope() as s:
            item = MemoryItem(
                session_id=session_id,
                kind=kind,
                text=text,
                meta=meta or {},
                embedding=vec,
                score=score,
            )
            s.add(item)
            await s.flush()
            return item.id

    async def search(
        self,
        query: str,
        *,
        k: int = 5,
        session_id: uuid.UUID | None = None,
        kinds: list[str] | None = None,
    ) -> list[MemoryRecord]:
        vec = (await self._emb.embed([query]))[0]
        async with session_scope() as s:
            stmt = select(
                MemoryItem,
                MemoryItem.embedding.cosine_distance(vec).label("distance"),
            )
            if session_id is not None:
                stmt = stmt.where(MemoryItem.session_id == session_id)
            if kinds:
                stmt = stmt.where(MemoryItem.kind.in_(kinds))
            stmt = stmt.order_by("distance").limit(k)
            rows = (await s.execute(stmt)).all()
        out: list[MemoryRecord] = []
        for item, dist in rows:
            out.append(
                MemoryRecord(
                    id=item.id,
                    kind=item.kind,
                    text=item.text,
                    score=float(1.0 - float(dist)),
                    meta=item.meta or {},
                )
            )
        return out


_default: MemoryStore | None = None


def get_memory() -> MemoryStore:
    global _default
    if _default is None:
        _default = PgvectorMemoryStore()
    return _default
