"""Typed read/write surface for instrument presets.

These are the only sanctioned mutators of ``data/instruments.yaml``. The
agent should *not* reach for ``fs_write`` / ``shell`` / ``python_exec`` to
touch the file directly — those bypass validation and atomic-write
semantics.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from jazz_guru.actions.registry import registry
from jazz_guru.actions.tools.render import PostProcess
from jazz_guru.presets import (
    Engine,
    Preset,
    PresetValidationError,
    load_presets,
    save_presets,
    validate_preset,
)


class _Empty(BaseModel):
    pass


class PresetGetInput(BaseModel):
    name: str = Field(..., description="Preset name.")


class PresetUpsertInput(BaseModel):
    name: str = Field(..., description="Preset name (alphanumeric, '-' and '_' allowed).")
    engine: Engine = Field(..., description="Render engine.")
    library: str | None = Field(
        None,
        description=(
            "Library path. Absolute, or relative to $JG_INSTRUMENTS_ROOT. "
            "Required for sfizz/liquidsfz; optional for fluidsynth (falls "
            "back to $FLUIDSYNTH_SOUNDFONT)."
        ),
    )
    description: str | None = Field(None, description="Human-readable label.")
    post: PostProcess | None = Field(
        None, description="Default post-processing applied unless render_midi overrides."
    )
    set_default: bool = Field(
        False,
        description=(
            "If true, also mark this preset as the file's `default` (used by "
            "render_midi when neither `instrument` nor `engine` is supplied)."
        ),
    )
    require_library_exists: bool = Field(
        True,
        description=(
            "When true (default), the library path must already exist on disk. "
            "Set false to author a preset before installing its library."
        ),
    )


class PresetDeleteInput(BaseModel):
    name: str = Field(..., description="Preset name to remove.")


@registry.register(
    "preset_list",
    description=(
        "List every render_midi preset (name, engine, description) and the "
        "current default. Read-only."
    ),
    input_model=_Empty,
    tags=("music", "presets"),
)
async def preset_list() -> dict[str, Any]:
    f = load_presets()
    return {
        "default": f.default,
        "presets": [
            {"name": name, "engine": p.engine, "description": p.description}
            for name, p in sorted(f.presets.items())
        ],
    }


@registry.register(
    "preset_get",
    description="Return the full record for one preset (engine, library, description, post).",
    input_model=PresetGetInput,
    tags=("music", "presets"),
)
async def preset_get(name: str) -> dict[str, Any]:
    f = load_presets()
    p = f.presets.get(name)
    if p is None:
        return {"error": f"unknown preset '{name}'", "available": sorted(f.presets.keys())}
    return {"name": name, **p.model_dump(exclude_none=True, mode="json")}


@registry.register(
    "preset_upsert",
    description=(
        "Create or update a render_midi preset. Validates engine and library, "
        "writes data/instruments.yaml atomically. Use this — not fs_write — to "
        "edit instrument presets."
    ),
    input_model=PresetUpsertInput,
    tags=("music", "presets"),
)
async def preset_upsert(
    name: str,
    engine: str,
    library: str | None = None,
    description: str | None = None,
    post: dict[str, Any] | PostProcess | None = None,
    set_default: bool = False,
    require_library_exists: bool = True,
) -> dict[str, Any]:
    if isinstance(post, dict):
        post_model: PostProcess | None = PostProcess.model_validate(post)
    else:
        post_model = post
    try:
        preset = Preset(
            engine=engine,  # type: ignore[arg-type]
            library=library,
            description=description,
            post=post_model,
        )
    except ValueError as e:
        return {"error": f"invalid preset: {e}"}
    try:
        validate_preset(name, preset, require_library=require_library_exists)
    except PresetValidationError as e:
        return {"error": str(e)}
    f = load_presets()
    existed = name in f.presets
    f.presets[name] = preset
    if set_default or f.default is None:
        f.default = name
    save_presets(f)
    return {
        "name": name,
        "status": "updated" if existed else "created",
        "default": f.default,
    }


@registry.register(
    "preset_delete",
    description=(
        "Delete a preset by name. Refuses if it's currently the file default; "
        "set a new default via preset_upsert(..., set_default=True) first."
    ),
    input_model=PresetDeleteInput,
    tags=("music", "presets"),
)
async def preset_delete(name: str) -> dict[str, Any]:
    f = load_presets()
    if name not in f.presets:
        return {"error": f"unknown preset '{name}'", "available": sorted(f.presets.keys())}
    if f.default == name:
        return {
            "error": f"cannot delete '{name}' while it is the default; set another default first",
            "default": f.default,
        }
    del f.presets[name]
    save_presets(f)
    return {"name": name, "status": "removed"}
