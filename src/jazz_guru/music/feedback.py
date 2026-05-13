"""Onset / pitch feedback computed from a transcription + analysis.

Phase 3 adds two heavier passes on top of the Phase-1 tempo summary:

* :func:`compute_timing_feedback` — given a transcribed MIDI and the
  detected beat grid, find the nearest beat for each note's onset and
  summarise how many notes were early / late and the average drift.
* :func:`compute_pitch_feedback` — given a transcribed MIDI and either
  a detected key or a chord-progression, classify each note as
  in-scale / chromatic and emit an out-of-key count.

Both helpers degrade gracefully: missing inputs produce ``None`` so
the orchestrator can leave the slot blank rather than fabricate
numbers.
"""
from __future__ import annotations

import bisect
from pathlib import Path

from jazz_guru.music.accompaniment import normalize_chord_symbol
from jazz_guru.music.models import (
    BeatTrackingResult,
    MusicContext,
    PitchFeedback,
    TimingFeedback,
)


def _note_onsets_seconds(midi_path: Path) -> list[tuple[float, int]]:
    """Return ``(onset_seconds, midi_pitch)`` for every note in the file.

    Uses ``pretty_midi`` (already a hard dependency) which handles tempo
    changes correctly. Empty / unreadable files return ``[]`` so the
    caller can keep going.
    """
    try:
        import pretty_midi  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - pretty_midi is mandatory
        return []
    try:
        pm = pretty_midi.PrettyMIDI(str(midi_path))
    except Exception:
        return []
    out: list[tuple[float, int]] = []
    for instr in pm.instruments:
        for note in instr.notes:
            out.append((float(note.start), int(note.pitch)))
    out.sort(key=lambda t: t[0])
    return out


def compute_timing_feedback(
    midi_path: Path | None,
    beats: BeatTrackingResult | None,
    *,
    context: MusicContext | None = None,
) -> TimingFeedback | None:
    """Per-note onset drift relative to the detected beat grid.

    Returns ``None`` when there isn't enough information to compute it
    (no transcription or fewer than two beats); the orchestrator's
    coarser tempo-delta summary still runs in that case.
    """
    if midi_path is None or beats is None or not beats.beats_sec or len(beats.beats_sec) < 2:
        return None
    onsets = _note_onsets_seconds(Path(midi_path))
    if not onsets:
        return None

    beat_times = sorted(beats.beats_sec)
    drifts_ms: list[float] = []
    early = 0
    late = 0
    for onset, _pitch in onsets:
        idx = bisect.bisect_left(beat_times, onset)
        # Pick the closer of the surrounding beats.
        candidates: list[float] = []
        if idx > 0:
            candidates.append(beat_times[idx - 1])
        if idx < len(beat_times):
            candidates.append(beat_times[idx])
        if not candidates:
            continue
        nearest = min(candidates, key=lambda b: abs(b - onset))
        drift_sec = onset - nearest
        drifts_ms.append(drift_sec * 1000.0)
        if drift_sec < -0.020:
            early += 1
        elif drift_sec > 0.020:
            late += 1

    mean_drift_ms: float | None = None
    if drifts_ms:
        mean_drift_ms = sum(drifts_ms) / len(drifts_ms)

    notes: list[str] = []
    notes.append(f"analysed {len(drifts_ms)} note onsets against {len(beat_times)} beats")
    if mean_drift_ms is not None:
        if abs(mean_drift_ms) < 5:
            notes.append(f"average onset drift {mean_drift_ms:+.1f} ms — tight to the grid")
        elif mean_drift_ms < 0:
            notes.append(
                f"average onset {abs(mean_drift_ms):.0f} ms ahead of the beat — rushing"
            )
        else:
            notes.append(
                f"average onset {mean_drift_ms:.0f} ms behind the beat — laying back"
            )
    if early or late:
        notes.append(f"early notes: {early}; late notes: {late} (>20 ms threshold)")
    if context and context.expected_tempo_bpm and beats.tempo_bpm:
        delta = beats.tempo_bpm - context.expected_tempo_bpm
        if abs(delta) >= 2:
            notes.append(
                f"detected tempo {beats.tempo_bpm:.1f} BPM vs chart "
                f"{context.expected_tempo_bpm:.0f} BPM ({delta:+.1f} BPM)"
            )

    return TimingFeedback(
        mean_drift_ms=mean_drift_ms,
        early_count=early,
        late_count=late,
        notes=notes,
    )


