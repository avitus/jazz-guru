"""Reflexion-style distillation loop."""

from jazz_guru.distillation.auto import (
    TriggerOutcome,
    TriggerResult,
    find_undistilled_predecessors,
    maybe_trigger,
    scan_predecessors,
    sweep_idle,
    sweep_job,
)
from jazz_guru.distillation.playbook import top_entries, upsert_entry
from jazz_guru.distillation.reflexion import (
    ReflectionResult,
    reflexion_job,
    run_reflexion,
)
from jazz_guru.distillation.scheduler import (
    enqueue_eval,
    enqueue_reflexion,
    schedule_idle_sweep,
    schedule_periodic_reflexion,
)

__all__ = [
    "ReflectionResult",
    "TriggerOutcome",
    "TriggerResult",
    "enqueue_eval",
    "enqueue_reflexion",
    "find_undistilled_predecessors",
    "maybe_trigger",
    "reflexion_job",
    "run_reflexion",
    "scan_predecessors",
    "schedule_idle_sweep",
    "schedule_periodic_reflexion",
    "sweep_idle",
    "sweep_job",
    "top_entries",
    "upsert_entry",
]
