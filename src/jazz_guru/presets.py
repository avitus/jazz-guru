"""Instrument-preset data layer.

Owns the on-disk shape of ``data/instruments.yaml`` and the only sanctioned
load/save path. Tools and ``render_midi`` go through here so the file is
never mutated via ``fs_write``/``shell``/``python_exec``.

Cache invalidates automatically when the file mtime changes; tests and
``clear_config_caches`` can force a reload via ``clear_presets_cache``.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from jazz_guru.actions.tools.render import PostProcess
from jazz_guru.config import get_settings

Engine = Literal["fluidsynth", "sfizz", "liquidsfz"]


class Preset(BaseModel):
    engine: Engine
    library: str | None = None
    description: str | None = None
    post: PostProcess | None = None


class PresetsFile(BaseModel):
    version: int = 1
    default: str | None = None
    presets: dict[str, Preset] = Field(default_factory=dict)


_cache: tuple[Path, float, PresetsFile] | None = None


def _path() -> Path:
    return Path(get_settings().jg_instruments_file)


def clear_presets_cache() -> None:
    global _cache
    _cache = None


def load_presets() -> PresetsFile:
    """Read the presets file. (path, mtime)-keyed cache; safe to call hot."""
    global _cache
    p = _path()
    if not p.exists():
        return PresetsFile()
    mtime = p.stat().st_mtime
    if _cache is not None and _cache[0] == p and _cache[1] == mtime:
        return _cache[2]
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    raw.setdefault("presets", {})
    file = PresetsFile.model_validate(raw)
    _cache = (p, mtime, file)
    return file


def save_presets(file: PresetsFile) -> None:
    """Atomic write: tmp file in the same dir, then rename. Invalidates cache."""
    target = _path()
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = yaml.safe_dump(
        file.model_dump(exclude_none=True, mode="json"),
        sort_keys=False,
        allow_unicode=True,
    )
    fd, tmp_path = tempfile.mkstemp(
        prefix=".instruments.", suffix=".yaml.tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(serialized)
        os.replace(tmp_path, target)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise
    clear_presets_cache()


def resolve_library(library: str | None) -> Path | None:
    """Return the on-disk path for a preset library, or None if unset."""
    if not library:
        return None
    p = Path(library).expanduser()
    if p.is_absolute():
        return p
    root = Path(get_settings().jg_instruments_root).expanduser()
    return (root / p).resolve()


class PresetValidationError(ValueError):
    """Raised when a preset fails post-construction validation."""


def validate_preset(name: str, preset: Preset, *, require_library: bool = True) -> None:
    """Check semantic constraints not enforced by the pydantic model.

    ``require_library=True`` (the default) demands that the library file
    actually exists on disk. Pass ``False`` if you want to allow forward
    references — e.g., authoring a preset before installing its library.
    """
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        raise PresetValidationError(
            f"preset name '{name}' must be alphanumeric (with '-' or '_' allowed)"
        )
    if preset.engine == "fluidsynth":
        # fluidsynth tolerates a null library (FLUIDSYNTH_SOUNDFONT fallback).
        if preset.library is None:
            return
    elif preset.library is None:
        raise PresetValidationError(
            f"engine '{preset.engine}' requires a `library` path"
        )
    if not require_library:
        return
    lib = resolve_library(preset.library)
    if lib is None or not lib.exists():
        raise PresetValidationError(
            f"library not found on disk: {lib} (relative paths resolve against "
            f"JG_INSTRUMENTS_ROOT={get_settings().jg_instruments_root})"
        )
