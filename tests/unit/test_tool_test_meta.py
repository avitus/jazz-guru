"""End-to-end tests for the ``tool_test_*`` meta-tools.

Uses real Postgres + real subprocess invocations. Each test creates a
uniquely-named Tier-2 tool, exercises one or more meta-tools, and cleans
up via ``store.remove`` (which cascades through tests and runs).
"""
from __future__ import annotations

import uuid as uuid_mod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import select

from jazz_guru.actions import store
from jazz_guru.actions.tools.tool_test_meta import (
    tool_test_add,
    tool_test_list,
    tool_test_remove,
    tool_test_run,
)
from jazz_guru.db import session_scope
from jazz_guru.state import GeneratedToolTestRun


def _unique(prefix: str) -> str:
    return f"_t_{prefix}_{uuid_mod.uuid4().hex[:10]}"


@asynccontextmanager
async def _tool(name: str, source: str | None = None) -> AsyncIterator[str]:
    """Publish a tool, yield its name, always clean up."""
    src = source or "def run(**kwargs):\n    return {'ok': True}\n"
    await store.upsert(
        name=name,
        description="x",
        input_schema={"type": "object", "properties": {}, "additionalProperties": True},
        source=src,
    )
    try:
        yield name
    finally:
        await store.remove(name)


# ---------------------------------------------------- tool_test_add


async def test_add_creates_case() -> None:
    name = _unique("add")
    async with _tool(name):
        r = await tool_test_add(
            name=name,
            case_name="smoke",
            case_spec={"case": {"input": {}, "predicate": {"result.ok": True}}},
        )
        assert r["ok"] is True
        assert r["status"] == "created"


async def test_add_overwrites_existing_case() -> None:
    name = _unique("addup")
    async with _tool(name):
        await tool_test_add(
            name=name, case_name="smoke", case_spec={"case": {"input": {}}}
        )
        r = await tool_test_add(
            name=name,
            case_name="smoke",
            case_spec={"case": {"input": {"x": 1}}},
        )
        assert r["status"] == "updated"


async def test_add_rejects_bad_case_name() -> None:
    name = _unique("badname")
    async with _tool(name):
        r = await tool_test_add(
            name=name,
            case_name="Bad Name",
            case_spec={"case": {"input": {}}},
        )
        assert r["ok"] is False
        assert "invalid case name" in r["error"]


async def test_add_unknown_tool() -> None:
    r = await tool_test_add(
        name="_t_does_not_exist",
        case_name="smoke",
        case_spec={"case": {"input": {}}},
    )
    assert r["ok"] is False
    assert "unknown tool" in r["error"]


# ---------------------------------------------------- tool_test_remove


async def test_remove_deletes_case() -> None:
    name = _unique("rm")
    async with _tool(name):
        await tool_test_add(name=name, case_name="c1", case_spec={"case": {"input": {}}})
        r = await tool_test_remove(name=name, case_name="c1")
        assert r["ok"] is True
        assert r["status"] == "removed"


async def test_remove_disable_only() -> None:
    """disable_only preserves the row (and FKs to test_runs) but mutes it."""
    name = _unique("disable")
    async with _tool(name):
        await tool_test_add(name=name, case_name="c1", case_spec={"case": {"input": {}}})
        r = await tool_test_remove(name=name, case_name="c1", disable_only=True)
        assert r["status"] == "disabled"

        listed = await tool_test_list(name=name, include_disabled=False)
        assert listed["cases"] == []  # filtered out
        listed_all = await tool_test_list(name=name, include_disabled=True)
        assert len(listed_all["cases"]) == 1
        assert listed_all["cases"][0]["enabled"] is False


async def test_remove_unknown_case() -> None:
    name = _unique("rmbad")
    async with _tool(name):
        r = await tool_test_remove(name=name, case_name="nope")
        assert r["ok"] is False


# ---------------------------------------------------- tool_test_list


async def test_list_empty_for_new_tool() -> None:
    name = _unique("listempty")
    async with _tool(name):
        r = await tool_test_list(name=name)
        assert r == {"ok": True, "name": name, "cases": []}


async def test_list_shows_added_cases() -> None:
    name = _unique("listadd")
    async with _tool(name):
        await tool_test_add(name=name, case_name="a", case_spec={"case": {"input": {}}})
        await tool_test_add(name=name, case_name="b", case_spec={"case": {"input": {}}})
        r = await tool_test_list(name=name)
        assert [c["name"] for c in r["cases"]] == ["a", "b"]
        # last_run is None for cases that haven't been run yet.
        assert all(c["last_run"] is None for c in r["cases"])


# ---------------------------------------------------- tool_test_run


