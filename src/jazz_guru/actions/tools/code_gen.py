from __future__ import annotations

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.registry import registry
from jazz_guru.actions.sandbox import resolve_in_workspace


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
    description="Find-and-replace edit a workspace file. old_str must occur exactly once unless change_all=true.",
    input_model=CodeEditInput,
    tags=("code",),
)
async def code_edit(path: str, old_str: str, new_str: str, change_all: bool = False) -> dict[str, object]:
    p = resolve_in_workspace(path, current().session_id)
    text = p.read_text(encoding="utf-8")
    count = text.count(old_str)
    if count == 0:
        return {"path": str(p), "edited": False, "reason": "old_str not found"}
    if count > 1 and not change_all:
        return {"path": str(p), "edited": False, "reason": f"old_str matches {count} times; set change_all=true"}
    new_text = text.replace(old_str, new_str) if change_all else text.replace(old_str, new_str, 1)
    p.write_text(new_text, encoding="utf-8")
    return {"path": str(p), "edited": True, "replacements": count if change_all else 1}
