"""Modular music-backend layer.

The harness keeps a single LLM as the agent brain. This package adds a
**clean adapter surface** for specialised music tools — transcription
(Basic Pitch, MT3), chord/beat analysis (Omnizart, librosa), music
understanding (Music Flamingo), and music generation (Magenta RT,
ElevenLabs Music) — that the agent can call without any one of them
being mandatory. Optional dependencies are lazy-imported; missing ones
raise :class:`BackendUnavailableError` with a clear install hint, and
the orchestrator turns that into a non-fatal warning.

The public surface is:

* models — :class:`MusicContext`, :class:`PracticeFeedback`, etc.
* interfaces — Protocols + :class:`BaseBackend`.
* registry — :func:`get_*_backend` selectors that read settings.
* analyze — :func:`analyze_practice_take` orchestrator.
"""
from __future__ import annotations

from jazz_guru.music.analyze import analyze_practice_take
from jazz_guru.music.errors import BackendUnavailableError
from jazz_guru.music.interfaces import (
    BaseBackend,
    BeatTrackingBackend,
    ChordAnalysisBackend,
    MusicGenerationBackend,
    MusicUnderstandingBackend,
    TranscriptionBackend,
)
from jazz_guru.music.models import (
    BeatTrackingResult,
    ChordAnalysisResult,
    ChordEvent,
    MusicAnalysis,
    MusicContext,
    MusicGenerationRequest,
    MusicGenerationResult,
    PitchFeedback,
    PracticeFeedback,
    TimingFeedback,
    TranscriptionResult,
)
from jazz_guru.music.registry import (
    available_backends,
    get_beat_tracking_backend,
    get_chord_analysis_backend,
    get_generation_backend,
    get_transcription_backend,
    get_understanding_backend,
)

__all__ = [
    "BackendUnavailableError",
    "BaseBackend",
    "BeatTrackingBackend",
    "BeatTrackingResult",
    "ChordAnalysisBackend",
    "ChordAnalysisResult",
    "ChordEvent",
    "MusicAnalysis",
    "MusicContext",
    "MusicGenerationBackend",
    "MusicGenerationRequest",
    "MusicGenerationResult",
    "MusicUnderstandingBackend",
    "PitchFeedback",
    "PracticeFeedback",
    "TimingFeedback",
    "TranscriptionBackend",
    "TranscriptionResult",
    "analyze_practice_take",
    "available_backends",
    "get_beat_tracking_backend",
    "get_chord_analysis_backend",
    "get_generation_backend",
    "get_transcription_backend",
    "get_understanding_backend",
]
