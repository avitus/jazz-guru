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


def data_dir() -> Path:
    return get_settings().jg_data_dir.resolve()


def safe_roots(session_id: str | None = None) -> list[Path]:
    """Roots the agent is allowed to *read* from.

    Order matters only for diagnostics; membership is set-like. Includes:
      * the session workspace (the one place writes are allowed too),
      * ``data/`` (curated project data — presets, sandbox profiles, etc.),
      * ``JG_INSTRUMENTS_ROOT`` (per-machine SFZ/SF2 library tree),
      * any extra absolute paths in ``JG_SAFE_EXTRA_PATHS``.
    """
    s = get_settings()
    roots = [session_workspace(session_id), data_dir()]
    instr = Path(s.jg_instruments_root).expanduser()
    if instr.exists():
        roots.append(instr.resolve())
    for p in s.jg_safe_extra_paths:
        rp = Path(p).expanduser().resolve()
        if rp.exists():
            roots.append(rp)
    return roots


def resolve_in_workspace(path: str | Path, session_id: str | None = None) -> Path:
    """Resolve a user-supplied path inside the session workspace, refusing escapes."""
    base = session_workspace(session_id)
    p = Path(path)
    candidate = p.resolve() if p.is_absolute() else (base / p).resolve()
    base_resolved = base.resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError as e:
        raise PermissionError(f"path {candidate} escapes workspace {base_resolved}") from e
    return candidate


def resolve_in_safe(path: str | Path, session_id: str | None = None) -> Path:
    """Resolve a path that's allowed to live inside *any* :func:`safe_roots`.

    Read-oriented: use this for tools that legitimately need to read
    project data (presets, fixtures) without granting blanket access.
    Writes should still go through :func:`resolve_in_workspace`.

    Relative paths resolve against the session workspace.
    """
    roots = safe_roots(session_id)
    p = Path(path)
    candidate = p.resolve() if p.is_absolute() else (roots[0] / p).resolve()
    for root in roots:
        try:
            candidate.relative_to(root.resolve())
            return candidate
        except ValueError:
            continue
    raise PermissionError(f"path {candidate} is not under any safe root: {[str(r) for r in roots]}")
