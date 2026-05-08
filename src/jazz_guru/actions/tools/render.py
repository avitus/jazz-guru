from __future__ import annotations

import asyncio
import shutil

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.registry import registry
from jazz_guru.actions.sandbox import resolve_in_workspace
from jazz_guru.config import get_settings


class RenderMidiInput(BaseModel):
    midi_path: str
    out_path: str = Field(..., description="Output .wav (or .flac via ffmpeg) path.")
    sample_rate: int = 44100
    soundfont: str | None = Field(None, description="Override the FLUIDSYNTH_SOUNDFONT env var.")


@registry.register(
    "render_midi",
    description="Render a .mid file to .wav using FluidSynth (and ffmpeg for .flac).",
    input_model=RenderMidiInput,
    tags=("music", "audio"),
)
async def render_midi(
    midi_path: str,
    out_path: str,
    sample_rate: int = 44100,
    soundfont: str | None = None,
) -> dict[str, object]:
    fluid = shutil.which("fluidsynth")
    if not fluid:
        return {"error": "fluidsynth binary not found on PATH"}
    sf = soundfont or get_settings().fluidsynth_soundfont
    if not sf:
        return {"error": "no soundfont configured (set FLUIDSYNTH_SOUNDFONT)"}

    sid = current().session_id
    midi = resolve_in_workspace(midi_path, sid)
    out = resolve_in_workspace(out_path, sid)
    out.parent.mkdir(parents=True, exist_ok=True)

    if out.suffix.lower() == ".wav":
        wav_target = out
        post_convert = False
    else:
        wav_target = out.with_suffix(".wav")
        post_convert = True

    cmd = [
        fluid, "-ni", "-F", str(wav_target),
        "-r", str(sample_rate), "-T", "wav", sf, str(midi),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, err_bytes = await proc.communicate()
    if proc.returncode != 0:
        return {
            "error": "fluidsynth failed",
            "exit_code": proc.returncode,
            "stderr": err_bytes.decode("utf-8", errors="replace"),
        }

    if post_convert:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return {"error": "ffmpeg not found for non-wav output", "wav_path": str(wav_target)}
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-i", str(wav_target), str(out),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        wav_target.unlink(missing_ok=True)

    return {"path": str(out), "sample_rate": sample_rate, "soundfont": sf}
