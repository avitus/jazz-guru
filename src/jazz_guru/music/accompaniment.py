"""Symbolic backing-track / accompaniment builder.

Takes a chord progression and produces a MIDI file with two parts —
piano comping voicings and a simple bass line — using ``music21``.
This is the deterministic, no-model path: useful for jazz practice
because it gives a reproducible track in any key/tempo without going
through a generation backend. Pair the resulting MIDI with
:func:`jazz_guru.actions.tools.render.render_midi` to land an audio
backing track.

This is intentionally simple in Phase 3: root-on-beat bass plus a
held chord voicing on the piano. Phase 4 can add walking bass and
swung comping rhythms.
"""
from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CHORD_RE = re.compile(r"^([A-G])(b|♭|♯|#|-)?(.*)$")
_FLAT_MAP = {"b": "-", "♭": "-", "♯": "#"}


def _normalize_chord_type(rest: str) -> str:
    """Map common capitalisation variants to what music21 accepts.

    music21's chord-type abbreviations are case-sensitive: ``maj7``
    works, ``Maj7`` does not. We rewrite the common variants at the
    *start* of the type token only, so alterations like ``b9`` (which
    are lowercase already) pass through untouched.
    """
    rest = rest.replace("Δ", "maj").replace("△", "maj")
    # Maj / MAJ -> maj   (must run before M(\d) so we don't mangle 'Major')
    rest = re.sub(r"^(Maj|MAJ)", "maj", rest)
    # Lone capital M followed by a digit means major chord with that 7th.
    rest = re.sub(r"^M(?=\d)", "maj", rest)
    # Min / MIN -> min   (lower-case `m` is already accepted by music21)
    rest = re.sub(r"^(Min|MIN)", "min", rest)
    return rest


def normalize_chord_symbol(symbol: str) -> str:
    """Massage a chord symbol into the form ``music21`` expects.

    music21's ``ChordSymbol`` parser is picky: flats on the root must
    be ``-`` (``B-maj7``, not ``BbMaj7``), and the chord-type
    abbreviation must be lowercase (``maj7``, not ``Maj7``). Most users
    — and most LLM outputs — write neither way. This helper does both
    fixups in one pass.
    """
    symbol = symbol.strip()
    m = _CHORD_RE.match(symbol)
    if not m:
        return symbol
    root, accidental, rest = m.groups()
    accidental = _FLAT_MAP.get(accidental or "", accidental or "")
    return f"{root}{accidental}{_normalize_chord_type(rest)}"


@dataclass
class BackingTrackResult:
    midi_path: Path
    chord_count: int
    bar_count: int
    tempo_bpm: float
    time_signature: str
    key: str | None
    warnings: list[str]


def _import_music21() -> Any:
    import music21  # type: ignore[import-not-found]

    return music21


def build_backing_track(
    chord_changes: list[str],
    output_path: Path,
    *,
    key: str | None = None,
    tempo_bpm: float = 120.0,
    bars_per_chord: int = 1,
    time_signature: str = "4/4",
    voicing_size: int = 4,
) -> BackingTrackResult:
    """Render a piano + bass backing track for the given chord changes.

    ``chord_changes`` accepts ``music21.harmony.ChordSymbol``-compatible
    strings (``"Cm7"``, ``"G7"``, ``"BbMaj7"``, ``"D7b9"``, ...).
    Unparseable symbols are skipped and reported in the result's
    ``warnings`` list — partial output is better than no output. The
    file is written to ``output_path`` (caller is responsible for
    sandboxing the path).
    """
    if not chord_changes:
        raise ValueError("chord_changes must contain at least one symbol")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    m21 = _import_music21()
    score: Any = m21.stream.Score()
    score.append(m21.tempo.MetronomeMark(number=tempo_bpm))
    score.append(m21.meter.TimeSignature(time_signature))
    if key:
        # music21's Key constructor is picky about "<tonic> <mode>" formatting.
        # If it rejects the label, fall through silently — the resulting score
        # is still useful.
        with contextlib.suppress(Exception):
            score.append(m21.key.Key(key))

    piano_part: Any = m21.stream.Part()
    piano_part.append(m21.instrument.Piano())
    bass_part: Any = m21.stream.Part()
    bass_part.append(m21.instrument.AcousticBass())

    ts_num = int(time_signature.split("/")[0])
    quarter_per_chord = ts_num * bars_per_chord
    warnings: list[str] = []
    parsed_chords = 0

    for raw_symbol in chord_changes:
        symbol = normalize_chord_symbol(raw_symbol)
        try:
            cs = m21.harmony.ChordSymbol(symbol)
        except Exception as exc:
            warnings.append(f"could not parse chord '{raw_symbol}': {exc}")
            # Emit a rest of the same duration so the bar grid stays aligned.
            piano_part.append(m21.note.Rest(quarterLength=quarter_per_chord))
            bass_part.append(m21.note.Rest(quarterLength=quarter_per_chord))
            continue
        parsed_chords += 1

        pitches = list(cs.pitches)[:voicing_size]
        chord = m21.chord.Chord(pitches, quarterLength=quarter_per_chord)
        # Centre the voicing around middle C so it sits in a comping range.
        while chord.bass().octave < 3:
            chord.transpose(12, inPlace=True)
        while chord.bass().octave > 5:
            chord.transpose(-12, inPlace=True)
        piano_part.append(chord)

        # Bass: root on each beat, dropped two octaves from the chord root.
        root_pitch = cs.root()
        bass_pitch = root_pitch.transpose("-P15")
        for _ in range(quarter_per_chord):
            bass_part.append(m21.note.Note(bass_pitch, quarterLength=1))

    score.insert(0, piano_part)
    score.insert(0, bass_part)
    score.write("midi", fp=str(output_path))

    bar_count = (parsed_chords + len(warnings)) * bars_per_chord
    return BackingTrackResult(
        midi_path=output_path,
        chord_count=parsed_chords,
        bar_count=bar_count,
        tempo_bpm=tempo_bpm,
        time_signature=time_signature,
        key=key,
        warnings=warnings,
    )
