"""MusicXML exercise generator.

Three Phase-3 exercise builders, all backed by ``music21``:

* :func:`scale_exercise` — write a one-octave (or multi-octave) scale
  ascending + descending.
* :func:`arpeggio_exercise` — chord-tone arpeggio in 8th notes.
* :func:`ii_v_i_exercise` — generate a 4-bar ii-V-I lead-sheet skeleton
  with chord symbols above empty bars, ready for the practiser to fill
  in lines or for the agent to populate via the LLM.

Output is always MusicXML so the file works with any notation app the
user already has. Inputs use plain-text labels (``"Bb major"``,
``"D7b9"``, etc.) the LLM agent can produce naturally.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from jazz_guru.music.accompaniment import normalize_chord_symbol

_M21_INTERVAL_BY_MODE = {
    "major": ["M2", "M2", "m2", "M2", "M2", "M2", "m2"],
    "minor": ["M2", "m2", "M2", "M2", "m2", "M2", "M2"],
    "dorian": ["M2", "m2", "M2", "M2", "M2", "m2", "M2"],
    "mixolydian": ["M2", "M2", "m2", "M2", "M2", "m2", "M2"],
    "lydian": ["M2", "M2", "M2", "m2", "M2", "M2", "m2"],
    "phrygian": ["m2", "M2", "M2", "M2", "m2", "M2", "M2"],
    "locrian": ["m2", "M2", "M2", "m2", "M2", "M2", "M2"],
}


@dataclass
class ExerciseResult:
    musicxml_path: Path
    title: str
    notes: int


def _import_music21() -> Any:
    import music21  # type: ignore[import-not-found]

    return music21


def _write(score: Any, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = "mxl" if output_path.suffix.lower() == ".mxl" else "musicxml"
    score.write(fmt, fp=str(output_path))


def scale_exercise(
    tonic: str,
    output_path: Path,
    *,
    mode: Literal["major", "minor", "dorian", "mixolydian", "lydian", "phrygian", "locrian"] = "major",
    octaves: int = 1,
    note_value: float = 0.5,
    title: str | None = None,
) -> ExerciseResult:
    """Write an ascending+descending scale exercise.

    ``tonic`` accepts pitch names like ``"C"``, ``"Eb"``, ``"F#"``.
    ``note_value`` is in quarter-note lengths (0.5 = eighth notes).
    """
    m21 = _import_music21()
    if mode not in _M21_INTERVAL_BY_MODE:
        raise ValueError(f"unknown mode '{mode}'")
    intervals = _M21_INTERVAL_BY_MODE[mode]

    start = m21.pitch.Pitch(f"{tonic}4")
    pitches: list[Any] = [start]
    cursor = start
    # Build one octave; replicate for additional octaves.
    for ivl in intervals:
        cursor = cursor.transpose(ivl)
        pitches.append(cursor)
    if octaves > 1:
        higher = list(pitches)
        for _ in range(1, octaves):
            higher = [p.transpose("P8") for p in higher[1:]]
            pitches.extend(higher)

    descending = list(reversed(pitches[:-1]))
    full = [*pitches, *descending]

    part: Any = m21.stream.Part()
    for p in full:
        part.append(m21.note.Note(p, quarterLength=note_value))

    score: Any = m21.stream.Score()
    title = title or f"{tonic} {mode} scale"
    score.metadata = m21.metadata.Metadata()
    score.metadata.title = title
    score.append(part)
    _write(score, output_path)
    return ExerciseResult(musicxml_path=output_path, title=title, notes=len(full))


def arpeggio_exercise(
    chord_symbol: str,
    output_path: Path,
    *,
    octaves: int = 2,
    note_value: float = 0.5,
    title: str | None = None,
) -> ExerciseResult:
    """Write an ascending+descending arpeggio for ``chord_symbol``.

    Chord parsing goes through ``music21.harmony.ChordSymbol`` so any
    of ``"Cmaj7"``, ``"G7b9"``, ``"Dm7b5"`` etc. work.
    """
    m21 = _import_music21()
    cs = m21.harmony.ChordSymbol(normalize_chord_symbol(chord_symbol))
    if not cs.pitches:
        raise ValueError(f"chord '{chord_symbol}' produced no pitches")

    base = list(cs.pitches)
    pitches: list[Any] = list(base)
    for octave in range(1, octaves):
        for p in base:
            pitches.append(p.transpose(f"P{8 * octave}"))
    descending = list(reversed(pitches[:-1]))
    full = [*pitches, *descending]

    part: Any = m21.stream.Part()
    for p in full:
        part.append(m21.note.Note(p, quarterLength=note_value))

    score: Any = m21.stream.Score()
    title = title or f"{chord_symbol} arpeggio"
    score.metadata = m21.metadata.Metadata()
    score.metadata.title = title
    score.append(part)
    _write(score, output_path)
    return ExerciseResult(musicxml_path=output_path, title=title, notes=len(full))


def ii_v_i_exercise(
    target_tonic: str,
    output_path: Path,
    *,
    mode: Literal["major", "minor"] = "major",
    bars_per_chord: int = 1,
    title: str | None = None,
) -> ExerciseResult:
    """Write a ii-V-I lead-sheet skeleton in ``target_tonic``.

    The result is a 4-bar score with chord symbols above empty bars
    (``ii / V / I / I``), so the practiser (or the agent) can fill in
    melodic content. Useful as a generator for the LLM's
    "give-me-a-lick" flow.
    """
    m21 = _import_music21()
    tonic = m21.pitch.Pitch(target_tonic)
    ii_pitch = tonic.transpose("M2")
    v_pitch = tonic.transpose("P5")
    if mode == "major":
        ii_sym = f"{ii_pitch.name}m7"
        v_sym = f"{v_pitch.name}7"
        i_sym = f"{tonic.name}Maj7"
    else:
        ii_sym = f"{ii_pitch.name}m7b5"
        v_sym = f"{v_pitch.name}7"
        i_sym = f"{tonic.name}m7"

    chord_seq = [ii_sym, v_sym, i_sym, i_sym]

    score: Any = m21.stream.Score()
    title = title or f"ii-V-I in {target_tonic} {mode}"
    score.metadata = m21.metadata.Metadata()
    score.metadata.title = title
    part: Any = m21.stream.Part()
    part.append(m21.meter.TimeSignature("4/4"))

    notes = 0
    for sym in chord_seq:
        # Emit `bars_per_chord` separate 4/4 measures per chord so each bar
        # is its own properly-sized measure (rather than one oversized rest).
        # The chord symbol attaches to the first measure only.
        for bar_idx in range(bars_per_chord):
            measure: Any = m21.stream.Measure()
            if bar_idx == 0:
                with contextlib.suppress(Exception):
                    measure.insert(0, m21.harmony.ChordSymbol(normalize_chord_symbol(sym)))
            measure.append(m21.note.Rest(quarterLength=4))
            part.append(measure)
            notes += 1

    score.append(part)
    _write(score, output_path)
    return ExerciseResult(musicxml_path=output_path, title=title, notes=notes)
