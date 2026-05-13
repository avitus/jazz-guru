"""Tests for the Tier-2 tool improvement gate (plan §B.3-B.5).

Stubs ``llm.complete`` so each test pins one proposal shape and exercises
one branch of ``maybe_improve``. All DB operations run against real
Postgres; tools are uniquely named and cleaned up on teardown.
"""
from __future__ import annotations

import json
import uuid as uuid_mod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import select

from jazz_guru.actions import store
from jazz_guru.actions.tools.tool_test_meta import tool_test_add
from jazz_guru.db import session_scope
from jazz_guru.distillation import improver
from jazz_guru.distillation.improver import (
    MAX_ATTEMPTS,
    ImproveStatus,
    maybe_improve,
)
from jazz_guru.llm import LLMResponse, LLMUsage
from jazz_guru.state import GeneratedTool
from jazz_guru.testing.failure_signals import FailureRecord


def _unique(prefix: str) -> str:
    return f"_t_{prefix}_{uuid_mod.uuid4().hex[:10]}"


@asynccontextmanager
async def _tool(name: str, source: str | None = None) -> AsyncIterator[str]:
    src = source or "def run(**kwargs):\n    return {'ok': True}\n"
    await store.upsert(
        name=name,
        description="x",
        input_schema={"type": "object", "additionalProperties": True},
        source=src,
    )
    try:
        yield name
    finally:
        await store.remove(name)


def _fake_response(text: str) -> LLMResponse:
    return LLMResponse(raw=None, text=text, tool_uses=[], stop_reason="end_turn", usage=LLMUsage())


