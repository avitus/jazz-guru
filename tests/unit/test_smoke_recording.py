"""Tests for the smoke-recording path in ``tool_publish``.

Two layers:
- ``_find_last_successful_invocation`` — pure trace mining over synthesized
  JSONL fixtures, no DB. Exercises the pairing / success-detection logic.
- ``_record_smoke_case`` — DB round-trip against the real Postgres, with
  cleanup via ``store.remove`` cascade.
"""
from __future__ import annotations

import json
import uuid as uuid_mod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from jazz_guru.actions import store
from jazz_guru.actions.tools.tool_meta import (
    _find_last_successful_invocation,
    _record_smoke_case,
)
from jazz_guru.config import get_settings
from jazz_guru.db import session_scope
from jazz_guru.state import GeneratedTool

# ---------------------------------------------------- trace fixtures


def _write_trace(sid: str, events: list[dict[str, object]]) -> Path:
    """Drop a synthesized trace under ``workspace/traces/<sid>.jsonl``."""
    trace_dir = Path(get_settings().jg_trace_dir)
    trace_dir.mkdir(parents=True, exist_ok=True)
    p = trace_dir / f"{sid}.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return p


def _ts() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------- _find_last_*


def test_find_returns_none_without_session_id() -> None:
    assert _find_last_successful_invocation("foo", None) is None


def test_find_returns_none_for_missing_trace_file() -> None:
    sid = f"sid_{uuid_mod.uuid4().hex[:8]}"
    # No trace written → file doesn't exist; no smoke case to derive.
    assert _find_last_successful_invocation("foo", sid) is None


def test_find_returns_none_when_only_failures_recorded() -> None:
    sid = f"sid_{uuid_mod.uuid4().hex[:8]}"
    _write_trace(sid, [
        {"ts": _ts(), "type": "tool_use",
         "payload": {"id": "1", "name": "foo", "input": {"a": 1}}},
        {"ts": _ts(), "type": "tool_result",
         "payload": {"id": "1", "name": "foo", "ok": False, "error": "boom"}},
    ])
    try:
        assert _find_last_successful_invocation("foo", sid) is None
    finally:
        (Path(get_settings().jg_trace_dir) / f"{sid}.jsonl").unlink(missing_ok=True)


def test_find_picks_last_success_over_earlier_failure() -> None:
    """Most-recent semantics — even if an earlier call failed, a later
    successful call is what we record."""
    sid = f"sid_{uuid_mod.uuid4().hex[:8]}"
    _write_trace(sid, [
        # call 1: failed
        {"ts": _ts(), "type": "tool_use",
         "payload": {"id": "1", "name": "foo", "input": {"a": 1}}},
        {"ts": _ts(), "type": "tool_result",
         "payload": {"id": "1", "name": "foo", "ok": False, "error": "err"}},
        # call 2: succeeded — this is the one we want
        {"ts": _ts(), "type": "tool_use",
         "payload": {"id": "2", "name": "foo", "input": {"a": 42}}},
        {"ts": _ts(), "type": "tool_result",
         "payload": {"id": "2", "name": "foo", "result": {"x": 1}}},
    ])
    try:
        assert _find_last_successful_invocation("foo", sid) == {"a": 42}
    finally:
        (Path(get_settings().jg_trace_dir) / f"{sid}.jsonl").unlink(missing_ok=True)


def test_find_ignores_other_tool_names() -> None:
    sid = f"sid_{uuid_mod.uuid4().hex[:8]}"
    _write_trace(sid, [
        {"ts": _ts(), "type": "tool_use",
         "payload": {"id": "1", "name": "other", "input": {"q": "x"}}},
        {"ts": _ts(), "type": "tool_result",
         "payload": {"id": "1", "name": "other"}},
    ])
    try:
        assert _find_last_successful_invocation("foo", sid) is None
    finally:
        (Path(get_settings().jg_trace_dir) / f"{sid}.jsonl").unlink(missing_ok=True)


def test_find_skips_tool_use_without_matching_result() -> None:
    """A tool_use without a paired tool_result is incomplete; skip it."""
    sid = f"sid_{uuid_mod.uuid4().hex[:8]}"
    _write_trace(sid, [
        {"ts": _ts(), "type": "tool_use",
         "payload": {"id": "abandoned", "name": "foo", "input": {"a": 1}}},
        # no result for abandoned
        {"ts": _ts(), "type": "tool_use",
         "payload": {"id": "2", "name": "foo", "input": {"a": 99}}},
        {"ts": _ts(), "type": "tool_result",
         "payload": {"id": "2", "name": "foo"}},
    ])
    try:
        # Most recent paired success.
        assert _find_last_successful_invocation("foo", sid) == {"a": 99}
    finally:
        (Path(get_settings().jg_trace_dir) / f"{sid}.jsonl").unlink(missing_ok=True)


