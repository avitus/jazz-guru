from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jazz_guru.actions.context import ToolContext, reset_tool_context, set_tool_context
from jazz_guru.actions.jobs import (
    BackgroundJobRegistry,
    attach_jobs,
    detach_jobs,
)
from jazz_guru.actions.registry import register_all, registry
from jazz_guru.config import get_settings


@pytest.fixture
def isolated_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "jg_workspace_dir", tmp_path)
    register_all()
    reg = BackgroundJobRegistry()
    jt = attach_jobs(reg)
    tok = set_tool_context(ToolContext(session_id="test", turn_idx=0))
    yield tmp_path, reg
    reset_tool_context(tok)
    detach_jobs(jt)


async def _wait_complete(reg: BackgroundJobRegistry, job_id: str, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        j = reg.get(job_id)
        if j and j.is_done:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} did not complete within {timeout}s")


async def test_python_exec_background_returns_job(isolated_session) -> None:
    _, reg = isolated_session
    out = await registry.invoke(
        "python_exec",
        {"code": "import time, sys; sys.stdout.write('hi\\n'); time.sleep(0.05)", "background": True},
    )
    assert out["started"] is True
    job_id = out["job_id"]
    assert job_id in reg
    await _wait_complete(reg, job_id)
    job = reg.get(job_id)
    assert job is not None
    assert job.status() == "completed"
    assert job.exit_code == 0
    text = job.stdout_path.read_text()
    assert "hi" in text


async def test_process_list_returns_jobs(isolated_session) -> None:
    _, _reg = isolated_session
    out = await registry.invoke(
        "python_exec", {"code": "print('x')", "background": True}
    )
    listing = await registry.invoke("process", {"action": "list"})
    assert listing["ok"] is True
    ids = [j["id"] for j in listing["jobs"]]
    assert out["job_id"] in ids


async def test_process_poll_reads_stdout(isolated_session) -> None:
    _, reg = isolated_session
    out = await registry.invoke(
        "python_exec",
        {"code": "print('hello'); print('world')", "background": True},
    )
    jid = out["job_id"]
    await _wait_complete(reg, jid)
    polled = await registry.invoke("process", {"action": "poll", "id": jid})
    assert polled["ok"] is True
    assert polled["status"] == "completed"
    text = polled["stdout"]["text"]
    assert "hello" in text
    assert "world" in text


async def test_process_wait_blocks_until_exit(isolated_session) -> None:
    _, _ = isolated_session
    out = await registry.invoke(
        "python_exec",
        {"code": "import time; time.sleep(0.1); print('done')", "background": True},
    )
    waited = await registry.invoke(
        "process", {"action": "wait", "id": out["job_id"], "timeout_sec": 5}
    )
    assert waited["ok"] is True
    assert waited["status"] == "completed"


async def test_process_wait_times_out(isolated_session) -> None:
    _, _ = isolated_session
    out = await registry.invoke(
        "python_exec",
        {"code": "import time; time.sleep(5)", "background": True},
    )
    waited = await registry.invoke(
        "process", {"action": "wait", "id": out["job_id"], "timeout_sec": 0.2}
    )
    assert waited["ok"] is False
    assert waited["timed_out"] is True
    # kill it to clean up
    await registry.invoke("process", {"action": "kill", "id": out["job_id"]})


async def test_process_kill_terminates_job(isolated_session) -> None:
    _, reg = isolated_session
    out = await registry.invoke(
        "python_exec",
        {"code": "import time; time.sleep(5)", "background": True},
    )
    killed = await registry.invoke("process", {"action": "kill", "id": out["job_id"]})
    assert killed["ok"] is True
    job = reg.get(out["job_id"])
    assert job is not None
    assert job.cancelled is True


async def test_process_log_paginates(isolated_session) -> None:
    _, reg = isolated_session
    out = await registry.invoke(
        "python_exec",
        {
            "code": "for i in range(10): print(f'line {i}')",
            "background": True,
        },
    )
    await _wait_complete(reg, out["job_id"])
    chunk = await registry.invoke(
        "process",
        {"action": "log", "id": out["job_id"], "offset": 0, "limit": 50, "stream": "stdout"},
    )
    assert chunk["ok"] is True
    assert chunk["stdout"]["size"] > 50
    # Read next chunk
    next_offset = chunk["stdout"]["next_offset"]
    chunk2 = await registry.invoke(
        "process",
        {
            "action": "log",
            "id": out["job_id"],
            "offset": next_offset,
            "limit": 50,
            "stream": "stdout",
        },
    )
    assert chunk2["stdout"]["offset"] == next_offset


async def test_process_rejects_unknown_id(isolated_session) -> None:
    _, _ = isolated_session
    out = await registry.invoke(
        "process", {"action": "poll", "id": "nonexistent"}
    )
    assert out["ok"] is False
    assert "no such job" in out["error"]


async def test_process_unknown_action(isolated_session) -> None:
    _, _ = isolated_session
    # 'frobnicate' with no id is rejected for missing id first; supply a known job.
    out = await registry.invoke(
        "python_exec", {"code": "print('x')", "background": True}
    )
    bad = await registry.invoke(
        "process", {"action": "frobnicate", "id": out["job_id"]}
    )
    assert bad["ok"] is False
    assert "unknown action" in bad["error"]


async def test_shell_background(isolated_session) -> None:
    _, reg = isolated_session
    out = await registry.invoke(
        "shell", {"command": "echo greetings && sleep 0.05", "background": True}
    )
    assert "job_id" in out
    await _wait_complete(reg, out["job_id"])
    job = reg.get(out["job_id"])
    assert job is not None
    assert "greetings" in job.stdout_path.read_text()
