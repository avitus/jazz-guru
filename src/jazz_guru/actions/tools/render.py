"""Offline MIDI → audio rendering.

Default engine is FluidSynth (SF2/SF3, General MIDI). Optional engines:
  * ``sfizz``     — SFZ via the ``sfizz_render`` CLI.
  * ``liquidsfz`` — SFZ via ``liquidsfz --no-jack --render``.

A presets registry (``data/instruments.yaml``) maps a preset name to an
engine + library path + post-processing defaults. The agent mutates this
file via the ``preset_*`` tool family; this renderer reloads it on every
call (mtime-cached in :mod:`jazz_guru.presets`).

Post-processing (lowpass, vibrato, gain, loudnorm) is applied via ffmpeg as
a single filter chain. Any ``post_process`` field passed by the caller
overrides the preset default; any field set to ``null``/``0`` disables that
filter for the call.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.registry import registry
from jazz_guru.actions.sandbox import resolve_in_workspace
from jazz_guru.config import get_settings


class PostProcess(BaseModel):
    lowpass_hz: float | None = Field(None, description="Second-order lowpass cutoff (Hz). None disables.")
    lowpass_q: float = Field(0.7, description="Q for the lowpass (default 0.7).")
    vibrato_hz: float | None = Field(None, description="Pitch vibrato rate (Hz). None disables.")
    vibrato_depth: float = Field(0.05, description="Vibrato depth, 0.0-1.0. ~0.05 is subtle.")
    gain_db: float = Field(0.0, description="Output gain in dB.")
    normalize: bool = Field(False, description="Run ffmpeg loudnorm.")


class RenderMidiInput(BaseModel):
    midi_path: str
    out_path: str = Field(..., description="Output .wav (or .flac/.mp3 via ffmpeg) path.")
    sample_rate: int = 44100
    instrument: str | None = Field(
        None,
        description=(
            "Preset name from data/instruments.yaml. If set, selects the engine "
            "and library; overrides `engine` and `soundfont`. Use `preset_list` "
            "to enumerate available presets."
        ),
    )
    engine: str | None = Field(
        None,
        description="'fluidsynth' | 'sfizz' | 'liquidsfz'. Default: fluidsynth.",
    )
    soundfont: str | None = Field(
        None,
        description="Library path override. SF2/SF3 for fluidsynth, SFZ for sfizz/liquidsfz.",
    )
    post_process: PostProcess | None = Field(
        None,
        description=(
            "Post-processing filter chain applied via ffmpeg. Values here override "
            "the preset defaults; pass an empty object to use the preset as-is."
        ),
    )


# ---------- preset loading -------------------------------------------------
#
# These are kept as thin wrappers over jazz_guru.presets so existing call
# sites and tests that monkeypatch them keep working. New code should call
# the presets module directly.


def _load_presets() -> dict[str, Any]:
    # Local import: presets imports from this module (PostProcess), so we
    # can't do it at module top level.
    from jazz_guru.presets import load_presets

    return load_presets().model_dump(exclude_none=True, mode="json")


def _resolve_library(library: str | None) -> Path | None:
    from jazz_guru.presets import resolve_library

    return resolve_library(library)


# ---------- engines --------------------------------------------------------


async def _render_fluidsynth(
    *, midi: Path, wav: Path, sample_rate: int, library: Path | None
) -> tuple[int, str]:
    fluid = shutil.which("fluidsynth")
    if not fluid:
        return 127, "fluidsynth binary not found on PATH"
    sf: Path | None = library
    if sf is None:
        env_sf = get_settings().fluidsynth_soundfont
        sf = Path(env_sf) if env_sf else None
    if sf is None:
        return 2, "no soundfont configured (set FLUIDSYNTH_SOUNDFONT or pass `soundfont`)"
    if not sf.exists():
        return 2, f"soundfont not found: {sf}"
    cmd = [
        fluid, "-ni", "-F", str(wav),
        "-r", str(sample_rate), "-T", "wav", str(sf), str(midi),
    ]
    return await _run(cmd)


async def _render_sfizz(
    *, midi: Path, wav: Path, sample_rate: int, library: Path | None
) -> tuple[int, str]:
    binary = shutil.which("sfizz_render")
    if not binary:
        return 127, (
            "sfizz_render not found. Install sfizz with the offline-render CLI "
            "(build with -DSFIZZ_RENDER=ON or use a distro package that ships it)."
        )
    if not library:
        return 2, "sfizz engine requires an SFZ library (set `instrument` or `soundfont`)"
    if not library.exists():
        return 2, f"SFZ library not found: {library}"
    cmd = [
        binary,
        "--sfz", str(library),
        "--midi", str(midi),
        "--wav", str(wav),
        "--samplerate", str(sample_rate),
    ]
    return await _run(cmd)


async def _render_liquidsfz(
    *, midi: Path, wav: Path, sample_rate: int, library: Path | None
) -> tuple[int, str]:
    binary = shutil.which("liquidsfz")
    if not binary:
        return 127, "liquidsfz not found. `brew install liquidsfz` (macOS) or your package manager."
    if not library:
        return 2, "liquidsfz engine requires an SFZ library"
    if not library.exists():
        return 2, f"SFZ library not found: {library}"
    cmd = [
        binary, "--no-jack", "--sample-rate", str(sample_rate),
        "--export", str(wav), "--midi", str(midi), str(library),
    ]
    return await _run(cmd)


_ENGINES = {
    "fluidsynth": _render_fluidsynth,
    "sfizz": _render_sfizz,
    "liquidsfz": _render_liquidsfz,
}


async def _run(cmd: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    return proc.returncode or 0, err.decode("utf-8", errors="replace")


# ---------- post-processing ------------------------------------------------


def _build_filter_chain(pp: PostProcess) -> list[str]:
    """Return the ffmpeg -af clauses for `pp`. Empty if no filters apply."""
    parts: list[str] = []
    if pp.lowpass_hz and pp.lowpass_hz > 0:
        parts.append(f"lowpass=f={pp.lowpass_hz}:t=q:w={pp.lowpass_q}")
    if pp.vibrato_hz and pp.vibrato_hz > 0:
        depth = max(0.0, min(1.0, pp.vibrato_depth))
        parts.append(f"vibrato=f={pp.vibrato_hz}:d={depth}")
    if pp.gain_db and abs(pp.gain_db) > 1e-6:
        parts.append(f"volume={pp.gain_db}dB")
    if pp.normalize:
        parts.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    return parts


def _merge_post(preset_post: dict[str, Any] | None, override: PostProcess | None) -> PostProcess:
    base = PostProcess(**(preset_post or {}))
    if override is None:
        return base
    # Caller-supplied values overwrite preset defaults field-by-field.
    return base.model_copy(update=override.model_dump(exclude_unset=True))


async def _ffmpeg_postprocess(
    *, src_wav: Path, dst: Path, pp: PostProcess
) -> tuple[int, str]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return 127, "ffmpeg not found"
    cmd = [ffmpeg, "-y", "-i", str(src_wav)]
    chain = _build_filter_chain(pp)
    if chain:
        cmd.extend(["-af", ",".join(chain)])
    cmd.append(str(dst))
    return await _run(cmd)


# ---------- the tool -------------------------------------------------------


@registry.register(
    "render_midi",
    description=(
        "Render a .mid file to audio. Engines: fluidsynth (SF2/SF3, default), "
        "sfizz, liquidsfz (both SFZ). Use `instrument` to pick a preset "
        "(see `preset_list`), or pass `engine` + `soundfont` directly. "
        "`post_process` adds an ffmpeg filter chain (lowpass / vibrato / gain / "
        "loudnorm) for warmth."
    ),
    input_model=RenderMidiInput,
    tags=("music", "audio"),
)
async def render_midi(
    midi_path: str,
    out_path: str,
    sample_rate: int = 44100,
    instrument: str | None = None,
    engine: str | None = None,
    soundfont: str | None = None,
    post_process: dict[str, Any] | PostProcess | None = None,
) -> dict[str, Any]:
    sid = current().session_id
    midi = resolve_in_workspace(midi_path, sid)
    out = resolve_in_workspace(out_path, sid)
    out.parent.mkdir(parents=True, exist_ok=True)

    presets = _load_presets()
    preset: dict[str, Any] = {}
    preset_name = instrument or (presets.get("default") if engine is None else None)
    if preset_name:
        preset = presets.get("presets", {}).get(preset_name) or {}
        if not preset:
            return {"error": f"unknown preset '{preset_name}'", "available": sorted((presets.get('presets') or {}).keys())}

    chosen_engine = engine or preset.get("engine") or "fluidsynth"
    if chosen_engine not in _ENGINES:
        return {"error": f"unknown engine '{chosen_engine}'", "available": sorted(_ENGINES.keys())}

    library = _resolve_library(soundfont or preset.get("library"))

    pp_input = post_process if isinstance(post_process, PostProcess) else (
        PostProcess(**(post_process or {})) if post_process else None
    )
    pp = _merge_post(preset.get("post"), pp_input)

    # Render to a temp WAV next to the target. We always go through ffmpeg
    # for the final hop when there's a filter chain or non-wav target.
    wav_chain = _build_filter_chain(pp)
    needs_ffmpeg = bool(wav_chain) or out.suffix.lower() != ".wav"
    raw_wav = out.with_suffix(".raw.wav") if needs_ffmpeg else out

    rc, err = await _ENGINES[chosen_engine](
        midi=midi, wav=raw_wav, sample_rate=sample_rate, library=library
    )
    if rc != 0:
        if raw_wav != out:
            raw_wav.unlink(missing_ok=True)
        return {"error": f"{chosen_engine} failed", "exit_code": rc, "stderr": err}

    if needs_ffmpeg:
        rc, err = await _ffmpeg_postprocess(src_wav=raw_wav, dst=out, pp=pp)
        raw_wav.unlink(missing_ok=True)
        if rc != 0:
            return {"error": "ffmpeg failed", "exit_code": rc, "stderr": err}

    return {
        "path": str(out),
        "sample_rate": sample_rate,
        "engine": chosen_engine,
        "library": str(library) if library else None,
        "preset": preset_name,
        "post_process": pp.model_dump(exclude_none=True),
        "filters_applied": wav_chain,
    }
