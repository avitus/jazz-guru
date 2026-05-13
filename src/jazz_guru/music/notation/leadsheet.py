"""Minimal lead-sheet parsing.

A real chart parser belongs in Phase 2 (likely a wrapper around
``music21.romanText`` or a custom iReal-style parser). This module gives
the orchestrator a uniform ``LeadSheet`` shape it can populate from
several formats — for now, MusicXML (via ``music21``) and a trivial
plain-text "Cm7 | F7 | BbMaj7 |" form.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class LeadSheet(BaseModel):
    """Minimal lead-sheet representation: title + chord-change sequence."""

    title: str | None = None
    key: str | None = None
    time_signature: str | None = None
    chord_changes: list[str] = Field(default_factory=list)
    source_path: Path | None = None


def _parse_chord_text(text: str) -> list[str]:
    """Split a 'Cm7 | F7 | BbMaj7' style chart into a flat chord list."""
    cleaned = text.replace("\n", " ").replace("\t", " ")
    parts = [p.strip() for p in cleaned.replace("||", "|").split("|")]
    chords: list[str] = []
    for chunk in parts:
        if not chunk:
            continue
        for tok in chunk.split():
            t = tok.strip(",;:")
            if t:
                chords.append(t)
    return chords


def load_leadsheet(path: Path) -> LeadSheet:
    """Load a lead sheet from disk; defers MusicXML/iReal parsing to Phase 2.

    Recognised formats:

    * ``.txt`` / ``.chords`` — pipe-separated chord chart.
    * ``.xml`` / ``.musicxml`` / ``.mxl`` — best-effort via ``music21``
      (only chord-symbol extraction; falls back to title-only).

    Always returns a :class:`LeadSheet` (possibly empty) so the
    orchestrator can keep going without branching on file type.
    """
    p = Path(path)
    if not p.exists():
        return LeadSheet(source_path=p)

    suffix = p.suffix.lower()
    if suffix in {".txt", ".chords", ".md"}:
        text = p.read_text(encoding="utf-8")
        return LeadSheet(
            title=p.stem,
            chord_changes=_parse_chord_text(text),
            source_path=p,
        )

    if suffix in {".xml", ".musicxml", ".mxl"}:
        try:
            from jazz_guru.music.notation.musicxml import _import_music21

            m21 = _import_music21()
            score: Any = m21.converter.parse(str(p))
            chords: list[str] = []
            for ch in score.recurse().getElementsByClass("ChordSymbol"):
                figure = getattr(ch, "figure", None)
                if figure:
                    chords.append(str(figure))
            title = None
            if getattr(score, "metadata", None) is not None:
                title = score.metadata.title
            ts_stream: Any = score.recurse().getElementsByClass("TimeSignature").stream()
            ks_stream: Any = score.recurse().getElementsByClass("KeySignature").stream()
            return LeadSheet(
                title=title or p.stem,
                key=(str(ks_stream[0]) if len(ks_stream) else None),
                time_signature=(ts_stream[0].ratioString if len(ts_stream) else None),
                chord_changes=chords,
                source_path=p,
            )
        except Exception:  # pragma: no cover - music21 corner cases
            return LeadSheet(title=p.stem, source_path=p)

    return LeadSheet(title=p.stem, source_path=p)
