from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.registry import registry
from jazz_guru.actions.sandbox import session_workspace
from jazz_guru.config import get_policy


class ShellInput(BaseModel):
    command: str = Field(..., description="Shell command run via /bin/sh -c, cwd=session workspace.")
    timeout_sec: int | None = Field(None, description="Override the policy timeout.")


@registry.register(
    "shell",
    description="Execute a shell command in the session workspace and return stdout/stderr/exit_code.",
    input_model=ShellInput,
    tags=("shell",),
)
async def shell(command: str, timeout_sec: int | None = None) -> dict[str, object]:
    policy = get_policy().for_tool("shell")
    timeout = timeout_sec or policy.timeout_sec or 60
    cwd = session_workspace(current().session_id)
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
