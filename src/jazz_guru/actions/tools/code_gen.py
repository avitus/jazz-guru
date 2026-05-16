from __future__ import annotations

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.registry import registry
from jazz_guru.actions.sandbox import resolve_in_workspace
from jazz_guru.actions.tools.patch import _patch_file


class CodeGenInput(BaseModel):
    path: str = Field(..., description="Target path (relative to session workspace).")
    content: str = Field(..., description="Full file contents to write.")
    overwrite: bool = True


class CodeEditInput(BaseModel):
    path: str
    old_str: str
    new_str: str
    change_all: bool = False


@registry.register(
    "code_gen",
    description="Create or replace a source file in the workspace.",
    input_model=CodeGenInput,
    tags=("code",),
)
async def code_gen(path: str, content: str, overwrite: bool = True) -> dict[str, object]:
    p = resolve_in_workspace(path, current().session_id)
    if p.exists() and not overwrite:
        return {"path": str(p), "written": False, "reason": "exists"}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"path": str(p), "written": True, "bytes": len(content.encode("utf-8"))}


@registry.register(
    "code_edit",
    description=(
        "Find-and-replace edit a workspace file. Thin back-compat wrapper around "
        "`patch` — tries exact first, then line-trimmed, then fuzzy match. Set "
        "change_all=true to allow multiple exact matches. Prefer `patch` for new "
        "code: it exposes min_ratio and a syntax-check toggle and returns a "
        "unified diff."
    ),
    input_model=CodeEditInput,
    tags=("code",),
)
async def code_edit(
    path: str, old_str: str, new_str: str, change_all: bool = False
) -> dict[str, object]:
    return await _patch_file(
        path,
        old_str,
        new_str,
        change_all=change_all,
        # Default min_ratio of 1.0 preserves the strict behavior the old
        # code_edit had (exact + line-trimmed only). Callers that want fuzzy
        # matching should use the `patch` tool directly.
        min_ratio=1.0,
        syntax_check=True,
    )
