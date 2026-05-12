"""Tools for reading/writing the always-loaded ``AGENT_NOTES.md`` and ``USER.md``.

These are the "instant facts" tier above the pgvector store: small, lossless,
always present in the system prompt. The reflexion loop can also write to them
via the ``notes_patches`` field of its JSON contract.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from jazz_guru.actions.registry import registry
from jazz_guru.notes import (
    AGENT_NOTES_CAP,
    USER_NOTES_CAP,
    NotesError,
    normalize_key,
    patch_notes,
    read_notes,
    write_notes,
)


class NotesReadInput(BaseModel):
    file: str | None = Field(
        default=None,
        description=(
            "Which notes file to read: 'AGENT_NOTES' or 'USER'. Omit to read both."
        ),
    )


@registry.register(
    "notes_read",
    description=(
        "Read the always-loaded persistent notes. AGENT_NOTES.md "
        f"(<= {AGENT_NOTES_CAP} chars) holds durable environment facts, project "
        f"conventions, and learned techniques. USER.md (<= {USER_NOTES_CAP} chars) "
        "holds the operator's profile and preferences. Both files are already "
        "injected into your system prompt every turn; use this tool only when "
        "you need the raw text (e.g. before notes_patch)."
    ),
    input_model=NotesReadInput,
    tags=("notes",),
)
async def notes_read(file: str | None = None) -> dict[str, Any]:
    notes = read_notes()
    if file is None:
        return {
            "ok": True,
            "agent_notes": notes["AGENT_NOTES"],
            "user": notes["USER"],
            "caps": {"AGENT_NOTES": AGENT_NOTES_CAP, "USER": USER_NOTES_CAP},
        }
    try:
        key = normalize_key(file)
    except NotesError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "file": key, "content": notes[key]}


class NotesWriteInput(BaseModel):
    file: str = Field(..., description="'AGENT_NOTES' or 'USER'.")
    content: str = Field(
        ...,
        description=(
            "Full replacement content. Subject to the per-file character cap "
            "(2500 for AGENT_NOTES, 1500 for USER). Prefer notes_patch for "
            "small edits."
        ),
    )


@registry.register(
    "notes_write",
    description=(
        "Replace the entire contents of a notes file. Use for the first write "
        "or a full rewrite. Subject to a hard per-file char cap."
    ),
    input_model=NotesWriteInput,
    tags=("notes",),
)
async def notes_write(file: str, content: str) -> dict[str, Any]:
    try:
        info = write_notes(file, content)
    except NotesError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **info}


class NotesPatchInput(BaseModel):
    file: str = Field(..., description="'AGENT_NOTES' or 'USER'.")
    find: str = Field(
        ...,
        description=(
            "Exact substring to replace. Must appear exactly once in the file."
        ),
    )
    replace: str = Field(..., description="Replacement string.")


@registry.register(
    "notes_patch",
    description=(
        "Apply a surgical find-and-replace to a notes file. The find string "
        "must match exactly once; if ambiguous, expand it with more surrounding "
        "context. Rejected if the patch would exceed the per-file cap."
    ),
    input_model=NotesPatchInput,
    tags=("notes",),
)
async def notes_patch(file: str, find: str, replace: str) -> dict[str, Any]:
    try:
        info = patch_notes(file, find, replace)
    except NotesError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **info}
