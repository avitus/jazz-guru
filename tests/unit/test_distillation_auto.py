"""Tests for distillation/auto.py — the session-distillation trigger funnel.

Covers maybe_trigger, find_undistilled_predecessors, scan_predecessors,
sweep_idle, plus the close endpoint that wraps them. enqueue_reflexion
and run_reflexion are stubbed so tests don't need Redis or Anthropic.
"""
from __future__ import annotations

import uuid as uuid_mod
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from jazz_guru.db import session_scope
from jazz_guru.distillation import auto
from jazz_guru.server import create_app
from jazz_guru.state import Event, EventType, Session, Turn, log_event

# ---------- helpers ----------

async def _make_session_with_assistant_turn(
    *, idle_seconds: int = 0, n_turns: int = 1
) -> uuid_mod.UUID:
    """Create a Session with N assistant turns; the newest is ``idle_seconds`` ago."""
    sid = uuid_mod.uuid4()
    base_ts = datetime.now(UTC) - timedelta(seconds=idle_seconds)
    async with session_scope() as s:
        s.add(Session(id=sid))
        await s.flush()
        for i in range(n_turns):
            # Older turns sit earlier; newest = base_ts.
            ts = base_ts - timedelta(seconds=(n_turns - 1 - i))
            s.add(
                Turn(
                    session_id=sid,
                    idx=i,
                    role="assistant",
                    content={"text": f"hi {i}"},
                    started_at=ts,
                )
            )
    return sid


async def _make_empty_session() -> uuid_mod.UUID:
    sid = uuid_mod.uuid4()
    async with session_scope() as s:
        s.add(Session(id=sid))
    return sid


async def _delete_session(sid: uuid_mod.UUID) -> None:
    async with session_scope() as s:
        row = (
            await s.execute(select(Session).where(Session.id == sid))
        ).scalar_one_or_none()
        if row is not None:
            await s.delete(row)


async def _events_for_session(sid: uuid_mod.UUID) -> list[Event]:
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Event)
                .where(Event.session_id == sid)
                .order_by(Event.ts.asc())
            )
        ).scalars().all()
        return list(rows)


def _stub_enqueue_ok(monkeypatch: pytest.MonkeyPatch, job_id: str = "fake-job") -> list[uuid_mod.UUID]:
    """Replace enqueue_reflexion with a non-Redis stub. Returns a list that captures calls."""
    captured: list[uuid_mod.UUID] = []

    def _enqueue(sid: uuid_mod.UUID) -> str:
        captured.append(sid)
        return job_id

    monkeypatch.setattr(auto, "enqueue_reflexion", _enqueue)
    return captured


def _stub_enqueue_failure(monkeypatch: pytest.MonkeyPatch, err: str = "no redis") -> None:
    def _enqueue(sid: uuid_mod.UUID) -> str:
        raise RuntimeError(err)

    monkeypatch.setattr(auto, "enqueue_reflexion", _enqueue)


def _stub_run_reflexion_noop(monkeypatch: pytest.MonkeyPatch, *, emit_event: bool = True):
    """Replace run_reflexion with a fast async no-op.

    By default it still writes a REFLEXION event so the dedup marker
    behaves like the real run; set ``emit_event=False`` to omit that.
    """
    calls: list[uuid_mod.UUID] = []

    async def _run(sid: uuid_mod.UUID):
        calls.append(sid)
        if emit_event:
            await log_event(
                session_id=sid,
                event_type=EventType.REFLEXION.value,
                payload={"stub": True},
            )
        return None

    monkeypatch.setattr(auto, "run_reflexion", _run)
    return calls


# ---------- core funnel ----------


