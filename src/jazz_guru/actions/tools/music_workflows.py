"""Phase-3 agent tools: backing tracks, exercises, music generation.

Three tools register here:

* ``build_backing_track`` — symbolic accompaniment from a chord
  progression (no model). Writes a MIDI inside the session workspace
  that ``render_midi`` can pick up.
* ``generate_exercise`` — MusicXML scale / arpeggio / ii-V-I exercise.
* ``generate_music`` — delegate to the configured
  :class:`MusicGenerationBackend` (Magenta RT or ElevenLabs Music).
  Returns the path of the generated audio.

All paths are resolved inside the per-session workspace so the
sandbox stays honest.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.registry import registry
from jazz_guru.actions.sandbox import resolve_in_workspace
from jazz_guru.music.accompaniment import build_backing_track
from jazz_guru.music.errors import BackendUnavailableError
from jazz_guru.music.exercises import arpeggio_exercise, ii_v_i_exercise, scale_exercise
from jazz_guru.music.models import MusicGenerationRequest
from jazz_guru.music.registry import get_generation_backend

# ---------------------------------------------------------------------------
# build_backing_track
# ---------------------------------------------------------------------------


class BuildBackingTrackInput(BaseModel):
    out_path: str = Field(..., description="Output .mid path inside the session workspace.")
    chord_changes: list[str] = Field(
        ...,
        description=(
            "Ordered chord-symbol list, e.g. ['Cm7','F7','BbMaj7','EbMaj7']. "
            "Flats can be written as 'b', '♭', or '-'; major-7 as 'Maj7', 'maj7', "
            "or '△'."
        ),
    )
    key: str | None = Field(
        None, description="Optional key for the score, e.g. 'Bb major'."
    )
    tempo_bpm: float = Field(120.0, gt=0)
    bars_per_chord: int = Field(1, ge=1, le=8)
    time_signature: str = Field("4/4")


@registry.register(
    "build_backing_track",
    description=(
        "Render a symbolic piano + bass backing track from a chord progression "
        "and write a MIDI file into the session workspace. No external model "
        "calls; great for jazz practice because it's reproducible. Pair with "
        "render_midi to land an audio file."
    ),
    input_model=BuildBackingTrackInput,
    tags=("music", "accompaniment"),
)
async def build_backing_track_tool(
    out_path: str,
    chord_changes: list[str],
    key: str | None = None,
    tempo_bpm: float = 120.0,
    bars_per_chord: int = 1,
    time_signature: str = "4/4",
) -> dict[str, Any]:
    sid = current().session_id
    resolved = resolve_in_workspace(out_path, sid)
    # music21's MIDI writer is sync; punt to a thread.
    result = await asyncio.to_thread(
        build_backing_track,
        chord_changes,
        resolved,
        key=key,
        tempo_bpm=tempo_bpm,
        bars_per_chord=bars_per_chord,
        time_signature=time_signature,
    )
    return {
        "midi_path": str(result.midi_path),
        "chord_count": result.chord_count,
        "bar_count": result.bar_count,
        "tempo_bpm": result.tempo_bpm,
        "time_signature": result.time_signature,
        "key": result.key,
        "warnings": result.warnings,
    }


# ---------------------------------------------------------------------------
# generate_exercise
# ---------------------------------------------------------------------------


class GenerateExerciseInput(BaseModel):
    kind: Literal["scale", "arpeggio", "ii_v_i"]
    out_path: str = Field(..., description="Output .musicxml/.mxl path in the workspace.")
    tonic: str | None = Field(
        None, description="Tonic note (required for scale/ii_v_i), e.g. 'Bb', 'F#'."
    )
    chord_symbol: str | None = Field(
        None, description="Chord symbol (required for arpeggio), e.g. 'Cmaj7'."
    )
    mode: str = Field(
        "major",
        description=(
            "Mode for scale exercises (major/minor/dorian/mixolydian/lydian/"
            "phrygian/locrian) or ii_v_i flavour (major/minor)."
        ),
    )
    octaves: int = Field(1, ge=1, le=4)
    note_value: float = Field(0.5, gt=0.0)
    bars_per_chord: int = Field(1, ge=1, le=8)


@registry.register(
    "generate_exercise",
    description=(
        "Generate a MusicXML practice exercise: scale, arpeggio, or ii-V-I "
        "lead-sheet skeleton. Output is .musicxml/.mxl so any notation app "
        "(or the music_xml_* tools) can open it."
    ),
    input_model=GenerateExerciseInput,
    tags=("music", "exercise"),
)
async def generate_exercise_tool(
    kind: str,
    out_path: str,
    tonic: str | None = None,
    chord_symbol: str | None = None,
    mode: str = "major",
    octaves: int = 1,
    note_value: float = 0.5,
    bars_per_chord: int = 1,
) -> dict[str, Any]:
    sid = current().session_id
    resolved = resolve_in_workspace(out_path, sid)

    def _run() -> dict[str, Any]:
        if kind == "scale":
            if not tonic:
                return {"error": "scale exercise requires a 'tonic'"}
            r = scale_exercise(
                tonic, resolved, mode=mode, octaves=octaves, note_value=note_value  # type: ignore[arg-type]
            )
        elif kind == "arpeggio":
            if not chord_symbol:
                return {"error": "arpeggio exercise requires a 'chord_symbol'"}
            r = arpeggio_exercise(chord_symbol, resolved, octaves=octaves, note_value=note_value)
        elif kind == "ii_v_i":
            if not tonic:
                return {"error": "ii_v_i exercise requires a 'tonic'"}
            r = ii_v_i_exercise(
                tonic, resolved, mode=mode, bars_per_chord=bars_per_chord  # type: ignore[arg-type]
            )
        else:
            return {"error": f"unknown exercise kind '{kind}'"}
        return {
            "musicxml_path": str(r.musicxml_path),
            "title": r.title,
            "notes": r.notes,
        }

    return await asyncio.to_thread(_run)


# ---------------------------------------------------------------------------
# generate_music
# ---------------------------------------------------------------------------


class GenerateMusicInput(BaseModel):
    prompt: str = Field(..., description="Style / mood / instrumentation prompt.")
    out_path: str | None = Field(
        None,
        description=(
            "Optional output path in the session workspace. The backend chooses "
            "a default if absent."
        ),
    )
    duration_sec: float = Field(30.0, gt=0)
    target_key: str | None = None
    target_tempo_bpm: float | None = None
    seed: int | None = None
    backend: str | None = Field(
        None,
        description=(
            "Override MUSIC_GENERATION_BACKEND for this call "
            "('magenta_rt' | 'elevenlabs_music')."
        ),
    )


@registry.register(
    "generate_music",
    description=(
        "Generate audio via the configured MusicGenerationBackend (Magenta RT or "
        "ElevenLabs Music). Returns the path of the produced audio plus any "
        "warnings from the backend. Backends that aren't installed/keyed surface "
        "their install hint."
    ),
    input_model=GenerateMusicInput,
    tags=("music", "generation"),
)
async def generate_music_tool(
    prompt: str,
    out_path: str | None = None,
    duration_sec: float = 30.0,
    target_key: str | None = None,
    target_tempo_bpm: float | None = None,
    seed: int | None = None,
    backend: str | None = None,
) -> dict[str, Any]:
    sid = current().session_id
    resolved: Path | None = None
    if out_path:
        resolved = resolve_in_workspace(out_path, sid)
    try:
        gen = get_generation_backend(backend)
    except BackendUnavailableError as exc:
        return {"error": str(exc)}
    if gen is None:
        return {
            "error": (
                "MUSIC_GENERATION_BACKEND is 'none'; set it (or pass 'backend') "
                "to 'magenta_rt' or 'elevenlabs_music' to enable generation."
            )
        }

    request = MusicGenerationRequest(
        prompt=prompt,
        duration_sec=duration_sec,
        target_key=target_key,
        target_tempo_bpm=target_tempo_bpm,
        seed=seed,
        output_path=resolved,
    )

    try:
        result = await asyncio.to_thread(gen.generate_audio, request)
    except BackendUnavailableError as exc:
        return {"error": str(exc)}
    return {
        "backend": result.backend,
        "output_path": str(result.output_path),
        "duration_sec": result.duration_sec,
        "model_name": result.model_name,
        "warnings": result.warnings,
    }