def _stub_complete(monkeypatch: pytest.MonkeyPatch, text: str) -> list[dict[str, Any]]:
    """Replace ``improver.complete`` with a stub that returns ``text``.

    Returns a list that captures the calls so tests can assert on them.
    """
    calls: list[dict[str, Any]] = []

    async def _fake(messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        calls.append({"messages": messages, **kwargs})
        return _fake_response(text)

    monkeypatch.setattr(improver, "complete", _fake)
    return calls


async def _set_meta(name: str, **fields: Any) -> None:
    async with session_scope() as s:
        tool = (
            await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
        ).scalar_one()
        meta = dict(tool.meta or {})
        meta.update(fields)
        tool.meta = meta


# ---------------------------------------------------- skip paths


async def test_skipped_when_tool_missing() -> None:
    """Unknown tool — nothing to do."""
    out = await maybe_improve("_t_truly_not_there", [])
    assert out.status == ImproveStatus.SKIPPED


async def test_skipped_when_no_tests() -> None:
    name = _unique("notests")
    async with _tool(name):
        out = await maybe_improve(name, [FailureRecord(tool_name=name, error="e")])
        assert out.status == ImproveStatus.SKIPPED_NO_TESTS


async def test_skipped_when_locked() -> None:
    """``improve_locked=True`` keeps the improver out — only the unlock CLI
    (or manual DB write) clears it."""
    name = _unique("locked")
    async with _tool(name):
        await tool_test_add(name=name, case_name="c", case_spec={"case": {"input": {}}})
        await _set_meta(name, improve_locked=True)
        out = await maybe_improve(name, [FailureRecord(tool_name=name, error="e")])
        assert out.status == ImproveStatus.SKIPPED_LOCKED


async def test_locks_when_max_attempts_exceeded() -> None:
    name = _unique("locknow")
    async with _tool(name):
        await tool_test_add(name=name, case_name="c", case_spec={"case": {"input": {}}})
        await _set_meta(name, consecutive_failures=MAX_ATTEMPTS)
        out = await maybe_improve(name, [FailureRecord(tool_name=name, error="e")])
        assert out.status == ImproveStatus.LOCKED_NOW
        # The lock is now persisted; subsequent runs see SKIPPED_LOCKED.
        out2 = await maybe_improve(name, [FailureRecord(tool_name=name, error="e")])
        assert out2.status == ImproveStatus.SKIPPED_LOCKED


# ---------------------------------------------------- proposal failure modes


async def test_propose_failed_on_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    name = _unique("badjson")
    async with _tool(name):
        await tool_test_add(name=name, case_name="c", case_spec={"case": {"input": {}}})
        _stub_complete(monkeypatch, "no json here, just prose")
        out = await maybe_improve(name, [FailureRecord(tool_name=name, error="e")])
        assert out.status == ImproveStatus.PROPOSE_FAILED


async def test_propose_failed_on_schema_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract requires schema_unchanged=true; a proposal saying
    otherwise is rejected without even running tests."""
    name = _unique("schemachange")
    async with _tool(name):
        await tool_test_add(name=name, case_name="c", case_spec={"case": {"input": {}}})
        _stub_complete(
            monkeypatch,
            json.dumps(
                {
                    "source": "def run(**kwargs):\n    return {'ok': True}\n",
                    "rationale": "r",
                    "new_test_cases": [],
                    "schema_unchanged": False,
                }
            ),
        )
        out = await maybe_improve(name, [FailureRecord(tool_name=name, error="e")])
        assert out.status == ImproveStatus.PROPOSE_FAILED


async def test_no_op_when_source_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """An echo-back proposal that doesn't actually change the source
    should be marked NO_OP rather than burning cycles on a redundant test
    run."""
    src = "def run(**kwargs):\n    return {'ok': True}\n"
    name = _unique("noop")
    async with _tool(name, source=src):
        await tool_test_add(name=name, case_name="c", case_spec={"case": {"input": {}}})
        _stub_complete(
            monkeypatch,
            json.dumps(
                {
                    "source": src,
                    "rationale": "no change",
                    "new_test_cases": [],
                    "schema_unchanged": True,
                }
            ),
        )
        out = await maybe_improve(name, [FailureRecord(tool_name=name, error="e")])
        assert out.status == ImproveStatus.NO_OP


# ---------------------------------------------------- tests-failed branch


async def test_tests_failed_bumps_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proposal that doesn't pass existing tests bumps the failure
    counter and leaves the live source unchanged."""
    name = _unique("testfail")
    async with _tool(name):
        await tool_test_add(
            name=name,
            case_name="strict",
            case_spec={
                "case": {
                    "input": {},
                    "predicate": {"result.ok": True},
                }
            },
        )
        # Proposal regresses the tool: returns {ok: False}, breaking the predicate.
        _stub_complete(
            monkeypatch,
            json.dumps(
                {
                    "source": "def run(**kwargs):\n    return {'ok': False}\n",
                    "rationale": "I made it worse",
                    "new_test_cases": [],
                    "schema_unchanged": True,
                }
            ),
        )
        out = await maybe_improve(name, [FailureRecord(tool_name=name, error="e")])
        assert out.status == ImproveStatus.TESTS_FAILED
        assert out.n_existing_fail == 1
        # The live tool should still be v1; no version bump.
        spec = await store.get_spec(name)
        assert spec.version == 1
        # Counter incremented.
        async with session_scope() as s:
            tool = (
                await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
            ).scalar_one()
            assert (tool.meta or {}).get("consecutive_failures") == 1


# ---------------------------------------------------- happy path


async def test_passed_commits_new_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """A green proposal lands as the next version with origin='improver'."""
    name = _unique("passed")
    async with _tool(name, source="def run(**kwargs):\n    return {'ok': True}\n"):
        await tool_test_add(
            name=name,
            case_name="smoke",
            case_spec={
                "case": {
                    "input": {},
                    "predicate": {"result.ok": True},
                }
            },
        )
        proposed = "def run(**kwargs):\n    # repaired\n    return {'ok': True, 'fixed': True}\n"
        _stub_complete(
            monkeypatch,
            json.dumps(
                {
                    "source": proposed,
                    "rationale": "Added the 'fixed' marker.",
                    "new_test_cases": [
                        {
                            "name": "regression_x",
                            "case": {
                                "input": {},
                                "predicate": {"result.fixed": True},
                            },
                        }
                    ],
                    "schema_unchanged": True,
                }
            ),
        )
        out = await maybe_improve(name, [FailureRecord(tool_name=name, error="e")])
        assert out.status == ImproveStatus.PASSED, out.failures
        assert out.new_version == 2
        assert out.n_new_cases == 1
        # Live source is now the proposed source under v2.
        spec = await store.get_spec(name)
        assert spec.version == 2
        assert "fixed" in spec.source
        # Snapshot of v1 lives in versions table with origin="improver".
        versions = await store.list_versions(name)
        assert len(versions) == 1
        assert versions[0].version == 1
        assert versions[0].origin == "improver"
        assert "fixed" not in versions[0].source
        # New regression case is attached with origin="improver_added".
        cases = await store.list_tests(name)
        names = {c.name: c for c in cases}
        assert "regression_x" in names
        assert names["regression_x"].origin == "improver_added"
        # consecutive_failures was reset (it was 0; success keeps it at 0).
        async with session_scope() as s:
            tool = (
                await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
            ).scalar_one()
            assert (tool.meta or {}).get("consecutive_failures") == 0


async def test_passed_resets_prior_failure_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    """A success after prior failed attempts clears the counter so future
    runs get the full MAX_ATTEMPTS budget again."""
    name = _unique("reset")
    async with _tool(name):
        await tool_test_add(
            name=name,
            case_name="smoke",
            case_spec={"case": {"input": {}, "predicate": {"result.ok": True}}},
        )
        await _set_meta(name, consecutive_failures=2)
        _stub_complete(
            monkeypatch,
            json.dumps(
                {
                    "source": "def run(**kwargs):\n    # rev\n    return {'ok': True}\n",
                    "rationale": "fix",
                    "new_test_cases": [],
                    "schema_unchanged": True,
                }
            ),
        )
        out = await maybe_improve(name, [FailureRecord(tool_name=name, error="e")])
        assert out.status == ImproveStatus.PASSED
        async with session_scope() as s:
            tool = (
                await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
            ).scalar_one()
            assert (tool.meta or {}).get("consecutive_failures") == 0


async def test_propose_failed_on_invalid_python(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proposal whose Python source doesn't parse is caught by
    ``validate_source`` before any test run."""
    name = _unique("invalidpy")
    async with _tool(name):
        await tool_test_add(name=name, case_name="c", case_spec={"case": {"input": {}}})
        _stub_complete(
            monkeypatch,
            json.dumps(
                {
                    "source": "def run(:\n  pass\n",
                    "rationale": "broken",
                    "new_test_cases": [],
                    "schema_unchanged": True,
                }
            ),
        )
        out = await maybe_improve(name, [FailureRecord(tool_name=name, error="e")])
        assert out.status == ImproveStatus.PROPOSE_FAILED
