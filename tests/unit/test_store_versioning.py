"""Round-trip tests for ``actions/store.py`` versioning + rollback.

These tests touch the real configured Postgres. Each test uses a
UUID-suffixed tool name to avoid collisions and cleans up via a
``try/finally`` wrapper. Migration ``0003`` must have been applied
(``make migrate``); without the new tables every test will error
immediately.
"""
from __future__ import annotations

import uuid as uuid_mod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from jazz_guru.actions import store


def _unique(prefix: str) -> str:
    """Tool name unlikely to collide with anything else in the DB."""
    return f"_t_{prefix}_{uuid_mod.uuid4().hex[:10]}"


@asynccontextmanager
async def _tool(name: str, **kwargs: Any) -> AsyncIterator[str]:
    """Create a tool, yield its name, and always clean it up."""
    defaults = dict(
        description="x",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        source="def run(**kwargs):\n    return {'ok': True}\n",
    )
    defaults.update(kwargs)
    await store.upsert(name=name, **defaults)
    try:
        yield name
    finally:
        # Cascades drop versions/tests/runs along with the tool.
        await store.remove(name)


async def test_first_upsert_does_not_snapshot() -> None:
    """A fresh tool has no prior state, so no version row should appear."""
    name = _unique("init")
    async with _tool(name):
        versions = await store.list_versions(name)
        assert versions == []


async def test_second_upsert_snapshots_prior_content() -> None:
    """An update must snapshot the previous source/schema/etc."""
    name = _unique("snap")
    async with _tool(
        name,
        description="v1 desc",
        source="def run():\n    return 1\n",
    ):
        # Replace the content; the snapshot should capture v1's body.
        await store.upsert(
            name=name,
            description="v2 desc",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            source="def run():\n    return 2\n",
        )
        versions = await store.list_versions(name)
        assert len(versions) == 1
        snap = versions[0]
        assert snap.version == 1
        assert snap.description == "v1 desc"
        assert snap.source == "def run():\n    return 1\n"
        assert snap.origin == "manual"
        assert snap.superseded_by == 2
        assert snap.superseded_at is not None


async def test_origin_and_rationale_propagate() -> None:
    """Improver/rollback need ``origin`` and ``rationale`` to survive."""
    name = _unique("origin")
    async with _tool(name, source="def run():\n    return 1\n"):
        await store.upsert(
            name=name,
            description="v2",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            source="def run():\n    return 2\n",
            origin="improver",
            rationale="fixed off-by-one",
        )
        versions = await store.list_versions(name)
        assert len(versions) == 1
        assert versions[0].origin == "improver"
        assert versions[0].rationale == "fixed off-by-one"


async def test_list_versions_ordered_ascending() -> None:
    """Multiple supersedes accumulate; order matters for diff/show CLIs."""
    name = _unique("order")
    async with _tool(name, source="def run():\n    return 1\n"):
        for i in range(2, 5):
            await store.upsert(
                name=name,
                description=f"v{i}",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                source=f"def run():\n    return {i}\n",
            )
        versions = await store.list_versions(name)
        assert [v.version for v in versions] == [1, 2, 3]
        assert versions[0].source.endswith("return 1\n")
        assert versions[1].source.endswith("return 2\n")
        assert versions[2].source.endswith("return 3\n")


async def test_get_version_returns_target() -> None:
    name = _unique("get")
    async with _tool(name, source="def run():\n    return 1\n"):
        await store.upsert(
            name=name,
            description="v2",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            source="def run():\n    return 2\n",
        )
        v1 = await store.get_version(name, 1)
        assert v1 is not None
        assert v1.source == "def run():\n    return 1\n"


async def test_get_version_returns_none_for_missing() -> None:
    name = _unique("missing")
    async with _tool(name):
        assert await store.get_version(name, 99) is None
    # Also: unknown tool entirely.
    assert await store.get_version("_t_does_not_exist", 1) is None


async def test_rollback_restores_prior_content_and_bumps_forward() -> None:
    """Rolling back v3 → v1 yields v4 holding v1's content (plan §B.6)."""
    name = _unique("rb")
    async with _tool(
        name,
        description="v1",
        source="def run():\n    return 1\n",
    ):
        await store.upsert(
            name=name,
            description="v2",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            source="def run():\n    return 2\n",
        )
        await store.upsert(
            name=name,
            description="v3",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            source="def run():\n    return 3\n",
        )
        result = await store.rollback(name, to_version=1)
        assert result.ok is True
        assert result.from_version == 3
        assert result.to_version == 1
        assert result.new_version == 4

        # Current row should now hold v1's content under version=4.
        current = next(t for t in await store.list_all() if t.name == name)
        assert current.version == 4
        assert current.source == "def run():\n    return 1\n"
        assert current.description == "v1"

        # The v3 state should have been snapshotted with origin="rollback".
        versions = await store.list_versions(name)
        # v1 + v2 + v3-as-rolled-back-from = 3 rows
        assert [v.version for v in versions] == [1, 2, 3]
        rollback_snap = next(v for v in versions if v.version == 3)
        assert rollback_snap.origin == "rollback"
        assert "rolled back to version 1" in (rollback_snap.rationale or "")


async def test_rollback_unknown_tool() -> None:
    result = await store.rollback("_t_definitely_not_there", 1)
    assert result.ok is False
    assert "unknown tool" in (result.error or "")


async def test_rollback_unknown_version() -> None:
    name = _unique("rb_bad_ver")
    async with _tool(name):
        result = await store.rollback(name, to_version=99)
        assert result.ok is False
        assert "no version 99" in (result.error or "")


async def test_list_tests_empty_for_new_tool() -> None:
    """Plan §A.9: tools without tests are skipped by the improvement loop."""
    name = _unique("notests")
    async with _tool(name):
        assert await store.list_tests(name) == []


async def test_list_tests_filters_disabled() -> None:
    """Explicit disable lets the user mute a flaky case without deleting it."""
    from sqlalchemy import select

    from jazz_guru.db import session_scope
    from jazz_guru.state import GeneratedTool, GeneratedToolTest

    name = _unique("disabled")
    async with _tool(name):
        async with session_scope() as s:
            tool = (
                await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
            ).scalar_one()
            s.add(
                GeneratedToolTest(
                    tool_id=tool.id, name="active_case", spec={"case": {}}, enabled=True
                )
            )
            s.add(
                GeneratedToolTest(
                    tool_id=tool.id, name="muted_case", spec={"case": {}}, enabled=False
                )
            )
        tests = await store.list_tests(name)
        assert [t.name for t in tests] == ["active_case"]


async def test_list_versions_unknown_tool_returns_empty() -> None:
    """No exception, just an empty list — callers iterate over the result."""
    assert await store.list_versions("_t_no_such_tool") == []


@pytest.mark.parametrize("ver_payload", [42, -1])
async def test_get_version_handles_missing_version_types_gracefully(
    ver_payload: int,
) -> None:
    """Defensive: ``get_version`` returns None for missing integer versions
    (both positive and negative) rather than raising."""
    name = _unique("typecheck")
    async with _tool(name):
        assert await store.get_version(name, ver_payload) is None
