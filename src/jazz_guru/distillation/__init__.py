"""Reflexion-style distillation loop."""

from jazz_guru.distillation.playbook import top_entries, upsert_entry
from jazz_guru.distillation.reflexion import (
    ReflectionResult,
    reflexion_job,
    run_reflexion,
)
from jazz_guru.distillation.scheduler import (
    enqueue_eval,
    enqueue_reflexion,
    schedule_periodic_reflexion,
)

__all__ = [
    "ReflectionResult",
    "enqueue_eval",
    "enqueue_reflexion",
    "reflexion_job",
    "run_reflexion",
    "schedule_periodic_reflexion",
    "top_entries",
    "upsert_entry",
]
