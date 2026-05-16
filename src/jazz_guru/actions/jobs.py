"""Background subprocess registry for long-running tool calls.

Each session gets its own :class:`BackgroundJobRegistry`, bound via the
``_JOBS_OVERLAY`` ContextVar in :meth:`AgentLoop.step` exactly the same way
as :class:`DynamicRegistry`. Concurrent sessions in the same process don't
share state.

Subprocesses started via :func:`start_background_subprocess` stream stdout
and stderr to per-job files under ``<workspace>/.jobs/<job_id>/`` so the
``process`` tool can paginate them after the fact without burning context.

A short-lived watcher task awaits ``proc.wait()`` and stamps the exit code +
completion time when the process exits, so polling is a cheap dict lookup.
"""
from __future__ import annotations

import asyncio
import contextlib
import signal
import time
import uuid as uuid_mod
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from jazz_guru.actions.sandbox import session_workspace


@dataclass
class BackgroundJob:
    id: str
    name: str  # originating tool (e.g. "python_exec", "shell")
    cmd: list[str]
    cwd: str
    stdout_path: Path
    stderr_path: Path
    proc: asyncio.subprocess.Process | None = None
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    exit_code: int | None = None
    cancelled: bool = False
    notify_on_complete: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_done(self) -> bool:
        return self.exit_code is not None or self.cancelled

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def status(self) -> str:
        if self.cancelled:
            return "cancelled"
        if self.exit_code is None:
            return "running"
        return "completed" if self.exit_code == 0 else "failed"


class BackgroundJobRegistry:
    """Per-session registry of background subprocesses."""

    def __init__(self) -> None:
        self._jobs: dict[str, BackgroundJob] = {}

    def add(self, job: BackgroundJob) -> None:
        self._jobs[job.id] = job

    def get(self, job_id: str) -> BackgroundJob | None:
        return self._jobs.get(job_id)

    def list(self) -> list[BackgroundJob]:
        return list(self._jobs.values())

    def remove(self, job_id: str) -> bool:
        return self._jobs.pop(job_id, None) is not None

    def __contains__(self, job_id: str) -> bool:
        return job_id in self._jobs


_JOBS_OVERLAY: ContextVar[BackgroundJobRegistry | None] = ContextVar(
    "jg_jobs_overlay", default=None
)


def attach_jobs(reg: BackgroundJobRegistry) -> Token[BackgroundJobRegistry | None]:
    return _JOBS_OVERLAY.set(reg)


def detach_jobs(token: Token[BackgroundJobRegistry | None] | None = None) -> None:
    if token is None:
        _JOBS_OVERLAY.set(None)
    else:
        _JOBS_OVERLAY.reset(token)


def current_jobs() -> BackgroundJobRegistry | None:
    return _JOBS_OVERLAY.get()


def jobs_dir(session_id: str | None) -> Path:
    p = session_workspace(session_id) / ".jobs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def new_job_id() -> str:
    return uuid_mod.uuid4().hex[:12]


async def _watch_proc(
    job: BackgroundJob,
    proc: asyncio.subprocess.Process,
    files: tuple[IO[bytes], IO[bytes]],
) -> None:
    """Block on ``proc.wait()`` and stamp the job + close log files when it exits."""
    try:
        await proc.wait()
        if not job.cancelled:
            job.exit_code = proc.returncode
    finally:
        job.completed_at = time.time()
        for fh in files:
            with contextlib.suppress(Exception):
                fh.close()


