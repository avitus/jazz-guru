"""Auto-distillation triggers.

Three triggers funnel through ``maybe_trigger`` so dedup + scheduling
logic lives in one place:

1. Explicit close (``POST /sessions/{id}/close``, ``jazz-guru session close``)
2. Idle-timeout sweep (RQ worker tick — ``sweep_job`` re-enqueues itself)
3. New-session predecessor scan (server + CLI, on session creation)

Idempotency rule: a session is "undistilled" iff its newest assistant
``Turn.started_at`` is newer than its newest ``REFLEXION`` /
``DISTILLATION_QUEUED`` / ``DISTILLATION_INLINE`` event. Once we queue or
distill it, we won't try again until a new assistant turn lands, which
moves ``max(turn.ts)`` past the marker and re-arms the trigger.

Inline-cap fallback: when ``enqueue_reflexion`` fails (Redis down or worker
unconfigured), we run ``run_reflexion`` directly up to
``jg_distill_inline_max_per_process`` times per process. After that we
emit ``DISTILLATION_SKIPPED`` and bail — the next trigger retries.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, text

from jazz_guru.config import get_settings
from jazz_guru.db import session_scope
from jazz_guru.distillation.reflexion import run_reflexion
from jazz_guru.distillation.scheduler import enqueue_reflexion
from jazz_guru.logging import get_logger
from jazz_guru.state import Event, EventType, Turn, log_event

log = get_logger(__name__)


# Event types that count as "this session has been distilled (or queued
# for distillation) more recently than its last turn." Used by both the
# per-session idempotency check and the predecessor scan.
_DISTILL_MARKER_TYPES = (
    EventType.REFLEXION.value,
    EventType.DISTILLATION_QUEUED.value,
    EventType.DISTILLATION_INLINE.value,
)


# Process-local cap on inline fallbacks. Resets on process restart, which
# is intentional — a long-lived server process shouldn't gradually accept
# unbounded inline distillations as Redis stays down.
_inline_calls_made = 0


def _inline_count() -> int:
    return _inline_calls_made


def _reset_inline_count() -> None:
    """Test-only hook. Avoids leaking state between tests in the same process."""
    global _inline_calls_made
    _inline_calls_made = 0


def _advisory_key_for(session_id: uuid.UUID) -> int:
    """Map a UUID to a stable signed int64 for ``pg_advisory_xact_lock``.

    Postgres advisory locks take a bigint; the first 8 bytes of the UUID
    give us a uniform random key with no collisions in practice. The lock
    auto-releases at txn close.
    """
    return int.from_bytes(session_id.bytes[:8], "big", signed=True)


# Outcomes are returned as plain strings (matching ImproveStatus's
# convention in distillation/improver.py) so callers can branch in a
# readable way without importing an enum.
class TriggerOutcome:
    QUEUED = "queued"
    INLINE = "inline"
    SKIPPED_EMPTY = "skipped_empty"
    SKIPPED_ALREADY_DISTILLED = "skipped_already_distilled"
    SKIPPED_ENQUEUE_FAILED = "skipped_enqueue_failed"


@dataclass
class TriggerResult:
    outcome: str
    reason: str
    session_id: uuid.UUID
    job_id: str | None = None
    err: str | None = None


async def maybe_trigger(session_id: uuid.UUID, *, reason: str) -> TriggerResult:
    """Queue (or inline-run) reflexion for ``session_id`` if it's undistilled.

    ``reason`` is one of ``close`` / ``idle_sweep`` / ``new_session`` (free-form;
    written into the event payload for the audit trail).

    Concurrency: the dedupe check + DISTILLATION_QUEUED insert are serialized
    by a Postgres advisory lock keyed on the session id, so two concurrent
    triggers for the same session cannot both reach the enqueue path.
    Different sessions don't block each other. Enqueue is called inside the
    locked transaction so its visible side effect (the queued marker) is
    atomic with the eligibility check.
    """
    lock_key = _advisory_key_for(session_id)
    enqueue_err: str | None = None
    queued_outcome: TriggerResult | None = None

    async with session_scope() as s:
        # pg_advisory_xact_lock auto-releases at txn close. All decision
        # paths (skipped/queued) commit their event in this same txn so
        # the audit trail and the dedup marker land together.
        await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key})

        last_turn = (
            await s.execute(
                select(func.max(Turn.started_at)).where(
                    Turn.session_id == session_id, Turn.role == "assistant"
                )
            )
        ).scalar_one_or_none()
        last_marker = (
            await s.execute(
                select(func.max(Event.ts)).where(
                    Event.session_id == session_id,
                    Event.type.in_(_DISTILL_MARKER_TYPES),
                )
            )
        ).scalar_one_or_none()

        if last_turn is None:
            s.add(
                Event(
                    session_id=session_id,
                    type=EventType.DISTILLATION_SKIPPED.value,
                    payload={"reason": reason, "why": "empty"},
                )
            )
            return TriggerResult(
                outcome=TriggerOutcome.SKIPPED_EMPTY,
                reason=reason,
                session_id=session_id,
            )

        if last_marker is not None and last_marker >= last_turn:
            s.add(
                Event(
                    session_id=session_id,
                    type=EventType.DISTILLATION_SKIPPED.value,
                    payload={"reason": reason, "why": "already_distilled"},
                )
            )
            return TriggerResult(
                outcome=TriggerOutcome.SKIPPED_ALREADY_DISTILLED,
                reason=reason,
                session_id=session_id,
            )

        # Eligible. Call enqueue while still holding the lock so that
        # success → marker insert is atomic with the eligibility read.
        try:
            job_id = enqueue_reflexion(session_id)
        except Exception as e:
            enqueue_err = str(e)
        else:
            s.add(
                Event(
                    session_id=session_id,
                    type=EventType.DISTILLATION_QUEUED.value,
                    payload={
                        "reason": reason,
                        "scheduler": "rq",
                        "job_id": job_id,
                    },
                )
            )
            queued_outcome = TriggerResult(
                outcome=TriggerOutcome.QUEUED,
                reason=reason,
                session_id=session_id,
                job_id=job_id,
            )

    if queued_outcome is not None:
        log.info(
            "auto.queued",
            session_id=str(session_id),
            reason=reason,
            job_id=queued_outcome.job_id,
        )
        return queued_outcome

    # Enqueue failed; fall through to inline-cap fallback. The advisory
    # lock has already been released, so concurrent inline fallbacks could
    # technically race — but the inline path has its own per-process cap
    # and the next user turn re-arms the dedup marker either way.
    assert enqueue_err is not None  # set by the except branch above
    return await _inline_fallback(
        session_id, reason=reason, enqueue_err=enqueue_err
    )


async def _inline_fallback(
    session_id: uuid.UUID, *, reason: str, enqueue_err: str
) -> TriggerResult:
    """Run ``run_reflexion`` inline when the queue is unavailable.

    Caps at ``jg_distill_inline_max_per_process`` per-process. Beyond that,
    skip and let the next trigger retry. The REFLEXION event written by
    ``run_reflexion`` itself plus our DISTILLATION_INLINE breadcrumb both
    contribute to the dedup marker.
    """
    global _inline_calls_made
    settings = get_settings()
    if _inline_calls_made >= settings.jg_distill_inline_max_per_process:
        await _emit_skipped(
            session_id,
            reason=reason,
            why="enqueue_failed_and_cap_reached",
            err=enqueue_err,
        )
        log.warning(
            "auto.inline_cap_reached",
            session_id=str(session_id),
            cap=settings.jg_distill_inline_max_per_process,
        )
        return TriggerResult(
            outcome=TriggerOutcome.SKIPPED_ENQUEUE_FAILED,
            reason=reason,
            session_id=session_id,
            err=enqueue_err,
        )
    _inline_calls_made += 1
    try:
        await run_reflexion(session_id)
    except Exception as run_err:
        log.warning(
            "auto.inline_run_failed", session_id=str(session_id), err=str(run_err)
        )
        return TriggerResult(
            outcome=TriggerOutcome.SKIPPED_ENQUEUE_FAILED,
            reason=reason,
            session_id=session_id,
            err=f"enqueue: {enqueue_err}; run: {run_err}",
        )
    await log_event(
        session_id=session_id,
        event_type=EventType.DISTILLATION_INLINE.value,
        payload={
            "reason": reason,
            "scheduler": "inline",
            "enqueue_err": enqueue_err,
        },
    )
    log.info("auto.inline", session_id=str(session_id), reason=reason)
    return TriggerResult(
        outcome=TriggerOutcome.INLINE, reason=reason, session_id=session_id
    )


async def _emit_skipped(
    session_id: uuid.UUID,
    *,
    reason: str,
    why: str,
    err: str | None = None,
) -> None:
    payload: dict[str, Any] = {"reason": reason, "why": why}
    if err is not None:
        payload["err"] = err
    try:
        await log_event(
            session_id=session_id,
            event_type=EventType.DISTILLATION_SKIPPED.value,
            payload=payload,
        )
    except Exception as e:
        # If the session row was deleted under us (rare; mostly tests), the
        # FK insert will fail. The skip is still a no-op for behavior; just
        # warn instead of bubbling.
        log.warning(
            "auto.skip_event_log_failed",
            session_id=str(session_id),
            why=why,
            err=str(e),
        )


async def find_undistilled_predecessors(*, idle_for_seconds: int) -> list[uuid.UUID]:
    """Return sessions whose newest assistant turn is older than the cutoff and
    has no distill marker more recent than that turn.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=idle_for_seconds)
    last_turn = (
        select(
            Turn.session_id.label("sid"),
            func.max(Turn.started_at).label("last_at"),
        )
        .where(Turn.role == "assistant")
        .group_by(Turn.session_id)
        .subquery()
    )
    last_marker = (
        select(
            Event.session_id.label("sid"),
            func.max(Event.ts).label("last_at"),
        )
        .where(Event.type.in_(_DISTILL_MARKER_TYPES))
        .group_by(Event.session_id)
        .subquery()
    )
    stmt = (
        select(last_turn.c.sid)
        .outerjoin(last_marker, last_turn.c.sid == last_marker.c.sid)
        .where(last_turn.c.last_at < cutoff)
        .where(
            or_(
                last_marker.c.last_at.is_(None),
                last_marker.c.last_at < last_turn.c.last_at,
            )
        )
    )
    async with session_scope() as s:
        rows = (await s.execute(stmt)).all()
    return [r[0] for r in rows]


