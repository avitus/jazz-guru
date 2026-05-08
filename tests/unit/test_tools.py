from __future__ import annotations

from pathlib import Path

import pytest

from jazz_guru.actions import ToolContext, register_all, set_tool_context
from jazz_guru.actions.sandbox import resolve_in_workspace
from jazz_guru.config import get_settings


@pytest.fixture
def isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(get_settings(), "jg_workspace_dir", tmp_path)
    return tmp_path


def test_registry_lists_expected_tools() -> None:
    r = register_all()
    names = set(r.names())
    for name in {"fs_read", "fs_write", "fs_list", "shell", "http_get", "python_exec", "code_gen", "code_edit", "tts"}:
        assert name in names


def test_registry_anthropic_schemas_have_required_fields() -> None:
    r = register_all()
    for spec in r.all_specs():
        a = spec.to_anthropic()
        assert a["name"] == spec.name
        assert "description" in a
        assert "input_schema" in a
        assert a["input_schema"].get("type") == "object"


@pytest.mark.asyncio
async def test_fs_write_then_read(isolated_workspace: Path) -> None:
    r = register_all()
    token = set_tool_context(ToolContext(session_id="test"))
    try:
        await r.invoke("fs_write", {"path": "hello.txt", "content": "world"})
        out = await r.invoke("fs_read", {"path": "hello.txt"})
        assert out["content"] == "world"
    finally:
        from jazz_guru.actions import reset_tool_context

        reset_tool_context(token)


@pytest.mark.asyncio
async def test_code_edit(isolated_workspace: Path) -> None:
    r = register_all()
    token = set_tool_context(ToolContext(session_id="test"))
    try:
        await r.invoke("code_gen", {"path": "a.py", "content": "x = 1\n"})
        res = await r.invoke("code_edit", {"path": "a.py", "old_str": "1", "new_str": "2"})
        assert res["edited"] is True
        rd = await r.invoke("fs_read", {"path": "a.py"})
        assert "x = 2" in rd["content"]
    finally:
        from jazz_guru.actions import reset_tool_context

        reset_tool_context(token)


def test_resolve_in_workspace_blocks_escape(isolated_workspace: Path) -> None:
    with pytest.raises(PermissionError):
        resolve_in_workspace("../../etc/passwd", session_id="test")


@pytest.mark.asyncio
async def test_python_exec_runs(isolated_workspace: Path) -> None:
    r = register_all()
    token = set_tool_context(ToolContext(session_id="test"))
    try:
        out = await r.invoke("python_exec", {"code": "print(1+1)", "timeout_sec": 10})
        assert out["exit_code"] == 0
        assert "2" in out["stdout"]
    finally:
        from jazz_guru.actions import reset_tool_context

        reset_tool_context(token)


@pytest.mark.asyncio
async def test_shell_echo(isolated_workspace: Path) -> None:
    r = register_all()
    token = set_tool_context(ToolContext(session_id="test"))
    try:
        out = await r.invoke("shell", {"command": "echo hi"})
        assert out["exit_code"] == 0
        assert "hi" in out["stdout"]
    finally:
        from jazz_guru.actions import reset_tool_context

        reset_tool_context(token)
