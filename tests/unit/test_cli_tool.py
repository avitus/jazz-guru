"""Smoke tests for the ``jazz-guru tool ...`` CLI surface.

CLI commands are thin wrappers around store + meta-tools (which have their
own thorough tests). These tests verify the wiring: subcommands are
registered, help renders, and the happy/sad paths return appropriate exit
codes against a real (UUID-named, cleaned-up) tool.

A subtlety: typer's CliRunner runs each CLI command in its own
``asyncio.run`` (the commands wrap their bodies that way). When a test
fixture also uses ``asyncio.run`` for setup, the cached SQLAlchemy
engine ends up bound to a loop that's already closed by the time the
CLI runs. The fix is to dispose between each ``asyncio.run`` so the
next call gets a fresh engine + pool.
"""
from __future__ import annotations

import asyncio
import uuid as uuid_mod

import pytest
from typer.testing import CliRunner

from jazz_guru.actions import store
from jazz_guru.actions.tools.tool_test_meta import tool_test_add
from jazz_guru.cli import app

runner = CliRunner()


def _unique(prefix: str) -> str:
    return f"_t_{prefix}_{uuid_mod.uuid4().hex[:10]}"


def _dispose_engine() -> None:
    """Drop any cached SQLAlchemy engine so the next ``asyncio.run`` rebuilds.

    Necessary because asyncpg connections are bound to the event loop that
    created them; multiple ``asyncio.run`` calls in one test (fixture
    setup → CLI invoke → fixture teardown) each create a fresh loop.
    """
    from jazz_guru import db

    if db.get_engine.cache_info().currsize > 0:

        async def _d() -> None:
            await db.get_engine().dispose()

        asyncio.run(_d())
        db.get_engine.cache_clear()
        db.get_sessionmaker.cache_clear()


@pytest.fixture
def published_tool() -> str:
    """Publish a tool, hand its name to the test, then remove it."""
    name = _unique("cli")

    async def _setup() -> None:
        await store.upsert(
            name=name,
            description="cli test tool",
            input_schema={"type": "object", "additionalProperties": True},
            source="def run(**kwargs):\n    return {'ok': True}\n",
        )

    async def _teardown() -> None:
        await store.remove(name)

    asyncio.run(_setup())
    _dispose_engine()
    try:
        yield name
    finally:
        _dispose_engine()
        asyncio.run(_teardown())
        _dispose_engine()


def test_tool_help_shows_subcommands() -> None:
    result = runner.invoke(app, ["tool", "--help"])
    assert result.exit_code == 0
    for sub in ("list", "show", "test", "diff", "rollback"):
        assert sub in result.stdout


def test_tool_list_smoke() -> None:
    """``list`` should run cleanly even if no tools are published."""
    result = runner.invoke(app, ["tool", "list"])
    assert result.exit_code == 0


def test_tool_show_unknown_returns_error_exit() -> None:
    result = runner.invoke(app, ["tool", "show", "_t_nope_does_not_exist"])
    assert result.exit_code != 0


def test_tool_show_known_tool(published_tool: str) -> None:
    result = runner.invoke(app, ["tool", "show", published_tool])
    assert result.exit_code == 0, result.stdout
    # The output panel should at least mention the version field.
    assert "version" in result.stdout


def test_tool_test_passes_when_predicate_matches(published_tool: str) -> None:
    asyncio.run(
        tool_test_add(
            name=published_tool,
            case_name="smoke",
            case_spec={
                "case": {"input": {}, "predicate": {"result.ok": True}},
            },
        )
    )
    _dispose_engine()
    result = runner.invoke(app, ["tool", "test", published_tool])
    assert result.exit_code == 0, result.stdout
    assert "passed" in result.stdout


def test_tool_test_no_cases_succeeds_with_note(published_tool: str) -> None:
    """Tools without cases should exit 0 (nothing to fail), with a note."""
    result = runner.invoke(app, ["tool", "test", published_tool])
    assert result.exit_code == 0
    assert "no enabled cases" in result.stdout


def test_tool_test_fails_loudly_when_a_case_red(published_tool: str) -> None:
    asyncio.run(
        tool_test_add(
            name=published_tool,
            case_name="wrong",
            case_spec={
                "case": {"input": {}, "predicate": {"result.ok": False}},
            },
        )
    )
    _dispose_engine()
    result = runner.invoke(app, ["tool", "test", published_tool])
    # Non-zero exit so CI/shell pipelines can detect failure.
    assert result.exit_code != 0


def test_tool_rollback_unknown_returns_error(published_tool: str) -> None:
    """A non-existent version should produce a clean error and non-zero exit."""
    result = runner.invoke(app, ["tool", "rollback", published_tool, "--to", "99"])
    assert result.exit_code != 0
    assert "no version 99" in result.stdout or "no version" in result.stdout


def test_tool_unlock_clears_lock_flag(published_tool: str) -> None:
    """``tool unlock`` clears ``improve_locked`` and resets the counter."""

    async def _lock() -> None:
        from sqlalchemy import select

        from jazz_guru.db import session_scope
        from jazz_guru.state import GeneratedTool

        async with session_scope() as s:
            tool = (
                await s.execute(
                    select(GeneratedTool).where(GeneratedTool.name == published_tool)
                )
            ).scalar_one()
            tool.meta = {"improve_locked": True, "consecutive_failures": 3}

    asyncio.run(_lock())
    _dispose_engine()
    result = runner.invoke(app, ["tool", "unlock", published_tool])
    assert result.exit_code == 0
    assert "unlocked" in result.stdout

    async def _read_meta() -> dict[str, object]:
        from sqlalchemy import select

        from jazz_guru.db import session_scope
        from jazz_guru.state import GeneratedTool

        async with session_scope() as s:
            tool = (
                await s.execute(
                    select(GeneratedTool).where(GeneratedTool.name == published_tool)
                )
            ).scalar_one()
            return dict(tool.meta or {})

    _dispose_engine()
    meta = asyncio.run(_read_meta())
    assert "improve_locked" not in meta
    assert meta["consecutive_failures"] == 0


def test_tool_unlock_unknown_tool() -> None:
    result = runner.invoke(app, ["tool", "unlock", "_t_definitely_not_there"])
    assert result.exit_code != 0


def test_tool_diff_between_versions(published_tool: str) -> None:
    """Bump the tool, then diff v1 against the current version."""

    async def _bump() -> None:
        await store.upsert(
            name=published_tool,
            description="cli test tool v2",
            input_schema={"type": "object", "additionalProperties": True},
            source="def run(**kwargs):\n    return {'ok': True, 'v': 2}\n",
        )

    asyncio.run(_bump())
    _dispose_engine()
    # v2 omitted = diff against current.
    result = runner.invoke(app, ["tool", "diff", published_tool, "1"])
    assert result.exit_code == 0, result.stdout
    # Diff should mention both versions in the header.
    assert "v1" in result.stdout