# ----- pitch feedback ------------------------------------------------------


_KEY_TO_SCALE_PCS: dict[str, frozenset[int]] = {
    "major": frozenset({0, 2, 4, 5, 7, 9, 11}),
    "minor": frozenset({0, 2, 3, 5, 7, 8, 10}),
}

_NOTE_PCS = {
    "C": 0, "C#": 1, "Db": 1, "D-": 1,
    "D": 2, "D#": 3, "Eb": 3, "E-": 3,
    "E": 4, "F": 5, "F#": 6, "Gb": 6, "G-": 6,
    "G": 7, "G#": 8, "Ab": 8, "A-": 8,
    "A": 9, "A#": 10, "Bb": 10, "B-": 10,
    "B": 11, "Cb": 11,
}


def _key_to_pc_set(key_label: str) -> frozenset[int] | None:
    """Map a textual key like 'G minor' or 'Bb major' to a 12-pc set."""
    parts = key_label.strip().split()
    if not parts:
        return None
    tonic = parts[0]
    mode = parts[1].lower() if len(parts) > 1 else "major"
    if mode not in _KEY_TO_SCALE_PCS:
        return None
    tonic_pc = _NOTE_PCS.get(tonic) or _NOTE_PCS.get(tonic.capitalize())
    if tonic_pc is None:
        return None
    return frozenset((p + tonic_pc) % 12 for p in _KEY_TO_SCALE_PCS[mode])


def _chord_pitches(chord_symbol: str) -> list[int] | None:
    """Pitch-class list for ``chord_symbol`` via music21."""
    try:
        import music21  # type: ignore[import-not-found]

        cs = music21.harmony.ChordSymbol(normalize_chord_symbol(chord_symbol))
        return [int(p.pitchClass) for p in cs.pitches]
    except Exception:
        return None


def compute_pitch_feedback(
    midi_path: Path | None,
    *,
    detected_key: str | None = None,
    chord_changes: list[str] | None = None,
    context: MusicContext | None = None,
) -> PitchFeedback | None:
    """Tally in-scale vs chromatic notes from a transcription.

    If ``chord_changes`` is supplied we compute the union of chord-tone
    pitch classes; otherwise we use the major/minor scale implied by
    ``detected_key`` (or ``context.expected_key``). Returns ``None``
    when none of those inputs are available.
    """
    if midi_path is None:
        return None
    key_source = detected_key or (context.expected_key if context else None)
    in_set: frozenset[int] | None = None
    if chord_changes:
        pcs: set[int] = set()
        for ch in chord_changes:
            pl = _chord_pitches(ch)
            if pl:
                pcs.update(pl)
        if pcs:
            in_set = frozenset(pcs)
    if in_set is None and key_source:
        in_set = _key_to_pc_set(key_source)
    if in_set is None:
        return None

    onsets = _note_onsets_seconds(Path(midi_path))
    if not onsets:
        return None

    in_count = 0
    out_count = 0
    for _onset, pitch in onsets:
        if (pitch % 12) in in_set:
            in_count += 1
        else:
            out_count += 1

    total = in_count + out_count
    pct_out = (out_count / total * 100.0) if total else 0.0
    notes: list[str] = []
    notes.append(
        f"analysed {total} transcribed notes against "
        f"{'chord changes' if chord_changes else 'detected key'}"
    )
    if out_count:
        notes.append(f"{out_count} note(s) outside the reference set ({pct_out:.1f}%)")
    else:
        notes.append("all transcribed notes fall inside the reference set")

    return PitchFeedback(
        detected_key=detected_key,
        out_of_key_count=out_count,
        notes=notes,
    )
