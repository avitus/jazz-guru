"""Postgres persistence for dynamic tools (Tier 2)."""
from __future__ import annotations

import uuid as uuid_mod
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from jazz_guru.actions.dynamic import DynamicSpec, hash_source
from jazz_guru.db import session_scope
from jazz_guru.state import GeneratedTool, GeneratedToolTest, GeneratedToolVersion


@dataclass
class RollbackResult:
    """Outcome of ``rollback(name, to_version)``.

    On success, ``new_version`` is the version number the rolled-back content
    now holds (rollback is forward in version space — see plan §B.6).
    """

    ok: bool
    tool_id: uuid_mod.UUID | None = None
    from_version: int | None = None
    to_version: int | None = None
    new_version: int | None = None
    error: str | None = None


async def upsert(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    source: str,
    scope: str = "global",
    owner_session_id: str | None = None,
    meta: dict[str, Any] | None = None,
    origin: str = "manual",
    rationale: str | None = None,
) -> uuid_mod.UUID:
    """Insert-or-replace by name.

    When an existing row is found, its current state is snapshotted into
    ``generated_tool_versions`` BEFORE the update runs, in the same
    transaction. ``origin`` distinguishes who initiated the supersede
    ("manual", "improver", "rollback") for later audit. ``rationale`` is a
    free-text reason (used by the improver).
    """
    sha = hash_source(source)
    sid = uuid_mod.UUID(owner_session_id) if owner_session_id else None
    async with session_scope() as s:
        # Row-lock the live tool row before computing next_version. Without
        # FOR UPDATE, two concurrent upserts can both read version v and try
        # to insert a snapshot with version v, colliding on the
        # (tool_id, version) uniqueness constraint and corrupting history.
        existing = (
            await s.execute(
                select(GeneratedTool)
                .where(GeneratedTool.name == name)
                .with_for_update()
            )
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
        # Snapshot the OLD row so rollback is one query away. Doing this
        # before the update means the version row captures the exact state
        # being replaced, not a half-mutated one.
        next_version = (existing.version or 0) + 1
        snapshot = GeneratedToolVersion(
            tool_id=existing.id,
            version=existing.version,
            source=existing.source,
            sha256=existing.sha256,
            input_schema=existing.input_schema or {},
            description=existing.description,
            meta=existing.meta or {},
            origin=origin,
            rationale=rationale,
            superseded_at=datetime.now(UTC),
            superseded_by=next_version,
        )
        s.add(snapshot)
        existing.description = description
        existing.input_schema = input_schema
        existing.source = source
        existing.sha256 = sha
        existing.scope = scope
        existing.owner_session_id = sid
        existing.version = next_version
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


# ---------- version history ------------------------------------------------


async def list_versions(name: str) -> list[GeneratedToolVersion]:
    """Historical versions of ``name`` ordered ascending by version number.

    Does NOT include the current version — that lives in ``generated_tools``.
    Returns an empty list for unknown tools.
    """
    async with session_scope() as s:
        tool = (
            await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
        ).scalar_one_or_none()
        if tool is None:
            return []
        rows = (
            await s.execute(
                select(GeneratedToolVersion)
                .where(GeneratedToolVersion.tool_id == tool.id)
                .order_by(GeneratedToolVersion.version.asc())
            )
        ).scalars().all()
        return list(rows)


async def get_version(name: str, version: int) -> GeneratedToolVersion | None:
    async with session_scope() as s:
        tool = (
            await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
        ).scalar_one_or_none()
        if tool is None:
            return None
        return (
            await s.execute(
                select(GeneratedToolVersion)
                .where(GeneratedToolVersion.tool_id == tool.id)
                .where(GeneratedToolVersion.version == version)
            )
        ).scalar_one_or_none()


async def rollback(name: str, to_version: int) -> RollbackResult:
    """Restore a historical version as the new current version.

    Snapshots the current row to ``_versions`` first, then writes the
    historical source/schema/description/meta back to ``generated_tools``
    and bumps version by one. Rollback is forward in version space: if
    current was v4 and you roll to v1, the new current is v5 with v1's
    content. This keeps version numbers monotonic and lets the audit log
    distinguish "we rolled back" from "we never went there."
    """
    async with session_scope() as s:
        # Row-lock to prevent a concurrent upsert from also computing
        # next_version against the same baseline — see the analogous
        # comment in ``upsert``.
        tool = (
            await s.execute(
                select(GeneratedTool)
                .where(GeneratedTool.name == name)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if tool is None:
            return RollbackResult(ok=False, error=f"unknown tool '{name}'")
        target = (
            await s.execute(
                select(GeneratedToolVersion)
                .where(GeneratedToolVersion.tool_id == tool.id)
                .where(GeneratedToolVersion.version == to_version)
            )
        ).scalar_one_or_none()
        if target is None:
            return RollbackResult(
                ok=False,
                tool_id=tool.id,
                error=f"no version {to_version} for '{name}'",
            )
        from_version = tool.version
        next_version = (from_version or 0) + 1
        snapshot = GeneratedToolVersion(
            tool_id=tool.id,
            version=from_version,
            source=tool.source,
            sha256=tool.sha256,
            input_schema=tool.input_schema or {},
            description=tool.description,
            meta=tool.meta or {},
            origin="rollback",
            rationale=f"rolled back to version {to_version}",
            superseded_at=datetime.now(UTC),
            superseded_by=next_version,
        )
        s.add(snapshot)
        # Restore historical content; scope and owner_session_id are
        # properties of the live tool, not the code state, so they pass
        # through unchanged.
        tool.source = target.source
        tool.sha256 = target.sha256
        tool.input_schema = target.input_schema or {}
        tool.description = target.description
        tool.meta = target.meta or {}
        tool.version = next_version
        tool.deprecated = False
        await s.flush()
        return RollbackResult(
            ok=True,
            tool_id=tool.id,
            from_version=from_version,
            to_version=to_version,
            new_version=next_version,
        )


# ---------- tests ----------------------------------------------------------


async def list_tests(name: str) -> list[GeneratedToolTest]:
    """Enabled test cases for ``name`` ordered by case name.

    Returns empty for unknown tools or tools with no tests yet. The
    improvement loop is a no-op for the latter (plan §A.9).
    """
    async with session_scope() as s:
        tool = (
            await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
        ).scalar_one_or_none()
        if tool is None:
            return []
        rows = (
            await s.execute(
                select(GeneratedToolTest)
                .where(GeneratedToolTest.tool_id == tool.id)
                .where(GeneratedToolTest.enabled.is_(True))
                .order_by(GeneratedToolTest.name.asc())
            )
        ).scalars().all()
        return list(rows)
