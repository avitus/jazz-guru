"""Omnizart backend (placeholder).

Omnizart (https://github.com/Music-and-Culture-Technology-Lab/omnizart)
covers melody, vocal, chord, beat, and drum transcription. Wiring up
its many subcommands is Phase 2 work; for now this module exposes a
fully-typed stub so the rest of the layer can already select it via
``MUSIC_ANALYSIS_BACKEND=omnizart`` without crashing.

When the real implementation lands it should adapt to
:class:`ChordAnalysisBackend` and :class:`BeatTrackingBackend`.
"""
from __future__ import annotations

from pathlib import Path

from jazz_guru.music.interfaces import BaseBackend
from jazz_guru.music.models import BeatTrackingResult, ChordAnalysisResult


class OmnizartBackend(BaseBackend):
    """Stub adapter. Always raises until Phase 2."""

    name: str = "omnizart"
    install_hint: str | None = "pip install omnizart"

    @classmethod
    def _probe(cls) -> None:
        import omnizart  # type: ignore[import-not-found]  # noqa: F401

    def analyze_chords(self, audio_path: Path) -> ChordAnalysisResult:
        raise self._unavailable("Omnizart adapter is a Phase 2 stub")

    def track_beats(self, audio_path: Path) -> BeatTrackingResult:
        raise self._unavailable("Omnizart adapter is a Phase 2 stub")