def test_find_returns_empty_dict_when_input_missing() -> None:
    """A successful tool_use with no input still records a (vacuous) smoke."""
    sid = f"sid_{uuid_mod.uuid4().hex[:8]}"
    _write_trace(sid, [
        {"ts": _ts(), "type": "tool_use",
         "payload": {"id": "1", "name": "foo"}},
        {"ts": _ts(), "type": "tool_result",
         "payload": {"id": "1", "name": "foo"}},
    ])
    try:
        assert _find_last_successful_invocation("foo", sid) == {}
    finally:
        (Path(get_settings().jg_trace_dir) / f"{sid}.jsonl").unlink(missing_ok=True)


def test_find_skips_malformed_jsonl_lines() -> None:
    """A corrupt JSONL line shouldn't abort the whole scan."""
    sid = f"sid_{uuid_mod.uuid4().hex[:8]}"
    trace_dir = Path(get_settings().jg_trace_dir)
    trace_dir.mkdir(parents=True, exist_ok=True)
    p = trace_dir / f"{sid}.jsonl"
    with p.open("w", encoding="utf-8") as f:
        f.write("{not json}\n")
        f.write(json.dumps({"ts": _ts(), "type": "tool_use",
                            "payload": {"id": "1", "name": "foo", "input": {"x": 1}}}) + "\n")
        f.write(json.dumps({"ts": _ts(), "type": "tool_result",
                            "payload": {"id": "1", "name": "foo"}}) + "\n")
    try:
        assert _find_last_successful_invocation("foo", sid) == {"x": 1}
    finally:
        p.unlink(missing_ok=True)


# ---------------------------------------------------- _record_smoke_case


def _unique(prefix: str) -> str:
    return f"_t_{prefix}_{uuid_mod.uuid4().hex[:10]}"


@asynccontextmanager
async def _tool(name: str) -> AsyncIterator[str]:
    await store.upsert(
        name=name,
        description="x",
        input_schema={"type": "object", "properties": {}, "additionalProperties": True},
        source="def run(**kwargs):\n    return {'ok': True}\n",
    )
    try:
        yield name
    finally:
        await store.remove(name)


async def test_record_smoke_creates_case() -> None:
    name = _unique("smoke")
    async with _tool(name):
        async with session_scope() as s:
            tool = (
                await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
            ).scalar_one()
            tool_id = tool.id
        await _record_smoke_case(tool_id, {"a": 5})
        cases = await store.list_tests(name)
        assert len(cases) == 1
        c = cases[0]
        assert c.name == "smoke_recorded"
        assert c.origin == "smoke_recorded"
        assert c.spec["case"]["input"] == {"a": 5}
        # Default predicate asserts the result is an object with no __error__.
        assert "result.__error__" in c.spec["case"]["predicate"]


async def test_record_smoke_is_idempotent_on_replay() -> None:
    """Re-publishing after a fix should refresh the smoke baseline, not
    duplicate the row (which would violate the uniqueness constraint)."""
    name = _unique("idemp")
    async with _tool(name):
        async with session_scope() as s:
            tool = (
                await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
            ).scalar_one()
            tool_id = tool.id
        await _record_smoke_case(tool_id, {"a": 1})
        await _record_smoke_case(tool_id, {"a": 2})
        cases = await store.list_tests(name)
        assert len(cases) == 1
        assert cases[0].spec["case"]["input"] == {"a": 2}


async def test_record_smoke_default_predicate_passes_against_typical_output() -> None:
    """A tool that returns a dict-without-__error__ should pass the smoke
    case the helper synthesizes — that's the whole point of the default."""
    from jazz_guru.testing.predicates import evaluate

    name = _unique("smokeok")
    async with _tool(name):
        async with session_scope() as s:
            tool = (
                await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
            ).scalar_one()
            tool_id = tool.id
        await _record_smoke_case(tool_id, {})
        cases = await store.list_tests(name)
        predicate = cases[0].spec["case"]["predicate"]
        # The tool returns {'ok': True}. Wrapped under "result" for the
        # path syntax, the synthesized predicate should pass.
        r = evaluate({"result": {"ok": True}}, predicate)
        assert r.passed, r.failures
        # And fail on an error result.
        r = evaluate({"result": {"__error__": "boom"}}, predicate)
        assert not r.passed
