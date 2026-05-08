from __future__ import annotations

import uuid
from typing import Any

from rq import Queue

from jazz_guru.logging import get_logger
from jazz_guru.worker import get_redis

log = get_logger(__name__)


def _queue(name: str = "distillation") -> Queue:
    return Queue(name, connection=get_redis())


def enqueue_reflexion(session_id: uuid.UUID) -> str:
    job = _queue("distillation").enqueue(
        "jazz_guru.distillation.reflexion.reflexion_job", str(session_id)
    )
    log.info("scheduler.enqueued_reflexion", session_id=str(session_id), job_id=job.id)
    return job.id


def enqueue_eval(task_id: str | None = None) -> str:
    job = _queue("eval").enqueue(
        "jazz_guru.eval.regression.regression_job", task_id
    )
    log.info("scheduler.enqueued_eval", task_id=task_id, job_id=job.id)
    return job.id


def schedule_periodic_reflexion(session_id: uuid.UUID, *, interval_sec: int = 600) -> Any:
    """Use RQ-Scheduler-like API; with rq>=1.9 the worker has built-in scheduler support."""
    q = _queue("distillation")
    return q.enqueue_in(
        timedelta_from_seconds(interval_sec),
        "jazz_guru.distillation.reflexion.reflexion_job",
        str(session_id),
    )


def timedelta_from_seconds(seconds: int):
    from datetime import timedelta

    return timedelta(seconds=seconds)
