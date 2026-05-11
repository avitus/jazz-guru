from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.registry import registry
from jazz_guru.actions.sandbox import resolve_in_workspace, session_workspace


class FsReadInput(BaseModel):
    path: str = Field(..., description="Path inside the session workspace.")
    encoding: str = Field("utf-8", description="Text encoding.")
    max_bytes: int = Field(200_000, description="Max bytes to return.")


class FsWriteInput(BaseModel):
    path: str
    content: str
    mode: Literal["overwrite", "append"] = "overwrite"
    encoding: str = "utf-8"


class FsListInput(BaseModel):
    path: str = Field(".", description="Subdirectory under the workspace.")
    recursive: bool = False


@registry.register(
    "fs_read",
    description="Read a UTF-8 text file from the session workspace.",
    input_model=FsReadInput,
    tags=("fs",),
)
async def fs_read(path: str, encoding: str = "utf-8", max_bytes: int = 200_000) -> dict[str, str | int]:
    p = resolve_in_workspace(path, current().session_id)
    data = p.read_bytes()[:max_bytes]
    return {"path": str(p), "size": len(data), "content": data.decode(encoding, errors="replace")}


@registry.register(
    "fs_write",
    description="Write text to a file in the session workspace (overwrite or append).",
    input_model=FsWriteInput,
    tags=("fs",),
)
async def fs_write(path: str, content: str, mode: str = "overwrite", encoding: str = "utf-8") -> dict[str, str | int]:
    p = resolve_in_workspace(path, current().session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    flag = "w" if mode == "overwrite" else "a"
    with p.open(flag, encoding=encoding) as fh:
        fh.write(content)
    return {"path": str(p), "bytes_written": len(content.encode(encoding))}


@registry.register(
    "fs_list",
    description="List files/dirs under a workspace path.",
    input_model=FsListInput,
    tags=("fs",),
)
async def fs_list(path: str = ".", recursive: bool = False) -> dict[str, list[str]]:
    base = session_workspace(current().session_id)
    target = resolve_in_workspace(path, current().session_id)
    if not target.exists():
        return {"entries": []}
    entries: list[str] = []
    if recursive:
        for p in sorted(target.rglob("*")):
            entries.append(str(p.relative_to(base)))
    else:
        for p in sorted(target.iterdir()):
            entries.append(str(p.relative_to(base)))
    return {"entries": entries}
