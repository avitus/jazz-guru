from __future__ import annotations

import asyncio
import sys
import textwrap

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.registry import registry
from jazz_guru.actions.sandbox import session_workspace
from jazz_guru.config import get_policy


class PythonExecInput(BaseModel):
    code: str = Field(..., description="Python source. Runs in a fresh subprocess with cwd=workspace.")
    timeout_sec: int | None = None
    stdin: str | None = None


@registry.register(
    "python_exec",
    description="Execute Python code in an ephemeral subprocess (cwd=session workspace).",
    input_model=PythonExecInput,
    tags=("code",),
)
async def python_exec(code: str, timeout_sec: int | None = None, stdin: str | None = None) -> dict[str, object]:
    policy = get_policy().for_tool("python_exec")
    timeout = timeout_sec or policy.timeout_sec or 30
    cwd = session_workspace(current().session_id)
    src = textwrap.dedent(code)
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
