from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.registry import registry
from jazz_guru.actions.sandbox import resolve_in_safe, resolve_in_workspace, session_workspace

_READ_PATH_HELP = (
    "Path to read. Relative paths resolve against the session workspace, "
    "then the project root — so `notes.md` reads from your workspace and "
    "`data/wjazzd/wjazzd-index.json` reads project data. Absolute paths "
    "are accepted if they fall under a safe root (session workspace, "
    "`data/`, the instruments library, or any `JG_SAFE_EXTRA_PATHS`)."
)


class FsReadInput(BaseModel):
    path: str = Field(..., description=_READ_PATH_HELP)
    encoding: str = Field("utf-8", description="Text encoding.")
    max_bytes: int = Field(200_000, description="Max bytes to return.")


class FsWriteInput(BaseModel):
    path: str = Field(..., description="Path inside the session workspace.")
    content: str
    mode: Literal["overwrite", "append"] = "overwrite"
    encoding: str = "utf-8"


class FsListInput(BaseModel):
    path: str = Field(".", description=_READ_PATH_HELP)
    recursive: bool = False


@registry.register(
    "fs_read",
    description=(
        "Read a UTF-8 text file. Can read from the session workspace or any "
        "safe project root — `data/wjazzd/` (the Weimar Jazz Database), "
        "`data/`, and the instruments library are all reachable. Writes "
        "still go through `fs_write` and are workspace-only."
    ),
    input_model=FsReadInput,
    tags=("fs",),
)
async def fs_read(path: str, encoding: str = "utf-8", max_bytes: int = 200_000) -> dict[str, str | int]:
    p = resolve_in_safe(path, current().session_id)
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
    description=(
        "List files/dirs under a path. Can read the session workspace or any "
        "safe project root — e.g. `data/wjazzd/` for the Weimar Jazz Database."
    ),
    input_model=FsListInput,
    tags=("fs",),
)
async def fs_list(path: str = ".", recursive: bool = False) -> dict[str, list[str]]:
    base = session_workspace(current().session_id)
    target = resolve_in_safe(path, current().session_id)
    if not target.exists():
        return {"entries": []}
    # Entries are reported relative to the workspace when the target is inside
    # it; otherwise we report absolute paths so cross-root listings are
    # unambiguous (e.g. listing `data/wjazzd/`).
    base_resolved = base.resolve()
    target_resolved = target.resolve()
    try:
        target_resolved.relative_to(base_resolved)
        rel_base: Path | None = base_resolved
    except ValueError:
        rel_base = None

    def _fmt(p: Path) -> str:
        if rel_base is None:
            return str(p)
        return str(p.relative_to(rel_base))

    entries: list[str] = []
    if recursive:
        for p in sorted(target.rglob("*")):
            entries.append(_fmt(p))
    else:
        for p in sorted(target.iterdir()):
            entries.append(_fmt(p))
    return {"entries": entries}
