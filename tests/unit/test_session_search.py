from __future__ import annotations

import uuid
from typing import Any

import pytest

from jazz_guru.actions.registry import register_all, registry
from jazz_guru.actions.tools import session_search as ss


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch):
    """Replace the DB query with an in-memory list of fake hits."""
    register_all()
    fake_hits: list[dict[str, Any]] = [
        {
            "turn_id": "t1",
            "session_id": "s1",
            "session_title": "blues study",
            "idx": 4,
            "role": "assistant",
            "text": "Use a ii-V-I in Bb major with smooth voice-leading.",
            "started_at": "2026-05-08T10:00:00+00:00",
        },
        {
            "turn_id": "t2",
            "session_id": "s2",
            "session_title": "voicings",
            "idx": 2,
            "role": "user",
            "text": "Can you show a ii-V-I in F minor?",
            "started_at": "2026-05-09T11:00:00+00:00",
        },
    ]

    received_args: dict[str, Any] = {}

    async def _fake_search(query, *, k, session_id, days, role):
        received_args["query"] = query
        received_args["k"] = k
        received_args["session_id"] = session_id
        received_args["days"] = days
        received_args["role"] = role
        if "ii-V-I" in query:
            return fake_hits
        return []

    monkeypatch.setattr(ss, "_search_turns", _fake_search)
    return received_args


async def test_session_search_returns_hits(fake_db) -> None:
    out = await registry.invoke("session_search", {"query": "ii-V-I", "k": 5})
    assert out["ok"] is True
    assert out["count"] == 2
    assert out["hits"][0]["text"].startswith("Use a ii-V-I")
    assert fake_db["k"] == 5


async def test_session_search_no_hits(fake_db) -> None:
    out = await registry.invoke("session_search", {"query": "nonexistent topic"})
    assert out["ok"] is True
    assert out["count"] == 0


async def test_session_search_rejects_empty_query(fake_db) -> None:
    out = await registry.invoke("session_search", {"query": "   "})
    assert out["ok"] is False
    assert "must not be empty" in out["error"]


async def test_session_search_session_id_filter(fake_db) -> None:
    sid = uuid.uuid4()
    out = await registry.invoke(
        "session_search", {"query": "ii-V-I", "session_id": str(sid)}
    )
    assert out["ok"] is True
    assert fake_db["session_id"] == sid


async def test_session_search_invalid_session_id(fake_db) -> None:
    out = await registry.invoke(
        "session_search", {"query": "ii-V-I", "session_id": "not-a-uuid"}
    )
    assert out["ok"] is False
    assert "invalid session_id" in out["error"]


async def test_session_search_days_filter(fake_db) -> None:
    out = await registry.invoke(
        "session_search", {"query": "ii-V-I", "days": 7}
    )
    assert out["ok"] is True
    assert fake_db["days"] == 7


async def test_session_search_role_filter(fake_db) -> None:
    out = await registry.invoke(
        "session_search", {"query": "ii-V-I", "role": "assistant"}
    )
    assert out["ok"] is True
    assert fake_db["role"] == "assistant"


async def test_session_search_summarize(
    fake_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_summarize(history):
        return f"Summary of {len(history)} hits."

    monkeypatch.setattr(ss, "summarize_history", _fake_summarize)
    out = await registry.invoke(
        "session_search", {"query": "ii-V-I", "summarize": True}
    )
    assert out["ok"] is True
    assert "Summary of 2 hits" in out["summary"]


async def test_session_search_summarize_failure_is_soft(
    fake_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _bad_summarize(history):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(ss, "summarize_history", _bad_summarize)
    out = await registry.invoke(
        "session_search", {"query": "ii-V-I", "summarize": True}
    )
    # The summarize failure is soft — the hits are still returned.
    assert out["ok"] is True
    assert out["count"] == 2
    assert "summary_error" in out
    assert "LLM down" in out["summary_error"]


async def test_session_search_db_exception_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_all()

    async def _bad_search(*_args, **_kw):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(ss, "_search_turns", _bad_search)
    out = await registry.invoke("session_search", {"query": "x"})
    assert out["ok"] is False
    assert "search failed" in out["error"]
