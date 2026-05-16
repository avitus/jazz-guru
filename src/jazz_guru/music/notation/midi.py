"""MIDI inspection helpers used by the music layer.

Thin wrappers over ``mido`` so the orchestrator and backends share a
single summary shape. Heavier MIDI authoring stays in
:mod:`jazz_guru.actions.tools.midi`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def midi_note_count(path: Path) -> int:
    """Count ``note_on`` events with velocity > 0 in a MIDI file."""
    import mido  # type: ignore[import-untyped]

    mid = mido.MidiFile(str(path))
    count = 0
    for track in mid.tracks:
        for msg in track:
            if msg.type == "note_on" and msg.velocity > 0:
                count += 1
    return count


def midi_summary(path: Path) -> dict[str, Any]:
    """Return a small dict summarising a MIDI file's structure."""
    import mido  # type: ignore[import-untyped]

    mid = mido.MidiFile(str(path))
    tempos: list[int] = []
    note_on_count = 0
    for track in mid.tracks:
        for msg in track:
            if msg.type == "set_tempo":
                tempos.append(msg.tempo)
            elif msg.type == "note_on" and msg.velocity > 0:
                note_on_count += 1
    return {
        "path": str(path),
        "ticks_per_beat": mid.ticks_per_beat,
        "track_count": len(mid.tracks),
        "length_sec": float(mid.length),
        "note_on_count": note_on_count,
        "tempos_us_per_beat": tempos,
    }
