from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.jobs import start_background_subprocess
from jazz_guru.actions.registry import registry
from jazz_guru.actions.sandbox import session_workspace
from jazz_guru.config import get_policy


class ShellInput(BaseModel):
    command: str = Field(..., description="Shell command run via /bin/sh -c, cwd=session workspace.")
    timeout_sec: int | None = Field(None, description="Override the policy timeout.")
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
    "shell",
    description=(
        "Execute a shell command in the session workspace. By default blocks "
        "and returns stdout/stderr/exit_code. Set background=true to spawn a "
        "long-running job and return a job_id; then use the `process` tool."
    ),
    input_model=ShellInput,
    tags=("shell",),
)
async def shell(
    command: str,
    timeout_sec: int | None = None,
    background: bool = False,
    notify_on_complete: bool = False,
) -> dict[str, object]:
    cwd = session_workspace(current().session_id)
    if background:
        job = await start_background_subprocess(
            "shell",
            ["/bin/sh", "-c", command],
            cwd=cwd,
            notify_on_complete=notify_on_complete,
            meta={"command": command},
        )
        return {
            "job_id": job.id,
            "pid": job.pid,
            "started": True,
            "stdout_path": str(job.stdout_path),
            "stderr_path": str(job.stderr_path),
            "tip": "Use the `process` tool with this job_id to poll/wait/kill.",
        }

    policy = get_policy().for_tool("shell")
    timeout = timeout_sec or policy.timeout_sec or 60
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {"exit_code": -1, "stdout": "", "stderr": f"timeout after {timeout}s"}
    return {
        "exit_code": proc.returncode or 0,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }
