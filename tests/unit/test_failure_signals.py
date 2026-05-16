"""Tests for the trace-mining failure extractor.

Synthesizes JSONL trace records (no DB, no real agent runs) and asserts
the extractor catches each documented failure mode without flagging the
happy path.
"""
from __future__ import annotations

import json
import uuid as uuid_mod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jazz_guru.config import get_settings
from jazz_guru.testing.failure_signals import (
    extract_from_session,
    extract_tool_failures,
)


def _ts() -> str:
    return datetime.now(UTC).isoformat()


def _rec(type_: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"ts": _ts(), "type": type_, "payload": payload}


# ---------------------------------------------------- happy path


def test_success_is_not_a_failure() -> None:
    """``ok: True`` without error markers means no failure to record."""
    records = [
        _rec("tool_use", {"id": "1", "name": "foo", "input": {"x": 1}}),
        _rec("tool_result", {"id": "1", "name": "foo", "ok": True}),
    ]
    assert extract_tool_failures(records) == {}


def test_empty_trace_returns_empty_dict() -> None:
    assert extract_tool_failures([]) == {}


# ---------------------------------------------------- raised


def test_handler_exception_is_recorded() -> None:
    """``ok: False`` with an ``error`` field → kind='raised'."""
    records = [
        _rec("tool_use", {"id": "1", "name": "foo", "input": {"x": 1}}),
        _rec(
            "tool_result",
            {"id": "1", "name": "foo", "ok": False, "error": "ValueError: boom"},
        ),
    ]
    out = extract_tool_failures(records)
    assert list(out) == ["foo"]
    assert len(out["foo"]) == 1
    fr = out["foo"][0]
    assert fr.kind == "raised"
    assert fr.input == {"x": 1}
    assert fr.error == "ValueError: boom"
    assert fr.tool_use_id == "1"


# ---------------------------------------------------- policy


def test_policy_denial_is_recorded() -> None:
    """Policy denial: error key, no ``ok`` field."""
    records = [
        _rec("tool_use", {"id": "1", "name": "foo", "input": {"x": 1}}),
        _rec(
            "tool_result",
            {"id": "1", "name": "foo", "error": "tool 'foo' not allowed by policy"},
        ),
    ]
    out = extract_tool_failures(records)
    assert out["foo"][0].kind == "policy"
    assert "not allowed" in (out["foo"][0].error or "")


# ---------------------------------------------------- result_error


def test_subprocess_error_envelope_is_recorded() -> None:
    """``result_has_error: True`` → the tool returned an ``__error__`` envelope."""
    records = [
        _rec("tool_use", {"id": "1", "name": "lick_lookup", "input": {"chord": "X"}}),
        _rec(
            "tool_result",
            {
                "id": "1",
                "name": "lick_lookup",
                "ok": True,
                "result_has_error": True,
                "error_excerpt": "timeout after 30s",
            },
        ),
    ]
    out = extract_tool_failures(records)
    assert out["lick_lookup"][0].kind == "result_error"
    assert out["lick_lookup"][0].error == "timeout after 30s"


# ---------------------------------------------------- grouping


def test_failures_grouped_by_tool_in_order() -> None:
    """Multiple failures preserve in-trace order within a tool's group."""
    records = [
        _rec("tool_use", {"id": "1", "name": "foo", "input": {"x": 1}}),
        _rec("tool_result", {"id": "1", "name": "foo", "ok": False, "error": "e1"}),
        _rec("tool_use", {"id": "2", "name": "bar", "input": {"y": 2}}),
        _rec("tool_result", {"id": "2", "name": "bar", "ok": False, "error": "e2"}),
        _rec("tool_use", {"id": "3", "name": "foo", "input": {"x": 99}}),
        _rec("tool_result", {"id": "3", "name": "foo", "ok": False, "error": "e3"}),
    ]
    out = extract_tool_failures(records)
    assert set(out) == {"foo", "bar"}
    assert [f.tool_use_id for f in out["foo"]] == ["1", "3"]
    assert [f.error for f in out["foo"]] == ["e1", "e3"]


def test_mixed_success_and_failure_only_reports_failure() -> None:
    records = [
        # one good, one bad — only the bad is in the output
        _rec("tool_use", {"id": "good", "name": "foo", "input": {}}),
        _rec("tool_result", {"id": "good", "name": "foo", "ok": True}),
        _rec("tool_use", {"id": "bad", "name": "foo", "input": {"q": "x"}}),
        _rec("tool_result", {"id": "bad", "name": "foo", "ok": False, "error": "boom"}),
    ]
    out = extract_tool_failures(records)
    assert len(out["foo"]) == 1
    assert out["foo"][0].tool_use_id == "bad"


def test_orphan_tool_result_handled_gracefully() -> None:
    """A tool_result without a matching tool_use shouldn't crash — input stays empty."""
    records = [
        _rec("tool_result", {"id": "ghost", "name": "foo", "ok": False, "error": "x"}),
    ]
    out = extract_tool_failures(records)
    assert out["foo"][0].input == {}
    assert out["foo"][0].error == "x"


def test_malformed_records_skipped() -> None:
    """A bad record shouldn't taint the rest of the parse."""
    records: list[dict[str, Any]] = [
        {"type": "tool_use", "payload": {"id": "1", "name": "foo", "input": {"x": 1}}},
        {"type": "tool_result"},  # missing payload entirely
        {"type": "tool_result", "payload": {"name": "foo", "ok": False, "error": "ok"}},  # no id
        {"type": "tool_use", "payload": "not-a-dict"},  # bad payload type
    ]
    out = extract_tool_failures(records)
    # The one valid failure (no id, but name and error) is still recorded;
    # input couldn't be paired so it's empty.
    assert out["foo"][0].input == {}
    assert out["foo"][0].error == "ok"


# ---------------------------------------------------- extract_from_session


def test_extract_from_session_round_trip() -> None:
    """End-to-end: write a real JSONL file, read it via session id."""
    sid = uuid_mod.uuid4()
    trace_dir = Path(get_settings().jg_trace_dir)
    trace_dir.mkdir(parents=True, exist_ok=True)
    p = trace_dir / f"{sid}.jsonl"
    events = [
        _rec("tool_use", {"id": "1", "name": "foo", "input": {"q": 1}}),
        _rec("tool_result", {"id": "1", "name": "foo", "ok": False, "error": "e"}),
    ]
    with p.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    try:
        out = extract_from_session(sid)
        assert list(out) == ["foo"]
        assert out["foo"][0].error == "e"
    finally:
        p.unlink(missing_ok=True)


def test_extract_from_session_missing_file_returns_empty() -> None:
    sid = uuid_mod.uuid4()
    # No file written.
    assert extract_from_session(sid) == {}


def test_extract_from_session_bad_uuid_returns_empty() -> None:
    """Defensive: a non-UUID string shouldn't escape the trace dir."""
    assert extract_from_session("../../etc/passwd") == {}


def test_extract_from_session_skips_corrupt_lines() -> None:
    sid = uuid_mod.uuid4()
    trace_dir = Path(get_settings().jg_trace_dir)
    trace_dir.mkdir(parents=True, exist_ok=True)
    p = trace_dir / f"{sid}.jsonl"
    with p.open("w", encoding="utf-8") as f:
        f.write("{not json}\n")
        f.write(
            json.dumps(_rec("tool_result", {"id": "1", "name": "foo", "ok": False, "error": "x"}))
            + "\n"
        )
    try:
        out = extract_from_session(sid)
        assert out["foo"][0].error == "x"
    finally:
        p.unlink(missing_ok=True)