async def sweep_idle() -> list[TriggerResult]:
    """One sweep pass: trigger reflexion for every idle, undistilled session."""
    settings = get_settings()
    sids = await find_undistilled_predecessors(
        idle_for_seconds=settings.jg_distill_idle_sec
    )
    if not sids:
        return []
    log.info("auto.sweep_found", count=len(sids))
    results: list[TriggerResult] = []
    for sid in sids:
        try:
            results.append(await maybe_trigger(sid, reason="idle_sweep"))
        except Exception as e:
            log.warning(
                "auto.sweep_trigger_failed", session_id=str(sid), err=str(e)
            )
    return results


async def scan_predecessors(*, reason: str = "new_session") -> list[TriggerResult]:
    """Run on new-session creation: queue reflexion for idle predecessors.

    Honors ``jg_distill_on_new_session``; returns empty when disabled.
    """
    settings = get_settings()
    if not settings.jg_distill_on_new_session:
        return []
    sids = await find_undistilled_predecessors(
        idle_for_seconds=settings.jg_distill_idle_sec
    )
    if not sids:
        return []
    log.info("auto.new_session_scan_found", count=len(sids))
    results: list[TriggerResult] = []
    for sid in sids:
        try:
            results.append(await maybe_trigger(sid, reason=reason))
        except Exception as e:
            log.warning(
                "auto.new_session_trigger_failed",
                session_id=str(sid),
                err=str(e),
            )
    return results


def sweep_job() -> dict[str, Any]:
    """Sync RQ entrypoint. Runs one sweep, then re-enqueues itself.

    Both the sweep run and the reschedule are guarded: a transient DB or
    Redis failure on this tick must not stop the periodic chain. If the
    reschedule fails too, the chain stops — RQ would otherwise mark the
    job failed and never tick again.
    """
    try:
        triggered = asyncio.run(sweep_idle())
    except Exception as e:
        log.warning("auto.sweep_failed", err=str(e))
        triggered = []
    # Refresh the singleton lease so other workers don't seed a parallel
    # chain at their next boot. Best-effort: a transient Redis hiccup
    # shouldn't break the reschedule.
    try:
        from jazz_guru.worker import refresh_sweep_lease

        refresh_sweep_lease()
    except Exception as e:
        log.warning("auto.sweep_lease_refresh_failed", err=str(e))
    try:
        from jazz_guru.distillation.scheduler import schedule_idle_sweep

        schedule_idle_sweep()
    except Exception as e:
        log.warning("auto.sweep_reschedule_failed", err=str(e))
    return {"triggered": len(triggered)}