async def start_background_subprocess(
    name: str,
    cmd: list[str],
    *,
    cwd: Path,
    stdin_bytes: bytes | None = None,
    notify_on_complete: bool = False,
    meta: dict[str, Any] | None = None,
) -> BackgroundJob:
    """Spawn ``cmd`` in the background and register a job.

    ``stdin_bytes`` is written once at start, then stdin is closed. Use the
    ``process write`` tool for interactive jobs (requires a job started in a
    streaming-stdin mode -- not currently exposed).
    """
    from jazz_guru.actions.context import current as current_ctx

    reg = current_jobs()
    if reg is None:
        raise RuntimeError("no BackgroundJobRegistry attached to the current context")

    job_id = new_job_id()
    sid = current_ctx().session_id
    j_dir = jobs_dir(sid) / job_id
    j_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = j_dir / "stdout.log"
    stderr_path = j_dir / "stderr.log"

    job = BackgroundJob(
        id=job_id,
        name=name,
        cmd=list(cmd),
        cwd=str(cwd),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        notify_on_complete=notify_on_complete,
        meta=meta or {},
    )

    stdout_fh = stdout_path.open("wb", buffering=0)
    try:
        stderr_fh = stderr_path.open("wb", buffering=0)
    except Exception:
        with contextlib.suppress(Exception):
            stdout_fh.close()
        raise

    try:
        # Apply the macOS sandbox wrapper (JG_OS_SANDBOX=1) so background jobs
        # honour the same confinement as foreground tool subprocesses.
        from jazz_guru.actions.sandbox_profile import wrap_subprocess

        wrapped_cmd = wrap_subprocess(list(cmd), cwd)
        proc = await asyncio.create_subprocess_exec(
            *wrapped_cmd,
            cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=stdout_fh,
            stderr=stderr_fh,
        )
    except Exception:
        # If spawn fails the file handles would otherwise leak.
        with contextlib.suppress(Exception):
            stdout_fh.close()
        with contextlib.suppress(Exception):
            stderr_fh.close()
        raise
    job.proc = proc

    if proc.stdin is not None:
        if stdin_bytes:
            try:
                proc.stdin.write(stdin_bytes)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                # process exited before we finished writing; the watcher will
                # capture the exit code.
                pass
        # Close stdin so any sys.stdin.read() in the child returns immediately
        # with whatever we wrote (or empty bytes).
        with contextlib.suppress(Exception):
            proc.stdin.close()

    reg.add(job)
    # Anchor the watcher task on the job so the event loop holds a strong
    # reference (ruff's RUF006 catches discard-the-task bugs). We don't ever
    # ``await`` it directly; the job's exit_code / completed_at fields are
    # what callers actually observe.
    job.meta.setdefault("_watcher_task", None)
    job.meta["_watcher_task"] = asyncio.create_task(
        _watch_proc(job, proc, (stdout_fh, stderr_fh))
    )
    return job


async def terminate_job(job: BackgroundJob, grace_sec: float = 2.0) -> bool:
    """SIGTERM the job; SIGKILL after ``grace_sec`` if still alive."""
    if job.proc is None or job.proc.returncode is not None:
        return False
    try:
        job.proc.terminate()
    except ProcessLookupError:
        # Process already exited; don't mark the job as cancelled because
        # the watcher will see the real exit code in a moment.
        return False
    job.cancelled = True
    try:
        await asyncio.wait_for(job.proc.wait(), timeout=grace_sec)
        return True
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            job.proc.send_signal(signal.SIGKILL)
        with contextlib.suppress(Exception):
            await job.proc.wait()
        return True


def read_log_window(path: Path, offset: int = 0, limit: int = 16_000) -> dict[str, Any]:
    """Read a chunk of a log file starting at ``offset``. Returns text + next offset."""
    if not path.exists():
        return {"text": "", "offset": 0, "next_offset": 0, "size": 0, "eof": True}
    size = path.stat().st_size
    if offset < 0:
        offset = max(0, size + offset)
    offset = min(offset, size)
    with path.open("rb") as fh:
        fh.seek(offset)
        data = fh.read(limit)
    text = data.decode("utf-8", errors="replace")
    next_offset = offset + len(data)
    return {
        "text": text,
        "offset": offset,
        "next_offset": next_offset,
        "size": size,
        "eof": next_offset >= size,
    }
