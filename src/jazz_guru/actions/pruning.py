"""Tool-result pruning.

When a tool returns a payload larger than the configured threshold, persist
the full value to ``workspace/sessions/<sid>/tool_outputs/`` and return a
compact summary that the model can act on (preview + recoverable on-disk
path). The full payload remains reachable via ``fs_read`` of the path, so
the agent can fetch detail when it actually needs it instead of carrying
the whole blob through every subsequent LLM round in the turn.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from jazz_guru.actions.context import ToolContext
from jazz_guru.actions.sandbox import session_workspace
from jazz_guru.config import ToolPolicy

_PREVIEW_CHARS = 300


def _serialize(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return repr(value)


def _write_full(value: Any, name: str, ctx: ToolContext) -> Path:
    base = session_workspace(ctx.session_id) / "tool_outputs"
    base.mkdir(parents=True, exist_ok=True)
    turn = ctx.turn_idx if ctx.turn_idx is not None else 0
    path = base / f"{name}_{turn}_{uuid4().hex[:8]}.json"
    if isinstance(value, (str, bytes)):
        text = value if isinstance(value, str) else value.decode("utf-8", errors="replace")
        path.write_text(text, encoding="utf-8")
    else:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    return path


def _preview(text: str) -> str:
    if len(text) <= _PREVIEW_CHARS:
        return text
    return text[:_PREVIEW_CHARS] + "…"


def _prune_string(text: str, full_path: Path) -> dict[str, Any]:
    return {
        "preview": _preview(text),
        "full_path": str(full_path),
        "size_bytes": len(text.encode("utf-8")),
        "truncated_to_disk": True,
    }


def _handle_fs_read(value: dict[str, Any], full_path: Path) -> dict[str, Any]:
    content = value.get("content", "")
    summary: dict[str, Any] = {k: v for k, v in value.items() if k != "content"}
    summary["content"] = _prune_string(content if isinstance(content, str) else str(content), full_path)
    return summary


def _handle_shell(value: dict[str, Any], full_path: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {"exit_code": value.get("exit_code")}
    for key in ("stdout", "stderr"):
        text = value.get(key, "")
        if not isinstance(text, str):
            text = str(text)
        if len(text.encode("utf-8")) > _PREVIEW_CHARS:
            summary[key] = {
                "preview": _preview(text),
                "lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
                "size_bytes": len(text.encode("utf-8")),
                "full_path": str(full_path),
            }
        else:
            summary[key] = text
    return summary


def _handle_http(value: dict[str, Any], full_path: Path) -> dict[str, Any]:
    if "error" in value:
        return value  # error responses are already small
    summary: dict[str, Any] = {
        k: v for k, v in value.items() if k in {"status_code", "headers", "truncated", "final_url"}
    }
    body = value.get("body", "")
    if not isinstance(body, str):
        body = str(body)
    summary["body"] = _prune_string(body, full_path)
    return summary


_SHAPE_HANDLERS: dict[str, Callable[[dict[str, Any], Path], dict[str, Any]]] = {
    "fs_read": _handle_fs_read,
    "shell": _handle_shell,
    "python_exec": _handle_shell,  # same shape as shell
    "http_get": _handle_http,
    "http_post": _handle_http,
}


def _generic_summary(value: Any, full_path: Path) -> Any:
    """Fallback for tools without a registered shape handler.

    Strings get a preview + path; dicts keep their small scalar fields and
    replace any over-budget string/list values with a preview reference.
    Everything else gets stringified into a single preview block.
    """
    if isinstance(value, str):
        return _prune_string(value, full_path)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(v, (int, float, bool)) or v is None:
                out[k] = v
                continue
            if isinstance(v, str):
                if len(v.encode("utf-8")) > _PREVIEW_CHARS:
                    out[k] = _prune_string(v, full_path)
                else:
                    out[k] = v
                continue
            # nested list/dict: serialize, prune if large
            serialized = _serialize(v)
            if len(serialized.encode("utf-8")) > _PREVIEW_CHARS:
                out[k] = _prune_string(serialized, full_path)
            else:
                out[k] = v
        return out
    return _prune_string(_serialize(value), full_path)


def prune_tool_result(
    name: str,
    value: Any,
    *,
    ctx: ToolContext,
    policy: ToolPolicy,
    default_max_bytes: int,
) -> tuple[Any, dict[str, Any] | None]:
    """Return (visible_value, manifest_or_none).

    ``visible_value`` is what gets serialized into the LLM-visible
    ``tool_result`` block. ``manifest`` is non-None only when a full payload
    was written to disk; consumers (trace, WS) can surface it for debugging
    and the agent can re-read the file via ``fs_read``.
    """
    serialized = _serialize(value)
    size = len(serialized.encode("utf-8"))
    threshold = policy.max_result_bytes if policy.max_result_bytes is not None else default_max_bytes
    if size <= threshold:
        return value, None

    full_path = _write_full(value, name, ctx)
    handler = _SHAPE_HANDLERS.get(name)
    if handler is not None and isinstance(value, dict):
        visible = handler(value, full_path)
    else:
        visible = _generic_summary(value, full_path)
    manifest = {"path": str(full_path), "size_bytes": size, "tool": name}
    return visible, manifest
