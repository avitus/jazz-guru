"""Always-loaded persistent notes.

A small, lossless facts tier that complements the pgvector memory store.
``AGENT_NOTES.md`` holds durable environment facts, project conventions, and
learned techniques. ``USER.md`` holds the operator's profile and preferences.
Both files live at ``workspace/notes/`` and are injected into every system
prompt by :class:`jazz_guru.context.ContextBuilder`. Hard char caps keep the
prompt cache footprint small.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from jazz_guru.config import get_settings

NOTES_DIR_NAME = "notes"
AGENT_NOTES_FILE = "AGENT_NOTES.md"
USER_NOTES_FILE = "USER.md"

AGENT_NOTES_CAP = 2500
USER_NOTES_CAP = 1500

NotesFileKey = Literal["AGENT_NOTES", "USER"]
_VALID_KEYS: tuple[str, ...] = ("AGENT_NOTES", "USER")


class NotesError(ValueError):
    """Raised when a notes operation is rejected (unknown file / cap exceeded / etc)."""


def notes_dir() -> Path:
    """Resolve (and create) the workspace-level notes directory."""
    p = get_settings().jg_workspace_dir / NOTES_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def normalize_key(file: str) -> NotesFileKey:
    """Accept ``AGENT_NOTES`` / ``user`` / ``AGENT_NOTES.md`` / etc."""
    raw = file.strip().upper()
    if raw.endswith(".MD"):
        raw = raw[:-3]
    if raw in ("AGENT_NOTES", "AGENT", "AGENTNOTES"):
        return "AGENT_NOTES"
    if raw == "USER":
        return "USER"
    raise NotesError(
        f"unknown notes file {file!r}: expected one of {_VALID_KEYS} (or '*.md' variant)"
    )


def _file_and_cap(key: NotesFileKey) -> tuple[Path, int]:
    if key == "AGENT_NOTES":
        return notes_dir() / AGENT_NOTES_FILE, AGENT_NOTES_CAP
    return notes_dir() / USER_NOTES_FILE, USER_NOTES_CAP


def read_notes() -> dict[str, str]:
    """Read both notes files. Missing files are reported as empty strings."""
    result: dict[str, str] = {}
    for key in _VALID_KEYS:
        path, _ = _file_and_cap(key)  # type: ignore[arg-type]
        if path.exists():
            try:
                result[key] = path.read_text(encoding="utf-8")
            except OSError:
                # Defensive: a transient FS error shouldn't break prompt build.
                result[key] = ""
        else:
            result[key] = ""
    return result


def write_notes(file: str, content: str) -> dict[str, object]:
    """Replace a notes file's contents. Enforces the per-file char cap."""
    key = normalize_key(file)
    path, cap = _file_and_cap(key)
    # Strip a trailing run of whitespace then ensure a single trailing newline.
    normalized = content.rstrip() + "\n" if content.strip() else ""
    if len(normalized) > cap:
        raise NotesError(
            f"content for {key} is {len(normalized)} chars, exceeds cap {cap}"
        )
    try:
        path.write_text(normalized, encoding="utf-8")
    except OSError as e:
        raise NotesError(f"failed writing {key}: {e}") from e
    return {"file": key, "path": str(path), "bytes": len(normalized), "cap": cap}


def patch_notes(file: str, find: str, replace: str) -> dict[str, object]:
    """Apply a single find-and-replace. The find string must match exactly once."""
    if not find:
        raise NotesError("find string is empty")
    key = normalize_key(file)
    path, cap = _file_and_cap(key)
    if not path.exists():
        raise NotesError(f"file {key} does not exist yet; use notes_write first")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise NotesError(f"failed reading {key}: {e}") from e
    count = text.count(find)
    if count == 0:
        raise NotesError(f"find string not present in {key}")
    if count > 1:
        raise NotesError(
            f"find string occurs {count} times in {key}; make it unique before patching"
        )
    new_text = text.replace(find, replace, 1)
    if len(new_text) > cap:
        raise NotesError(
            f"patch would grow {key} to {len(new_text)} chars, exceeds cap {cap}"
        )
    try:
        path.write_text(new_text, encoding="utf-8")
    except OSError as e:
        raise NotesError(f"failed writing {key}: {e}") from e
    return {
        "file": key,
        "path": str(path),
        "bytes": len(new_text),
        "cap": cap,
        "replaced": 1,
    }


def render_notes_block(notes: dict[str, str] | None = None) -> str:
    """Render the notes for the system prompt. Returns '' when both files are empty.

    Output is intentionally compact and labeled with the file basename so the
    model can patch by name. The block sits below the goal block and tool hint
    in the system prompt and rarely changes -- good for prompt-cache hit rate.
    """
    if notes is None:
        try:
            notes = read_notes()
        except OSError:
            return ""
    agent = (notes.get("AGENT_NOTES") or "").strip()
    user = (notes.get("USER") or "").strip()
    if not agent and not user:
        return ""
    parts: list[str] = ["\n---\n## Durable notes (always loaded, char-capped)"]
    if agent:
        parts.append(f"\n### AGENT_NOTES.md\n{agent}")
    if user:
        parts.append(f"\n### USER.md\n{user}")
    return "\n".join(parts)
