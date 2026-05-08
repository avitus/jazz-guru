from __future__ import annotations

from pathlib import Path

from jazz_guru.config import get_settings


def workspace_root() -> Path:
    return get_settings().jg_workspace_dir.resolve()


def session_workspace(session_id: str | None = None) -> Path:
    base = workspace_root()
    p = base / "sessions" / session_id if session_id else base / "scratch"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_in_workspace(path: str | Path, session_id: str | None = None) -> Path:
    """Resolve a user-supplied path inside the session workspace, refusing escapes."""
    base = session_workspace(session_id)
    p = Path(path)
    candidate = p.resolve() if p.is_absolute() else (base / p).resolve()
    base_resolved = base.resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError as e:
        raise PermissionError(
            f"path {candidate} escapes workspace {base_resolved}"
        ) from e
    return candidate