async def test_happy_path_queues_reflexion(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _stub_enqueue_ok(monkeypatch, job_id="job-happy")
    sid = await _make_session_with_assistant_turn()
    try:
        result = await auto.maybe_trigger(sid, reason="close")
        assert result.outcome == auto.TriggerOutcome.QUEUED
        assert result.job_id == "job-happy"
        assert result.session_id == sid
        assert captured == [sid]

        events = await _events_for_session(sid)
        queued = [
            e for e in events if e.type == EventType.DISTILLATION_QUEUED.value
        ]
        assert len(queued) == 1
        assert queued[0].payload["reason"] == "close"
        assert queued[0].payload["scheduler"] == "rq"
        assert queued[0].payload["job_id"] == "job-happy"
    finally:
        await _delete_session(sid)


async def test_empty_session_emits_skipped_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_enqueue_ok(monkeypatch)
    sid = await _make_empty_session()
    try:
        result = await auto.maybe_trigger(sid, reason="close")
        assert result.outcome == auto.TriggerOutcome.SKIPPED_EMPTY

        events = await _events_for_session(sid)
        skipped = [
            e for e in events if e.type == EventType.DISTILLATION_SKIPPED.value
        ]
        assert len(skipped) == 1
        assert skipped[0].payload["why"] == "empty"
        # No queued event when we bail on empty.
        assert not any(
            e.type == EventType.DISTILLATION_QUEUED.value for e in events
        )
    finally:
        await _delete_session(sid)


async def test_idempotent_second_call_skips_already_distilled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _stub_enqueue_ok(monkeypatch)
    sid = await _make_session_with_assistant_turn(idle_seconds=2)
    try:
        r1 = await auto.maybe_trigger(sid, reason="close")
        r2 = await auto.maybe_trigger(sid, reason="close")

        assert r1.outcome == auto.TriggerOutcome.QUEUED
        assert r2.outcome == auto.TriggerOutcome.SKIPPED_ALREADY_DISTILLED
        # enqueue called exactly once across both triggers.
        assert captured == [sid]

        events = await _events_for_session(sid)
        queued = [
            e for e in events if e.type == EventType.DISTILLATION_QUEUED.value
        ]
        skipped = [
            e for e in events if e.type == EventType.DISTILLATION_SKIPPED.value
        ]
        assert len(queued) == 1
        assert len(skipped) == 1
        assert skipped[0].payload["why"] == "already_distilled"
    finally:
        await _delete_session(sid)


async def test_new_turn_rearms_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _stub_enqueue_ok(monkeypatch)
    sid = await _make_session_with_assistant_turn(idle_seconds=5)
    try:
        r1 = await auto.maybe_trigger(sid, reason="close")
        assert r1.outcome == auto.TriggerOutcome.QUEUED

        # New turn timestamped firmly after the queued event.
        future = datetime.now(UTC) + timedelta(seconds=10)
        async with session_scope() as s:
            s.add(
                Turn(
                    session_id=sid,
                    idx=1,
                    role="assistant",
                    content={"text": "second"},
                    started_at=future,
                )
            )

        r2 = await auto.maybe_trigger(sid, reason="close")
        assert r2.outcome == auto.TriggerOutcome.QUEUED
        assert captured == [sid, sid]
    finally:
        await _delete_session(sid)


async def test_user_only_turns_count_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sessions with no assistant reply yet should not be distilled."""
    _stub_enqueue_ok(monkeypatch)
    sid = uuid_mod.uuid4()
    async with session_scope() as s:
        s.add(Session(id=sid))
        await s.flush()
        s.add(
            Turn(
                session_id=sid,
                idx=0,
                role="user",
                content={"text": "hello"},
            )
        )
    try:
        result = await auto.maybe_trigger(sid, reason="close")
        assert result.outcome == auto.TriggerOutcome.SKIPPED_EMPTY
    finally:
        await _delete_session(sid)


# ---------- inline-cap fallback (Redis down) ----------


async def test_inline_fallback_when_enqueue_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auto._reset_inline_count()
    _stub_enqueue_failure(monkeypatch, err="connection refused")
    run_calls = _stub_run_reflexion_noop(monkeypatch)

    sid = await _make_session_with_assistant_turn()
    try:
        result = await auto.maybe_trigger(sid, reason="close")
        assert result.outcome == auto.TriggerOutcome.INLINE
        assert run_calls == [sid]

        events = await _events_for_session(sid)
        inline = [
            e for e in events if e.type == EventType.DISTILLATION_INLINE.value
        ]
        reflexion = [e for e in events if e.type == EventType.REFLEXION.value]
        assert len(inline) == 1
        assert len(reflexion) == 1
        assert inline[0].payload["reason"] == "close"
        assert inline[0].payload["scheduler"] == "inline"
        assert "connection refused" in inline[0].payload["enqueue_err"]
    finally:
        await _delete_session(sid)


async def test_inline_cap_blocks_further_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    auto._reset_inline_count()
    # Cap to one inline call this process.
    monkeypatch.setattr(auto.get_settings(), "jg_distill_inline_max_per_process", 1)
    _stub_enqueue_failure(monkeypatch)
    run_calls = _stub_run_reflexion_noop(monkeypatch)

    sid1 = await _make_session_with_assistant_turn()
    sid2 = await _make_session_with_assistant_turn()
    try:
        r1 = await auto.maybe_trigger(sid1, reason="close")
        r2 = await auto.maybe_trigger(sid2, reason="close")
        assert r1.outcome == auto.TriggerOutcome.INLINE
        assert r2.outcome == auto.TriggerOutcome.SKIPPED_ENQUEUE_FAILED
        # The capped second call must NOT have invoked run_reflexion.
        assert run_calls == [sid1]

        events_2 = await _events_for_session(sid2)
        skipped = [
            e for e in events_2 if e.type == EventType.DISTILLATION_SKIPPED.value
        ]
        assert len(skipped) == 1
        assert skipped[0].payload["why"] == "enqueue_failed_and_cap_reached"
    finally:
        await _delete_session(sid1)
        await _delete_session(sid2)


async def test_inline_run_failure_does_not_block_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the inline run_reflexion itself raises, no REFLEXION / INLINE event
    is written, so a future trigger can retry.
    """
    auto._reset_inline_count()
    _stub_enqueue_failure(monkeypatch)

    async def _broken_run(sid: uuid_mod.UUID) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(auto, "run_reflexion", _broken_run)
    sid = await _make_session_with_assistant_turn()
    try:
        result = await auto.maybe_trigger(sid, reason="close")
        assert result.outcome == auto.TriggerOutcome.SKIPPED_ENQUEUE_FAILED
        assert "boom" in (result.err or "")

        events = await _events_for_session(sid)
        # The breadcrumb is suppressed when the run itself failed, so a
        # subsequent trigger sees no dedup marker.
        assert not any(
            e.type
            in (EventType.DISTILLATION_INLINE.value, EventType.REFLEXION.value)
            for e in events
        )
    finally:
        await _delete_session(sid)


# ---------- predecessor scan ----------


async def test_find_predecessors_filters_by_idle_and_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid_idle_undistilled = await _make_session_with_assistant_turn(idle_seconds=900)
    sid_fresh = await _make_session_with_assistant_turn(idle_seconds=0)
    sid_idle_already = await _make_session_with_assistant_turn(idle_seconds=900)
    # Mark sid_idle_already as already-distilled (REFLEXION event newer than its turn).
    await log_event(
        session_id=sid_idle_already,
        event_type=EventType.REFLEXION.value,
        payload={"score": 0.5},
    )
    try:
        sids = await auto.find_undistilled_predecessors(idle_for_seconds=600)
        sids_set = set(sids)
        assert sid_idle_undistilled in sids_set
        assert sid_fresh not in sids_set
        assert sid_idle_already not in sids_set
    finally:
        await _delete_session(sid_idle_undistilled)
        await _delete_session(sid_fresh)
        await _delete_session(sid_idle_already)


async def test_scan_predecessors_queues_idle_undistilled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _stub_enqueue_ok(monkeypatch)
    sid_idle = await _make_session_with_assistant_turn(idle_seconds=900)
    sid_fresh = await _make_session_with_assistant_turn(idle_seconds=0)
    try:
        results = await auto.scan_predecessors(reason="new_session")
        # Filter out the fresh session — only the idle one should be triggered.
        queued = [r for r in results if r.outcome == auto.TriggerOutcome.QUEUED]
        queued_sids = {r.session_id for r in queued}
        assert sid_idle in queued_sids
        assert sid_fresh not in queued_sids
        # captured may contain unrelated idle sessions from prior runs sharing
        # the DB; only assert membership for the sessions this test created.
        assert sid_idle in captured
        assert sid_fresh not in captured
    finally:
        await _delete_session(sid_idle)
        await _delete_session(sid_fresh)


async def test_scan_predecessors_respects_disable_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auto.get_settings(), "jg_distill_on_new_session", False)
    captured = _stub_enqueue_ok(monkeypatch)
    sid = await _make_session_with_assistant_turn(idle_seconds=900)
    try:
        results = await auto.scan_predecessors(reason="new_session")
        assert results == []
        assert captured == []
    finally:
        await _delete_session(sid)


