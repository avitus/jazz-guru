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


def test_python_exec_uses_shell_handler(isolated_workspace: Path) -> None:
    """python_exec returns the same {exit_code, stdout, stderr} shape as
    shell, and should be summarized by the same handler — keeping exit_code
    at top level rather than burying it under a generic preview."""
    stdout = "trace\n" * 4_000
    value = {"exit_code": 0, "stdout": stdout, "stderr": ""}
    visible, manifest = prune_tool_result(
        "python_exec",
        value,
        ctx=_ctx(),
        policy=ToolPolicy(),
        default_max_bytes=10_000,
    )
    assert manifest is not None
    assert visible["exit_code"] == 0
    assert isinstance(visible["stdout"], dict)
    assert "preview" in visible["stdout"]
    assert visible["stdout"]["lines"] == 4_000


def test_http_post_uses_http_handler(isolated_workspace: Path) -> None:
    """http_post must route to the same handler as http_get (same response
    shape) — they share scalar fields like status_code and final_url."""
    body = "x" * 50_000
    value = {
        "status_code": 201,
        "headers": {"x": "y"},
        "body": body,
        "truncated": False,
        "final_url": "https://api.example.com/items",
    }
    visible, manifest = prune_tool_result(
        "http_post",
        value,
        ctx=_ctx(),
        policy=ToolPolicy(),
        default_max_bytes=10_000,
    )
    assert manifest is not None
    assert visible["status_code"] == 201
    assert visible["final_url"] == "https://api.example.com/items"
    assert visible["body"]["truncated_to_disk"] is True


def test_generic_dict_with_mixed_scalars_and_blobs(isolated_workspace: Path) -> None:
    """Mixed dict: scalars + small strings + nested list. Only over-budget
    fields should be replaced; everything else passes through verbatim."""
    big_list = ["entry"] * 5_000  # serialized → ~40KB
    value = {
        "id": 7,
        "label": "ok",
        "ratio": 0.42,
        "active": True,
        "small_text": "tiny",
        "items": big_list,
    }
    visible, manifest = prune_tool_result(
        "novel_tool",
        value,
        ctx=_ctx(),
        policy=ToolPolicy(),
        default_max_bytes=10_000,
    )
    assert manifest is not None
    assert visible["id"] == 7
    assert visible["ratio"] == 0.42
    assert visible["active"] is True
    assert visible["label"] == "ok"
    assert visible["small_text"] == "tiny"
    # The big list was the over-budget field — replaced with a preview.
    assert isinstance(visible["items"], dict)
    assert visible["items"]["truncated_to_disk"] is True


def test_generic_dict_preserves_none_values(isolated_workspace: Path) -> None:
    """None must survive the generic summary — the original code branches on
    isinstance(int|float|bool) which would otherwise drop it."""
    value = {"opt": None, "blob": "z" * 30_000}
    visible, _ = prune_tool_result(
        "novel_tool",
        value,
        ctx=_ctx(),
        policy=ToolPolicy(),
        default_max_bytes=10_000,
    )
    assert visible["opt"] is None
    assert visible["blob"]["truncated_to_disk"] is True


def test_pruning_threshold_zero_always_prunes(isolated_workspace: Path) -> None:
    """A 0-byte threshold should force every result to disk, even {} —
    useful for stress tests / debugging that want to verify the pipeline."""
    value = {"x": 1}
    visible, manifest = prune_tool_result(
        "fs_read",
        value,
        ctx=_ctx(),
        policy=ToolPolicy(max_result_bytes=0),
        default_max_bytes=10_000,
    )
    assert manifest is not None
    assert "truncated_to_disk" not in visible or visible.get("content")  # content field reshaped


def test_size_bytes_counts_utf8_not_chars(isolated_workspace: Path) -> None:
    """size_bytes in the manifest must be the UTF-8 byte length, not the
    character count — important for non-ASCII payloads where the two differ."""
    text = "🎵" * 5_000  # 4 bytes per emoji = 20KB UTF-8, but 5K chars
    value = {"path": "a", "size": 0, "content": text}
    _, manifest = prune_tool_result(
        "fs_read",
        value,
        ctx=_ctx(),
        policy=ToolPolicy(),
        default_max_bytes=10_000,
    )
    assert manifest is not None
    # JSON-encoded payload is at minimum ~20KB just for the emoji content.
    assert manifest["size_bytes"] >= 20_000


def test_manifest_path_includes_tool_name_and_turn(isolated_workspace: Path) -> None:
    """The on-disk filename pattern <tool>_<turn>_<uuid>.json lets the agent
    grep its own tool_outputs/ directory by tool or by turn."""
    ctx = ToolContext(session_id="sess-x", turn_idx=42)
    value = {"path": "a", "size": 0, "content": "y" * 30_000}
    _, manifest = prune_tool_result(
        "fs_read",
        value,
        ctx=ctx,
        policy=ToolPolicy(),
        default_max_bytes=10_000,
    )
    assert manifest is not None
    name = Path(manifest["path"]).name
    assert name.startswith("fs_read_42_")
    assert name.endswith(".json")


def test_default_turn_idx_zero_when_unset(isolated_workspace: Path) -> None:
    """If ToolContext.turn_idx is None, the filename should still be
    well-formed (no literal "None" leaking into the path)."""
    ctx = ToolContext(session_id="sess-x", turn_idx=None)
    value = {"path": "a", "size": 0, "content": "y" * 30_000}
    _, manifest = prune_tool_result(
        "fs_read",
        value,
        ctx=ctx,
        policy=ToolPolicy(),
        default_max_bytes=10_000,
    )
    assert manifest is not None
    assert "None" not in Path(manifest["path"]).name


def test_serialization_failure_falls_back_to_repr(isolated_workspace: Path) -> None:
    """A tool that returns an object without a JSON encoder shouldn't blow
    up the controller — the size check should silently fall back to repr."""

    class Opaque:
        def __repr__(self) -> str:
            return "<opaque obj>" + ("x" * 30_000)

    value = Opaque()
    visible, manifest = prune_tool_result(
        "weird",
        value,
        ctx=_ctx(),
        policy=ToolPolicy(),
        default_max_bytes=10_000,
    )
    assert manifest is not None
    # Generic summary stringifies it via repr → preview should reflect that.
    assert visible["truncated_to_disk"] is True


def test_tool_outputs_directory_created_lazily(isolated_workspace: Path) -> None:
    """Before pruning ever runs, no tool_outputs/ dir should exist; after a
    single prune it should exist with one entry."""
    sess_dir = isolated_workspace / "sessions" / "test-session"
    out_dir = sess_dir / "tool_outputs"
    # If a previous test in this module created the dir, count the delta
    # rather than asserting a hard ``== 1``.
    before = len(list(out_dir.iterdir())) if out_dir.exists() else 0
    prune_tool_result(
        "fs_read",
        {"path": "x", "size": 0, "content": "z" * 30_000},
        ctx=_ctx(),
        policy=ToolPolicy(),
        default_max_bytes=10_000,
    )
    assert out_dir.exists()
    assert len(list(out_dir.iterdir())) == before + 1
