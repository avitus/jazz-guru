from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import textwrap

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.jobs import start_background_subprocess
from jazz_guru.actions.registry import registry
from jazz_guru.actions.rpc import ToolRPCServer, build_rpc_prelude
from jazz_guru.actions.sandbox import get_or_create_session_repl, session_workspace
from jazz_guru.actions.sandbox_profile import wrap_subprocess
from jazz_guru.config import get_policy, get_settings


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
    rpc_tools: bool = Field(
        default=True,
        description=(
            "When true (default), the subprocess gets a `tools` proxy that "
            "calls back into the host tool registry via a per-call Unix "
            "socket. Use `tools.render_midi(...)`, `tools.web_search(...)`, "
            "etc. directly inside your script -- each call enforces the "
            "current policy and emits trace events. Set rpc_tools=false to "
            "run a fully isolated script."
        ),
    )
    backend: str = Field(
        default="ephemeral",
        description=(
            "'ephemeral' (default) spawns a fresh `python -I` subprocess per "
            "call -- no state survives. 'persistent' routes the code into a "
            "long-running per-session Python REPL that keeps imports, cwd, "
            "and module state alive across calls. The persistent backend "
            "does NOT expose the RPC `tools` proxy."
        ),
    )


def _allowed_for_rpc() -> set[str]:
    """Compute the set of tools the RPC proxy is allowed to call.

    Mirrors :meth:`ActionController._allowed_set` but is callable without an
    active controller (e.g. when python_exec runs as a stand-alone tool). The
    feature_flag gate is preserved.
    """
    s = get_settings()
    policy = get_policy()
    allowed: set[str] = set()
    for name in registry.names():
        tp = policy.for_tool(name)
        if tp.mode != "allow":
            continue
        if tp.feature_flag and not getattr(s, tp.feature_flag.lower(), 0):
            continue
        allowed.add(name)
    # Prevent obvious RPC re-entry that would make budget tracking confusing.
    allowed.discard("python_exec")
    return allowed


@registry.register(
    "python_exec",
    description=(
        "Execute Python code in an ephemeral subprocess (cwd=session workspace). "
        "By default the subprocess gets a `tools` proxy that calls back into the "
        "host tool registry via RPC -- use `tools.render_midi(...)` etc. inside "
        "your script to compose tool calls without burning LLM rounds. Set "
        "rpc_tools=false to run a fully isolated script. Set background=true "
        "for long jobs (poll/log via the `process` tool)."
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
    rpc_tools: bool = True,
    backend: str = "ephemeral",
) -> dict[str, object]:
    cwd = session_workspace(current().session_id)
    src_user = textwrap.dedent(code)

    if backend == "persistent":
        if background:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": "background=true is not supported with backend='persistent'",
            }
        policy = get_policy().for_tool("python_exec")
        timeout = float(timeout_sec or policy.timeout_sec or 30)
        repl = get_or_create_session_repl(current().session_id)
        resp = await repl.execute(src_user, timeout_sec=timeout)
        return {
            "exit_code": 0 if resp.get("ok", False) else 1,
            "stdout": str(resp.get("stdout", "")),
            "stderr": str(resp.get("stderr", "")),
            "backend": "persistent",
            "error": resp.get("error"),
        }
    if backend != "ephemeral":
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"unknown backend {backend!r}",
        }

    # When background=true, RPC isn't supported in this initial version. A
    # background-tool RPC server would need to outlive this call; revisit if
    # the use case appears. For now, just run the user code as-is.
    if background:
        job = await start_background_subprocess(
            "python_exec",
            [sys.executable, "-I", "-c", src_user],
            cwd=cwd,
            stdin_bytes=(stdin or "").encode("utf-8") if stdin else None,
            notify_on_complete=notify_on_complete,
            meta={"code_bytes": len(src_user)},
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

    rpc_server: ToolRPCServer | None = None
    rpc_sock: str = ""
    rpc_token: str = ""
    final_src = src_user
    env = os.environ.copy()
    if rpc_tools:
        rpc_server = ToolRPCServer(registry, _allowed_for_rpc())
        try:
            rpc_sock, rpc_token = await rpc_server.start()
        except Exception as e:
            # If RPC can't start (rare: permission, FS), tear down whatever
            # the partial start() left behind (tempdir / token / socket
            # placeholder) so we don't leak artifacts when falling back.
            with contextlib.suppress(Exception):
                await rpc_server.stop()
            rpc_server = None
            final_src = src_user
            env["JG_RPC_DISABLED"] = f"{type(e).__name__}: {e}"
        else:
            final_src = build_rpc_prelude(rpc_sock, rpc_token, src_user)
            env["JG_RPC_SOCK"] = rpc_sock
            env["JG_RPC_TOKEN"] = rpc_token

    argv = wrap_subprocess([sys.executable, "-I", "-c", final_src], cwd)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(input=(stdin or "").encode("utf-8")), timeout=timeout
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"timeout after {timeout}s",
                "rpc_calls": rpc_server.call_count if rpc_server else 0,
            }
    finally:
        if rpc_server is not None:
            await rpc_server.stop()

    result: dict[str, object] = {
        "exit_code": proc.returncode or 0,
        "stdout": out.decode("utf-8", errors="replace"),
        "stderr": err.decode("utf-8", errors="replace"),
    }
    if rpc_server is not None:
        result["rpc_calls"] = rpc_server.call_count
        if rpc_server.errors:
            result["rpc_errors"] = list(rpc_server.errors)
    return result