# ---------- idle sweep ----------


async def test_sweep_idle_triggers_only_idle_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _stub_enqueue_ok(monkeypatch)
    sid_idle = await _make_session_with_assistant_turn(idle_seconds=900)
    sid_fresh = await _make_session_with_assistant_turn(idle_seconds=0)
    try:
        results = await auto.sweep_idle()
        queued_sids = {
            r.session_id for r in results if r.outcome == auto.TriggerOutcome.QUEUED
        }
        assert sid_idle in queued_sids
        assert sid_fresh not in queued_sids
        # Each queued result reports the idle_sweep reason.
        assert all(
            r.reason == "idle_sweep"
            for r in results
            if r.outcome == auto.TriggerOutcome.QUEUED
        )
        assert captured == [sid_idle]
    finally:
        await _delete_session(sid_idle)
        await _delete_session(sid_fresh)


# ---------- close endpoint ----------


def _async_client(monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:
    """Build an httpx AsyncClient bound to the ASGI app.

    TestClient runs the route in a worker thread / fresh loop, which
    conflicts with the asyncpg connection pool. ASGITransport runs the
    route inside the test's own loop so the existing pool keeps working.
    """
    monkeypatch.delenv("JG_API_KEY", raising=False)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()),
        base_url="http://test",
    )


