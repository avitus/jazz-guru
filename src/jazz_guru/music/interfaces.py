"""Typed backend interfaces for the music layer.

Two flavours are exported:

* ``Protocol`` classes (``TranscriptionBackend``, ``ChordAnalysisBackend``,
  ``BeatTrackingBackend``, ``MusicUnderstandingBackend``,
  ``MusicGenerationBackend``) for duck-typed checks. Anything that
  matches one of these shapes can be plugged in without inheriting from
  the matching base.

* :class:`BaseBackend` — concrete adapters inherit from this to get a
  shared ``name``, an ``is_available()`` classmethod that does the lazy
  optional-import probe, and a ``_unavailable()`` helper for raising
  :class:`~jazz_guru.music.errors.BackendUnavailableError` consistently.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from jazz_guru.music.errors import BackendUnavailableError
from jazz_guru.music.models import (
    BeatTrackingResult,
    ChordAnalysisResult,
    MusicAnalysis,
    MusicContext,
    MusicGenerationRequest,
    MusicGenerationResult,
    TranscriptionResult,
)


@runtime_checkable
class TranscriptionBackend(Protocol):
    """Backend that converts an audio recording to a MIDI transcription."""

    name: str

    def transcribe_to_midi(
        self, audio_path: Path, *, instrument: str | None = None
    ) -> TranscriptionResult: ...


@runtime_checkable
class ChordAnalysisBackend(Protocol):
    """Backend that extracts a chord-change sequence from audio."""

    name: str

    def analyze_chords(self, audio_path: Path) -> ChordAnalysisResult: ...


@runtime_checkable
class BeatTrackingBackend(Protocol):
    """Backend that estimates tempo, beat grid, and (optionally) downbeats."""

    name: str

    def track_beats(self, audio_path: Path) -> BeatTrackingResult: ...


@runtime_checkable
class MusicUnderstandingBackend(Protocol):
    """High-level music-understanding model (e.g. Music Flamingo)."""

    name: str

    def analyze_audio(
        self, audio_path: Path, *, context: MusicContext | None = None
    ) -> MusicAnalysis: ...


@runtime_checkable
class MusicGenerationBackend(Protocol):
    """Backend that renders new audio from a prompt + constraints."""

    name: str

    def generate_audio(self, request: MusicGenerationRequest) -> MusicGenerationResult: ...


class BaseBackend:
    """Shared mixin: name + availability probe + uniform error helper.

    Subclasses set the class-level ``name`` and ``install_hint`` and
    override ``_probe`` to attempt the optional import. The default
    ``is_available()`` returns True iff ``_probe()`` does not raise.

    ``name`` is declared as a plain ``str`` (not ``ClassVar``) so the
    class structurally matches the backend Protocols, which expose
    ``name`` as an instance attribute.
    """

    name: str = "base"
    install_hint: str | None = None

    @classmethod
    def _probe(cls) -> None:  # pragma: no cover - trivial default
        """Override to ``import`` the optional dependency; let it raise."""
        return None

    @classmethod
    def is_available(cls) -> bool:
        try:
            cls._probe()
        except Exception:
            return False
        return True

    @classmethod
    def _unavailable(cls, reason: str) -> BackendUnavailableError:
        return BackendUnavailableError(cls.name, reason, cls.install_hint)
