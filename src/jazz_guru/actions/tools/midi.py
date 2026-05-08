from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.registry import registry
from jazz_guru.actions.sandbox import resolve_in_workspace


class MidiInfoInput(BaseModel):
    path: str


class MidiFromNotesInput(BaseModel):
    out_path: str = Field(..., description="Output .mid path in workspace.")
    notes: list[dict[str, Any]] = Field(
        ...,
        description="List of {pitch:int, start_beat:float, duration_beat:float, velocity:int (0-127), channel:int}.",
    )
    bpm: int = 120
    program: int = Field(0, description="General MIDI program (0=Piano).")
    ticks_per_beat: int = 480


@registry.register(
    "midi_info",
    description="Inspect a Standard MIDI File: ticks/beat, tracks, length, tempo events.",
    input_model=MidiInfoInput,
    tags=("music",),
)
async def midi_info(path: str) -> dict[str, Any]:
    import mido  # type: ignore[import-untyped]

    p = resolve_in_workspace(path, current().session_id)
    mid = mido.MidiFile(str(p))
    tempos: list[int] = []
    note_count = 0
    for tr in mid.tracks:
        for msg in tr:
            if msg.type == "set_tempo":
                tempos.append(msg.tempo)
            elif msg.type == "note_on" and msg.velocity > 0:
                note_count += 1
    return {
        "path": str(p),
        "ticks_per_beat": mid.ticks_per_beat,
        "track_count": len(mid.tracks),
        "length_sec": float(mid.length),
        "note_on_count": note_count,
        "tempos_us_per_beat": tempos,
    }


@registry.register(
    "midi_from_notes",
    description="Build a single-track .mid from a list of notes (pitch/start/dur/velocity).",
    input_model=MidiFromNotesInput,
    tags=("music",),
)
async def midi_from_notes(
    out_path: str,
    notes: list[dict[str, Any]],
    bpm: int = 120,
    program: int = 0,
    ticks_per_beat: int = 480,
) -> dict[str, Any]:
    import mido  # type: ignore[import-untyped]

    p = resolve_in_workspace(out_path, current().session_id)
    p.parent.mkdir(parents=True, exist_ok=True)

    mid = mido.MidiFile(ticks_per_beat=ticks_per_beat)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0))
    track.append(mido.Message("program_change", program=program, time=0))

    events: list[tuple[int, mido.Message]] = []
    for n in notes:
        ch = int(n.get("channel", 0))
        pitch = int(n["pitch"])
        vel = int(n.get("velocity", 96))
        start = round(float(n["start_beat"]) * ticks_per_beat)
        dur = round(float(n["duration_beat"]) * ticks_per_beat)
        events.append((start, mido.Message("note_on", note=pitch, velocity=vel, channel=ch, time=0)))
        events.append((start + dur, mido.Message("note_off", note=pitch, velocity=0, channel=ch, time=0)))
    events.sort(key=lambda e: e[0])

    last_tick = 0
    for abs_tick, msg in events:
        delta = abs_tick - last_tick
        msg.time = max(0, delta)
        track.append(msg)
        last_tick = abs_tick
    track.append(mido.MetaMessage("end_of_track", time=0))

    mid.save(str(p))
    return {"path": str(p), "notes": len(notes), "bpm": bpm, "program": program}