async def test_close_endpoint_returns_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_enqueue_ok(monkeypatch, job_id="job-close")
    sid = await _make_session_with_assistant_turn()
    try:
        async with _async_client(monkeypatch) as client:
            resp = await client.post(f"/sessions/{sid}/close")
        assert resp.status_code == 200
        body = resp.json()
        assert body["outcome"] == auto.TriggerOutcome.QUEUED
        assert body["reason"] == "close"
        assert body["job_id"] == "job-close"
    finally:
        await _delete_session(sid)


async def test_close_endpoint_sync_runs_reflexion_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """?sync=true bypasses maybe_trigger and runs run_reflexion directly."""
    inline_calls: list[uuid_mod.UUID] = []

    # ?sync=true imports run_reflexion from jazz_guru.distillation (the
    # package re-export), so patch it on the server module's binding.
    from jazz_guru import server as server_mod

    class _FakeResult:
        score = 0.42
        critique = "ok"

    async def _fake_run(sid: uuid_mod.UUID) -> Any:
        inline_calls.append(sid)
        return _FakeResult()

    monkeypatch.setattr(server_mod, "run_reflexion", _fake_run)
    sid = await _make_session_with_assistant_turn()
    try:
        async with _async_client(monkeypatch) as client:
            resp = await client.post(f"/sessions/{sid}/close?sync=true")
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "sync"
        assert body["score"] == pytest.approx(0.42)
        assert inline_calls == [sid]
    finally:
        await _delete_session(sid)


async def test_close_endpoint_invalid_uuid_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _async_client(monkeypatch) as client:
        resp = await client.post("/sessions/not-a-uuid/close")
    assert resp.status_code == 400


async def test_close_endpoint_disabled_returns_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auto.get_settings(), "jg_distill_on_close", False)
    sid = await _make_session_with_assistant_turn()
    try:
        async with _async_client(monkeypatch) as client:
            resp = await client.post(f"/sessions/{sid}/close")
        assert resp.status_code == 200
        assert resp.json() == {"outcome": "disabled"}
    finally:
        await _delete_session(sid)
