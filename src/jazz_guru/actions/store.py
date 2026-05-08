"""Postgres persistence for dynamic tools (Tier 2)."""
from __future__ import annotations

import uuid as uuid_mod
from typing import Any

from sqlalchemy import select

from jazz_guru.actions.dynamic import DynamicSpec, hash_source
from jazz_guru.db import session_scope
from jazz_guru.state import GeneratedTool


async def upsert(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    source: str,
    scope: str = "global",
    owner_session_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> uuid_mod.UUID:
    sha = hash_source(source)
    sid = uuid_mod.UUID(owner_session_id) if owner_session_id else None
    async with session_scope() as s:
        existing = (
            await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
        ).scalar_one_or_none()
        if existing is None:
            row = GeneratedTool(
                name=name,
                description=description,
                input_schema=input_schema,
                source=source,
                sha256=sha,
                scope=scope,
                owner_session_id=sid,
                version=1,
                meta=meta or {},
            )
            s.add(row)
            await s.flush()
            return row.id
        existing.description = description
        existing.input_schema = input_schema
        existing.source = source
        existing.sha256 = sha
        existing.scope = scope
        existing.owner_session_id = sid
        existing.version = (existing.version or 0) + 1
        existing.deprecated = False
        existing.meta = meta or {}
        await s.flush()
        return existing.id


async def remove(name: str) -> bool:
    async with session_scope() as s:
        existing = (
            await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
        ).scalar_one_or_none()
        if existing is None:
            return False
        await s.delete(existing)
        return True


async def deprecate(name: str) -> bool:
    async with session_scope() as s:
        existing = (
            await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
        ).scalar_one_or_none()
        if existing is None:
            return False
        existing.deprecated = True
        return True


async def list_all(*, scope: str | None = None, include_deprecated: bool = False) -> list[GeneratedTool]:
    async with session_scope() as s:
        q = select(GeneratedTool)
        if scope is not None:
            q = q.where(GeneratedTool.scope == scope)
        if not include_deprecated:
            q = q.where(GeneratedTool.deprecated.is_(False))
        rows = (await s.execute(q.order_by(GeneratedTool.name))).scalars().all()
        return list(rows)


async def load_all_specs() -> list[DynamicSpec]:
    """Load all non-deprecated global tools as ``DynamicSpec``s."""
    rows = await list_all(scope="global", include_deprecated=False)
    out: list[DynamicSpec] = []
    for r in rows:
        out.append(
            DynamicSpec(
                name=r.name,
                description=r.description,
                input_schema=r.input_schema or {},
                source=r.source,
                sha256=r.sha256,
                execution=(r.meta or {}).get("execution", "subprocess"),
                scope=r.scope,
                owner_session_id=str(r.owner_session_id) if r.owner_session_id else None,
                version=r.version,
                meta=r.meta or {},
            )
        )
    return out
