"""Integration tests for the reflexion → improver pass (plan §B.2).

Reflexion's main work (memory writes, playbook entries, snapshots) is
covered by existing tests; here we exercise only the new improvement
pass at the tail of ``run_reflexion``. ``_run_improvement_pass`` is
the seam we call directly with a synthesized trace and stubbed LLM
client.
"""
from __future__ import annotations

import json
import uuid as uuid_mod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from jazz_guru.actions import store
from jazz_guru.actions.tools.tool_test_meta import tool_test_add
from jazz_guru.config import get_settings
from jazz_guru.db import session_scope
from jazz_guru.distillation import improver, reflexion
from jazz_guru.llm import LLMResponse, LLMUsage
from jazz_guru.state import Event, EventType, GeneratedTool, Session


def _unique(prefix: str) -> str:
    return f"_t_{prefix}_{uuid_mod.uuid4().hex[:10]}"


async def _make_session() -> uuid_mod.UUID:
    """Create a real Session row so log_event's FK is satisfied."""
    sid = uuid_mod.uuid4()
    async with session_scope() as s:
        s.add(Session(id=sid))
    return sid


async def _delete_session(sid: uuid_mod.UUID) -> None:
    """CASCADE on events FK drops the per-session log too."""
    async with session_scope() as s:
        row = (
            await s.execute(select(Session).where(Session.id == sid))
        ).scalar_one_or_none()
        if row is not None:
            await s.delete(row)


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


def _write_trace(sid: uuid_mod.UUID, events: list[dict[str, Any]]) -> Path:
    trace_dir = Path(get_settings().jg_trace_dir)
    trace_dir.mkdir(parents=True, exist_ok=True)
    p = trace_dir / f"{sid}.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return p


def _rec(type_: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"ts": datetime.now(UTC).isoformat(), "type": type_, "payload": payload}


def _stub_complete(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    async def _fake(messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        return LLMResponse(
            raw=None, text=text, tool_uses=[], stop_reason="end_turn", usage=LLMUsage()
        )

    monkeypatch.setattr(improver, "complete", _fake)


async def _events_for_session(session_id: uuid_mod.UUID) -> list[Event]:
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Event)
                .where(Event.session_id == session_id)
                .order_by(Event.ts.asc())
            )
        ).scalars().all()
        return list(rows)


# ---------------------------------------------------- no-op cases


async def test_no_trace_file_returns_quietly() -> None:
    """A session that never produced a trace shouldn't be a problem."""
    sid = uuid_mod.uuid4()
    # Should not raise.
    await reflexion._run_improvement_pass(sid)


async def test_failures_below_threshold_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One failure < threshold of 2 → no LLM call, no event."""
    sid = uuid_mod.uuid4()
    name = _unique("subthresh")
    async with _tool(name):
        await tool_test_add(name=name, case_name="c", case_spec={"case": {"input": {}}})
        p = _write_trace(
            sid,
            [
                _rec("tool_use", {"id": "1", "name": name, "input": {"x": 1}}),
                _rec("tool_result", {"id": "1", "name": name, "ok": False, "error": "e"}),
            ],
        )
        # Stub the LLM so any unexpected call is loud.
        called = {"n": 0}

        async def _trap(*a: Any, **kw: Any) -> LLMResponse:
            called["n"] += 1
            return LLMResponse(
                raw=None, text="", tool_uses=[], stop_reason="end_turn", usage=LLMUsage()
            )

        monkeypatch.setattr(improver, "complete", _trap)
        try:
            await reflexion._run_improvement_pass(sid)
            assert called["n"] == 0
            events = await _events_for_session(sid)
            assert events == []
        finally:
            p.unlink(missing_ok=True)
            await _delete_session(sid)


async def test_locked_tool_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ``improve_locked`` tool above threshold is NOT proposed."""
    sid = await _make_session()
    name = _unique("locktest")
    async with _tool(name):
        await tool_test_add(name=name, case_name="c", case_spec={"case": {"input": {}}})
        # Manually lock.
        async with session_scope() as s:
            tool = (
                await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
            ).scalar_one()
            tool.meta = {"improve_locked": True}
        p = _write_trace(
            sid,
            [
                # Two failures = threshold met
                _rec("tool_use", {"id": "1", "name": name, "input": {"x": 1}}),
                _rec("tool_result", {"id": "1", "name": name, "ok": False, "error": "e1"}),
                _rec("tool_use", {"id": "2", "name": name, "input": {"x": 2}}),
                _rec("tool_result", {"id": "2", "name": name, "ok": False, "error": "e2"}),
            ],
        )
        called = {"n": 0}

        async def _trap(*a: Any, **kw: Any) -> LLMResponse:
            called["n"] += 1
            return LLMResponse(
                raw=None, text="", tool_uses=[], stop_reason="end_turn", usage=LLMUsage()
            )

        monkeypatch.setattr(improver, "complete", _trap)
        try:
            await reflexion._run_improvement_pass(sid)
            # PROPOSED event is still logged (we recorded "we tried"),
            # but skipped/locked outcomes don't emit a follow-up event.
            events = await _events_for_session(sid)
            assert any(
                e.type == EventType.TOOL_IMPROVE_PROPOSED.value for e in events
            )
            assert not any(
                e.type == EventType.TOOL_IMPROVE_PASSED.value for e in events
            )
            assert not any(
                e.type == EventType.TOOL_IMPROVE_FAILED.value for e in events
            )
        finally:
            p.unlink(missing_ok=True)
            await _delete_session(sid)