async def test_run_passes_when_predicate_matches() -> None:
    name = _unique("runok")
    src = "def run(**kwargs):\n    return {'sum': kwargs.get('a', 0) + kwargs.get('b', 0)}\n"
    async with _tool(name, source=src):
        await tool_test_add(
            name=name,
            case_name="adder",
            case_spec={
                "case": {
                    "input": {"a": 2, "b": 3},
                    "predicate": {"result.sum": 5},
                }
            },
        )
        r = await tool_test_run(name=name)
        assert r["ok"] is True
        assert r["total"] == 1
        assert r["passed"] == 1
        assert r["failed"] == 0
        assert r["cases"][0]["passed"] is True


async def test_run_records_failures() -> None:
    name = _unique("runfail")
    src = "def run(**kwargs):\n    return {'sum': 0}\n"
    async with _tool(name, source=src):
        await tool_test_add(
            name=name,
            case_name="wrong",
            case_spec={
                "case": {"input": {}, "predicate": {"result.sum": 999}},
            },
        )
        r = await tool_test_run(name=name)
        assert r["passed"] == 0
        assert r["failed"] == 1
        assert r["cases"][0]["failures"]  # non-empty


async def test_run_persists_runs_to_db() -> None:
    """run rows accumulate in generated_tool_test_runs for later audit."""
    name = _unique("persist")
    async with _tool(name):
        await tool_test_add(
            name=name,
            case_name="smoke",
            case_spec={"case": {"input": {}}},
        )
        await tool_test_run(name=name)
        await tool_test_run(name=name)  # second run

        async with session_scope() as s:
            spec = await store.get_spec(name)
            assert spec is not None
            cases = await store.list_tests(name)
            assert len(cases) == 1
            runs = (
                await s.execute(
                    select(GeneratedToolTestRun)
                    .where(GeneratedToolTestRun.test_id == cases[0].id)
                    .order_by(GeneratedToolTestRun.ran_at.asc())
                )
            ).scalars().all()
            assert len(runs) == 2
            assert all(r.passed for r in runs)
            assert all(r.tool_id == cases[0].tool_id for r in runs)


async def test_run_specific_case_only() -> None:
    """``case_name`` filter scopes the run to one case."""
    name = _unique("runone")
    async with _tool(name):
        await tool_test_add(name=name, case_name="a", case_spec={"case": {"input": {}}})
        await tool_test_add(name=name, case_name="b", case_spec={"case": {"input": {}}})
        r = await tool_test_run(name=name, case_name="a")
        assert r["total"] == 1
        assert r["cases"][0]["name"] == "a"


async def test_run_unknown_case_name() -> None:
    name = _unique("runnocase")
    async with _tool(name):
        await tool_test_add(name=name, case_name="x", case_spec={"case": {"input": {}}})
        r = await tool_test_run(name=name, case_name="nope")
        assert r["ok"] is False
        assert "no enabled case" in r["error"]


async def test_run_skips_disabled_cases() -> None:
    name = _unique("skipdis")
    async with _tool(name):
        await tool_test_add(name=name, case_name="a", case_spec={"case": {"input": {}}})
        await tool_test_add(
            name=name, case_name="b", case_spec={"case": {"input": {}}}, enabled=False
        )
        r = await tool_test_run(name=name)
        # Only the enabled case ran.
        assert r["total"] == 1
        assert r["cases"][0]["name"] == "a"


async def test_run_no_tests_returns_zero_total() -> None:
    """Plan §A.9: tools with no tests are skipped silently by the improver,
    but tool_test_run should still return a clean structured response."""
    name = _unique("runnoone")
    async with _tool(name):
        r = await tool_test_run(name=name)
        assert r["ok"] is True
        assert r["total"] == 0
        assert r["cases"] == []
        assert "no enabled cases" in (r.get("note") or "")


async def test_run_unknown_tool() -> None:
    r = await tool_test_run(name="_t_truly_not_there")
    assert r["ok"] is False
    assert "unknown tool" in r["error"]


# ---------------------------------------------------- input_schema


async def test_add_input_schema_is_normalized() -> None:
    """Registry/schema layer should accept this without explicit normalization."""
    name = _unique("schemashape")
    async with _tool(name):
        # Pass an unusually-shaped case_spec to verify acceptance.
        r = await tool_test_add(
            name=name,
            case_name="any",
            case_spec={"case": {"input": {"x": [1, 2]}, "predicate": {"result": {"present": True}}}},
        )
        assert r["ok"] is True


async def _make_random_test(name: str, case_name: str, payload: dict[str, Any]) -> None:
    """Small helper for parametric tests."""
    await tool_test_add(name=name, case_name=case_name, case_spec=payload)
