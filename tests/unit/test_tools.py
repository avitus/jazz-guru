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
async def test_fs_read_can_reach_project_data(
    isolated_workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point jg_data_dir at a sibling tmp directory and drop a fake WJazzD file in.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "wjazzd").mkdir()
    (data_dir / "wjazzd" / "index.json").write_text('{"hits": 42}')
    monkeypatch.setattr(get_settings(), "jg_data_dir", data_dir)

    r = register_all()
    token = set_tool_context(ToolContext(session_id="test"))
    try:
        # Relative path under data/ resolves via the project-root anchor.
        out = await r.invoke("fs_read", {"path": "data/wjazzd/index.json"})
        assert '"hits": 42' in out["content"]

        # fs_list returns absolute paths for cross-workspace listings.
        listing = await r.invoke("fs_list", {"path": "data/wjazzd"})
        assert any(e.endswith("index.json") for e in listing["entries"])
    finally:
        from jazz_guru.actions import reset_tool_context

        reset_tool_context(token)


@pytest.mark.asyncio
async def test_fs_write_still_workspace_only(
    isolated_workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # fs_write must refuse paths that escape the session workspace, even when
    # the target is an otherwise-readable safe root like data/.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(get_settings(), "jg_data_dir", data_dir)

    r = register_all()
    token = set_tool_context(ToolContext(session_id="test"))
    try:
        # Absolute path into data/ — fs_read would allow it; fs_write must not.
        with pytest.raises(PermissionError):
            await r.invoke(
                "fs_write",
                {"path": str(data_dir / "poison.txt"), "content": "no"},
            )
    finally:
        from jazz_guru.actions import reset_tool_context

        reset_tool_context(token)


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
