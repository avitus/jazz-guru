from __future__ import annotations

import asyncio
import sys
import textwrap

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.jobs import start_background_subprocess
from jazz_guru.actions.registry import registry
from jazz_guru.actions.sandbox import session_workspace
from jazz_guru.config import get_policy


class PythonExecInput(BaseModel):
    code: str = Field(..., description="Python source. Runs in a fresh subprocess with cwd=workspace.")
    timeout_sec: int | None = None
    stdin: str | None = None
    background: bool = Field(
        default=False,
        description=(
            "Run as a background job and return a job_id immediately. "
            "Use the `process` tool to poll/log/wait/kill."
        ),
    )
    notify_on_complete: bool = Field(
        default=False,
        description="Currently advisory; reserved for future delivery integration.",
    )


@registry.register(
    "python_exec",
    description=(
        "Execute Python code in an ephemeral subprocess (cwd=session workspace). "
        "By default blocks; set background=true to spawn as a job and return a "
        "job_id (then use the `process` tool to poll/log/wait/kill)."
    ),
    input_model=PythonExecInput,
    tags=("code",),
)
async def python_exec(
    code: str,
    timeout_sec: int | None = None,
    stdin: str | None = None,
    background: bool = False,
    notify_on_complete: bool = False,
) -> dict[str, object]:
    cwd = session_workspace(current().session_id)
    src = textwrap.dedent(code)
    if background:
        job = await start_background_subprocess(
            "python_exec",
            [sys.executable, "-I", "-c", src],
            cwd=cwd,
            stdin_bytes=(stdin or "").encode("utf-8") if stdin else None,
            notify_on_complete=notify_on_complete,
            meta={"code_bytes": len(src)},
        )
        return {
            "job_id": job.id,
            "pid": job.pid,
            "started": True,
            "stdout_path": str(job.stdout_path),
            "stderr_path": str(job.stderr_path),
            "tip": "Use the `process` tool with this job_id to poll/wait/kill.",
        }

    policy = get_policy().for_tool("python_exec")
    timeout = timeout_sec or policy.timeout_sec or 30
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        src,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input=(stdin or "").encode("utf-8")), timeout=timeout
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {"exit_code": -1, "stdout": "", "stderr": f"timeout after {timeout}s"}
    return {
        "exit_code": proc.returncode or 0,
        "stdout": out.decode("utf-8", errors="replace"),
        "stderr": err.decode("utf-8", errors="replace"),
    }
