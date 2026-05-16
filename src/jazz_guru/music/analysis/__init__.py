"""Audio-analysis backends (transcription, chord, beat, music understanding).

Each module exposes one adapter class. They are imported lazily by
:mod:`jazz_guru.music.registry` so that a missing optional dependency
never breaks the package import.
"""
from __future__ import annotations

__all__: list[str] = []