# ---------------------------------------------------- happy / sad paths


async def test_passed_emits_improve_passed_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """A green proposal logs TOOL_IMPROVE_PROPOSED + TOOL_IMPROVE_PASSED."""
    sid = await _make_session()
    name = _unique("passevent")
    src = "def run(**kwargs):\n    return {'ok': True}\n"
    async with _tool(name, source=src):
        await tool_test_add(
            name=name,
            case_name="smoke",
            case_spec={"case": {"input": {}, "predicate": {"result.ok": True}}},
        )
        p = _write_trace(
            sid,
            [
                _rec("tool_use", {"id": "1", "name": name, "input": {"x": 1}}),
                _rec("tool_result", {"id": "1", "name": name, "ok": False, "error": "e1"}),
                _rec("tool_use", {"id": "2", "name": name, "input": {"x": 2}}),
                _rec("tool_result", {"id": "2", "name": name, "ok": False, "error": "e2"}),
            ],
        )
        new_source = "def run(**kwargs):\n    # fixed\n    return {'ok': True}\n"
        _stub_complete(
            monkeypatch,
            json.dumps(
                {
                    "source": new_source,
                    "rationale": "added a comment as a fix marker",
                    "new_test_cases": [],
                    "schema_unchanged": True,
                }
            ),
        )
        try:
            await reflexion._run_improvement_pass(sid)
            events = await _events_for_session(sid)
            assert any(
                e.type == EventType.TOOL_IMPROVE_PROPOSED.value for e in events
            )
            passed = [
                e for e in events if e.type == EventType.TOOL_IMPROVE_PASSED.value
            ]
            assert len(passed) == 1
            assert passed[0].payload["name"] == name
            assert passed[0].payload["version_new"] == 2
        finally:
            p.unlink(missing_ok=True)
            await _delete_session(sid)


async def test_tests_failed_emits_improve_failed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A red proposal logs PROPOSED + FAILED, leaves the tool alone."""
    sid = await _make_session()
    name = _unique("failevent")
    async with _tool(name):
        await tool_test_add(
            name=name,
            case_name="strict",
            case_spec={"case": {"input": {}, "predicate": {"result.ok": True}}},
        )
        p = _write_trace(
            sid,
            [
                _rec("tool_use", {"id": "1", "name": name, "input": {}}),
                _rec("tool_result", {"id": "1", "name": name, "ok": False, "error": "e"}),
                _rec("tool_use", {"id": "2", "name": name, "input": {}}),
                _rec("tool_result", {"id": "2", "name": name, "ok": False, "error": "e"}),
            ],
        )
        # Proposal regresses the tool — predicate result.ok=True won't hold.
        _stub_complete(
            monkeypatch,
            json.dumps(
                {
                    "source": "def run(**kwargs):\n    return {'ok': False}\n",
                    "rationale": "wrong",
                    "new_test_cases": [],
                    "schema_unchanged": True,
                }
            ),
        )
        try:
            await reflexion._run_improvement_pass(sid)
            events = await _events_for_session(sid)
            failed = [
                e for e in events if e.type == EventType.TOOL_IMPROVE_FAILED.value
            ]
            assert len(failed) == 1
            assert failed[0].payload["name"] == name
        finally:
            p.unlink(missing_ok=True)
            await _delete_session(sid)


async def test_per_run_cap_limits_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """The global cap stops the loop after N maybe_improve calls, even if
    more tools crossed their thresholds."""
    sid = await _make_session()
    names = [_unique(f"cap{i}") for i in range(3)]
    # Cap to 1 attempt this run.
    monkeypatch.setattr(get_settings(), "jg_improver_max_per_run", 1)

    # Set up three tools each with a test + two failures in the trace.
    events_for_trace: list[dict[str, Any]] = []
    for i, n in enumerate(names):
        await store.upsert(
            name=n,
            description="x",
            input_schema={"type": "object", "additionalProperties": True},
            source="def run(**kwargs):\n    return {'ok': True}\n",
        )
        await tool_test_add(
            name=n,
            case_name="smoke",
            case_spec={"case": {"input": {}, "predicate": {"result.ok": True}}},
        )
        for j in range(2):
            uid = f"u{i}{j}"
            events_for_trace.append(
                _rec("tool_use", {"id": uid, "name": n, "input": {}})
            )
            events_for_trace.append(
                _rec(
                    "tool_result",
                    {"id": uid, "name": n, "ok": False, "error": f"e{j}"},
                )
            )
    p = _write_trace(sid, events_for_trace)

    n_calls = {"count": 0}

    async def _stub(messages: list[dict[str, Any]], **kw: Any) -> LLMResponse:
        n_calls["count"] += 1
        return LLMResponse(
            raw=None,
            text=json.dumps(
                {
                    "source": (
                        f"def run(**kwargs):\n    # v{n_calls['count']}\n    return {{'ok': True}}\n"
                    ),
                    "rationale": "ok",
                    "new_test_cases": [],
                    "schema_unchanged": True,
                }
            ),
            tool_uses=[],
            stop_reason="end_turn",
            usage=LLMUsage(),
        )

    monkeypatch.setattr(improver, "complete", _stub)
    try:
        await reflexion._run_improvement_pass(sid)
        # Despite three eligible tools, only one proposal call was made.
        assert n_calls["count"] == 1
    finally:
        p.unlink(missing_ok=True)
        for n in names:
            await store.remove(n)
        await _delete_session(sid)
