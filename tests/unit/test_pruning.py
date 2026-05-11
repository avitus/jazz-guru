from __future__ import annotations

import json
from pathlib import Path

import pytest

from jazz_guru.actions.context import ToolContext
from jazz_guru.actions.pruning import prune_tool_result
from jazz_guru.config import ToolPolicy, get_settings


@pytest.fixture
def isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(get_settings(), "jg_workspace_dir", tmp_path)
    return tmp_path


def _ctx() -> ToolContext:
    return ToolContext(session_id="test-session", turn_idx=3)


def test_small_result_passes_through(isolated_workspace: Path) -> None:
    value = {"path": "a.txt", "size": 5, "content": "hello"}
    visible, manifest = prune_tool_result(
        "fs_read",
        value,
        ctx=_ctx(),
        policy=ToolPolicy(),
        default_max_bytes=10_000,
    )
    assert visible is value
    assert manifest is None


def test_fs_read_large_result_pruned(isolated_workspace: Path) -> None:
    big = "x" * 50_000
    value = {"path": "/tmp/big.txt", "size": len(big), "content": big}
    visible, manifest = prune_tool_result(
        "fs_read",
        value,
        ctx=_ctx(),
        policy=ToolPolicy(),
        default_max_bytes=10_000,
    )
    assert manifest is not None
    assert manifest["tool"] == "fs_read"
    assert manifest["size_bytes"] >= 50_000

    # Visible value keeps the small scalars and shrinks the content.
    assert visible["path"] == value["path"]
    assert visible["size"] == value["size"]
    assert visible["content"]["truncated_to_disk"] is True
    assert visible["content"]["preview"].startswith("xxx")
    assert len(visible["content"]["preview"]) < 1000

    # Full payload is recoverable from disk and round-trips byte-for-byte.
    full = json.loads(Path(manifest["path"]).read_text(encoding="utf-8"))
    assert full == value


def test_shell_large_stdout_pruned(isolated_workspace: Path) -> None:
    stdout = "line\n" * 5_000  # 25KB of newlines
    value = {"exit_code": 0, "stdout": stdout, "stderr": "small err"}
    visible, manifest = prune_tool_result(
        "shell",
        value,
        ctx=_ctx(),
        policy=ToolPolicy(),
        default_max_bytes=10_000,
    )
    assert manifest is not None
    assert visible["exit_code"] == 0
    assert visible["stdout"]["lines"] == 5_000
    assert "preview" in visible["stdout"]
    # Small stderr stays as-is.
    assert visible["stderr"] == "small err"


def test_shell_small_passes_through(isolated_workspace: Path) -> None:
    value = {"exit_code": 0, "stdout": "hi\n", "stderr": ""}
    visible, manifest = prune_tool_result(
        "shell",
        value,
        ctx=_ctx(),
        policy=ToolPolicy(),
        default_max_bytes=10_000,
    )
    assert visible is value
    assert manifest is None


def test_http_get_large_body_pruned(isolated_workspace: Path) -> None:
    body = "<html>" + ("a" * 50_000) + "</html>"
    value = {
        "status_code": 200,
        "headers": {"content-type": "text/html"},
        "body": body,
        "truncated": False,
        "final_url": "https://example.com/",
    }
    visible, manifest = prune_tool_result(
        "http_get",
        value,
        ctx=_ctx(),
        policy=ToolPolicy(),
        default_max_bytes=10_000,
    )
    assert manifest is not None
    assert visible["status_code"] == 200
    assert visible["final_url"] == "https://example.com/"
    assert visible["body"]["truncated_to_disk"] is True
    assert "preview" in visible["body"]


def test_http_error_response_passes_through(isolated_workspace: Path) -> None:
    value = {"error": "blocked", "reason": "private IP"}
    visible, manifest = prune_tool_result(
        "http_get",
        value,
        ctx=_ctx(),
        policy=ToolPolicy(),
        default_max_bytes=10_000,
    )
    assert visible is value
    assert manifest is None


def test_per_tool_override_beats_default(isolated_workspace: Path) -> None:
    # 5KB content fits the default 10KB but not the per-tool 4KB override.
    big = "x" * 5_000
    value = {"path": "x.txt", "size": len(big), "content": big}
    visible, manifest = prune_tool_result(
        "fs_read",
        value,
        ctx=_ctx(),
        policy=ToolPolicy(max_result_bytes=4_000),
        default_max_bytes=10_000,
    )
    assert manifest is not None
    assert visible["content"]["truncated_to_disk"] is True


def test_unknown_tool_uses_generic_summary(isolated_workspace: Path) -> None:
    big = "z" * 30_000
    value = {"label": "ok", "blob": big, "count": 42}
    visible, manifest = prune_tool_result(
        "some_future_tool",
        value,
        ctx=_ctx(),
        policy=ToolPolicy(),
        default_max_bytes=10_000,
    )
    assert manifest is not None
    assert visible["label"] == "ok"  # small string preserved
    assert visible["count"] == 42  # scalar preserved
    assert visible["blob"]["truncated_to_disk"] is True


def test_unknown_tool_string_result(isolated_workspace: Path) -> None:
    big = "q" * 20_000
    visible, manifest = prune_tool_result(
        "weird_tool",
        big,
        ctx=_ctx(),
        policy=ToolPolicy(),
        default_max_bytes=10_000,
    )
    assert manifest is not None
    assert visible["truncated_to_disk"] is True
    full = Path(manifest["path"]).read_text(encoding="utf-8")
    assert full == big


def test_manifest_path_lives_under_session_workspace(isolated_workspace: Path) -> None:
    big = "y" * 20_000
    value = {"content": big, "path": "x", "size": 0}
    _, manifest = prune_tool_result(
        "fs_read",
        value,
        ctx=_ctx(),
        policy=ToolPolicy(),
        default_max_bytes=10_000,
    )
    assert manifest is not None
    p = Path(manifest["path"])
    assert p.exists()
    assert p.parent.name == "tool_outputs"
    assert "test-session" in str(p)
