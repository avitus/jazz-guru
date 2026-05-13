"""MusicXML inspection helpers used by the music layer.

Thin wrappers over ``music21``. Heavier MusicXML authoring/transposing
stays in :mod:`jazz_guru.actions.tools.music_xml`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def _import_music21() -> Any:
    """Return the music21 module as ``Any``.

    Existing tools in :mod:`jazz_guru.actions.tools.music_xml` use the
    same pattern so mypy doesn't have to model music21's
    ``Score | Part | Opus`` union returned by ``converter.parse``.
    """
    import music21  # type: ignore[import-not-found]

    return music21


def musicxml_summary(path: Path) -> dict[str, Any]:
    """Return a small dict summarising a MusicXML/.mxl/.xml file."""
    m21 = _import_music21()
    score: Any = m21.converter.parse(str(path))
    info: dict[str, Any] = {
        "path": str(path),
        "parts": [pt.partName or pt.id for pt in score.parts],
        "measures": (
            len(list(score.parts[0].getElementsByClass("Measure"))) if len(score.parts) else 0
        ),
    }
    ts: Any = score.recurse().getElementsByClass("TimeSignature").stream()
    if len(ts):
        info["time_signature"] = ts[0].ratioString
    ks: Any = score.recurse().getElementsByClass("KeySignature").stream()
    if len(ks):
        info["key_signature_sharps"] = ks[0].sharps
    tempo: Any = score.recurse().getElementsByClass("MetronomeMark").stream()
    if len(tempo):
        info["tempo_bpm"] = float(tempo[0].number) if tempo[0].number else None
    return info
