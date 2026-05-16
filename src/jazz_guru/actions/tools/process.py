"""``process`` — inspect and control background subprocesses started by ``python_exec``
or ``shell`` with ``background=true``."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import BaseModel, Field

from jazz_guru.actions.jobs import (
    BackgroundJob,
    current_jobs,
    read_log_window,
    terminate_job,
)
from jazz_guru.actions.registry import registry


class ProcessInput(BaseModel):
    action: str = Field(
        ...,
        description="One of: list, poll, log, wait, kill.",
    )
    id: str | None = Field(default=None, description="Job id (required for everything except 'list').")
    offset: int = Field(default=0, description="Byte offset for the 'log' / 'poll' actions.")
    limit: int = Field(default=16_000, description="Max bytes to read for 'log' / 'poll'.")
    stream: str = Field(default="both", description="For 'log' / 'poll': 'stdout' | 'stderr' | 'both'.")
    timeout_sec: float = Field(default=30.0, description="For 'wait': max seconds to block.")
    grace_sec: float = Field(default=2.0, description="For 'kill': SIGTERM grace before SIGKILL.")


def _job_summary(job: BackgroundJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "name": job.name,
        "status": job.status(),
        "pid": job.pid,
        "exit_code": job.exit_code,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "duration_sec": (
            (job.completed_at or 0) - job.started_at if job.completed_at else None
        ),
        "stdout_path": str(job.stdout_path),
        "stderr_path": str(job.stderr_path),
        "cmd": job.cmd,
        "meta": job.meta,
    }


def _read_stream_for(action: str, job: BackgroundJob, stream: str, offset: int, limit: int) -> dict[str, Any]:
    if stream == "stdout":
        return {"stdout": read_log_window(job.stdout_path, offset, limit)}
    if stream == "stderr":
        return {"stderr": read_log_window(job.stderr_path, offset, limit)}
    # both: split the byte budget so stdout + stderr together stay within the
    # caller's `limit`. (The previous floor of 1024 per side could blow past it.)
    left = max(0, limit // 2)
    right = max(0, limit - left)
    return {
        "stdout": read_log_window(job.stdout_path, offset, left),
        "stderr": read_log_window(job.stderr_path, offset, right),
    }


@registry.register(
    "process",
    description=(
        "Inspect and control background subprocesses. Actions: 'list' (no id), "
        "'poll' (new output since offset + status), 'log' (read window of stdout/"
        "stderr), 'wait' (block until exit or timeout), 'kill' (SIGTERM then "
        "SIGKILL). Jobs are created when python_exec / shell are called with "
        "background=true."
    ),
    input_model=ProcessInput,
    tags=("control",),
)
async def process(
    action: str,
    id: str | None = None,
    offset: int = 0,
    limit: int = 16_000,
    stream: str = "both",
    timeout_sec: float = 30.0,
    grace_sec: float = 2.0,
) -> dict[str, Any]:
    reg = current_jobs()
    if reg is None:
        return {"ok": False, "error": "no background job registry attached"}

    valid_actions = {"list", "poll", "log", "wait", "kill"}
    if action not in valid_actions:
        return {"ok": False, "error": f"unknown action: {action!r}"}

    if action == "list":
        return {"ok": True, "jobs": [_job_summary(j) for j in reg.list()]}

    if id is None:
        return {"ok": False, "error": f"action {action!r} requires an 'id'"}
    job = reg.get(id)
    if job is None:
        return {"ok": False, "error": f"no such job: {id}"}

    if action == "poll":
        return {"ok": True, **_job_summary(job), **_read_stream_for(action, job, stream, offset, limit)}

    if action == "log":
        return {"ok": True, "id": job.id, **_read_stream_for(action, job, stream, offset, limit)}

    if action == "wait":
        if job.proc is None:
            return {"ok": False, "error": "job has no underlying process"}
        try:
            await asyncio.wait_for(job.proc.wait(), timeout=timeout_sec)
        except TimeoutError:
            return {"ok": False, "timed_out": True, **_job_summary(job)}
        # The watcher task in jobs.py also waits on proc.wait(); both will fire
        # but ordering isn't guaranteed. Stamp the job state inline so the
        # summary we return reflects the just-finished process. The watcher's
        # work becomes idempotent.
        if job.exit_code is None and not job.cancelled:
            job.exit_code = job.proc.returncode
        if job.completed_at is None:
            job.completed_at = time.time()
        return {"ok": True, **_job_summary(job)}

    if action == "kill":
        if job.is_done:
            return {"ok": False, "error": "job already finished", **_job_summary(job)}
        killed = await terminate_job(job, grace_sec=grace_sec)
        return {"ok": killed, **_job_summary(job)}

    # Unreachable: valid_actions check above guarantees we matched one branch.
    return {"ok": False, "error": f"unknown action: {action!r}"}
