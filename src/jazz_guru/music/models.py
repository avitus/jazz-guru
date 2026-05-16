"""Shared data models for the music-backend layer.

These are the typed objects passed between the orchestrator
(:mod:`jazz_guru.music.analyze`) and individual backend adapters. They
deliberately stay backend-agnostic — Basic Pitch, Omnizart, MT3 and the
rest all map their own outputs into these shapes.

Fields are mostly optional because real analysis pipelines degrade
gracefully: a backend that only does tempo should still produce a
:class:`MusicAnalysis` with the other fields left as ``None``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field


class MusicContext(BaseModel):
    """User-supplied context that backends can use to constrain analysis.

    Carries the chart name, target instrument, an optional lead-sheet
    path, and a few coarse hints (expected key/tempo, chord changes).
    Backends are free to ignore any field they cannot use.
    """

    chart: str | None = Field(None, description="Chart/tune name, e.g. 'Autumn Leaves'.")
    instrument: str | None = Field(
        None, description="Performer's instrument, e.g. 'tenor-sax', 'piano'."
    )
    lead_sheet_path: Path | None = Field(None, description="Optional lead-sheet path.")
    expected_key: str | None = Field(None, description="User-declared key, e.g. 'G minor'.")
    expected_tempo_bpm: float | None = Field(None, description="User-declared tempo target.")
    chord_changes: list[str] | None = Field(
        None,
        description="Optional ordered chord-change list, e.g. ['Cm7','F7','BbMaj7'].",
    )


class ChordEvent(BaseModel):
    """One detected chord with a confidence score and time window."""

    start_sec: float
    end_sec: float
    chord: str = Field(..., description="Chord symbol, e.g. 'Cm7', 'F7', 'BbMaj7'.")
    confidence: Annotated[float | None, Field(ge=0.0, le=1.0)] = None


class ChordAnalysisResult(BaseModel):
    backend: str
    chords: list[ChordEvent] = Field(default_factory=list)
    detected_key: str | None = None
    warnings: list[str] = Field(default_factory=list)


class BeatTrackingResult(BaseModel):
    backend: str
    tempo_bpm: float | None = None
    beats_sec: list[float] = Field(default_factory=list)
    downbeats_sec: list[float] = Field(default_factory=list)
    time_signature: str | None = None
    warnings: list[str] = Field(default_factory=list)


class TranscriptionResult(BaseModel):
    """A backend-produced transcription bundle.

    ``midi_path`` is the primary deliverable; ``musicxml_path`` is set
    when a backend can additionally engrave or when a downstream tool
    converts the MIDI.
    """

    backend: str
    midi_path: Path | None = None
    musicxml_path: Path | None = None
    note_count: int | None = None
    # Pydantic v2 ``Annotated`` form keeps the validation constraints while
    # letting plain mypy (no pydantic plugin) read the ``= None`` default.
    confidence: Annotated[float | None, Field(ge=0.0, le=1.0)] = None
    model_name: str | None = None
    warnings: list[str] = Field(default_factory=list)


class MusicAnalysis(BaseModel):
    """High-level music-understanding output, aggregated across backends."""

    backend: str
    detected_key: str | None = None
    tempo_bpm: float | None = None
    time_signature: str | None = None
    chord_changes: list[ChordEvent] = Field(default_factory=list)
    structure: list[str] = Field(
        default_factory=list, description="Coarse sections, e.g. ['intro', 'A', 'A', 'B']."
    )
    summary: str | None = None
    warnings: list[str] = Field(default_factory=list)


class MusicGenerationRequest(BaseModel):
    prompt: str = Field(..., description="Style / mood / instrumentation prompt.")
    duration_sec: float = Field(30.0, gt=0.0)
    target_key: str | None = None
    target_tempo_bpm: float | None = None
    seed: int | None = None
    output_path: Path | None = Field(
        None, description="Destination audio file; backend may choose a default."
    )


class MusicGenerationResult(BaseModel):
    backend: str
    output_path: Path
    duration_sec: float
    model_name: str | None = None
    warnings: list[str] = Field(default_factory=list)


class TimingFeedback(BaseModel):
    """Coarse rhythmic feedback derived from beat alignment.

    ``mean_drift_ms``: average signed offset from the detected beat grid.
    """

    mean_drift_ms: float | None = None
    early_count: int = 0
    late_count: int = 0
    notes: list[str] = Field(default_factory=list)


class PitchFeedback(BaseModel):
    """Coarse pitch / intonation feedback.

    ``cents_drift``: average abs cents deviation from equal temperament.
    """

    detected_key: str | None = None
    out_of_key_count: int = 0
    cents_drift: float | None = None
    notes: list[str] = Field(default_factory=list)


class PracticeFeedback(BaseModel):
    """Composite result returned by :func:`analyze_practice_take`.

    Always safe to return even when most optional backends are absent —
    populated fields reflect what was actually computed. ``warnings``
    enumerates every "backend unavailable" or partial-result event so
    the LLM can mention them to the user.
    """

    audio_path: Path
    context: MusicContext
    transcription: TranscriptionResult | None = None
    analysis: MusicAnalysis | None = None
    chord_analysis: ChordAnalysisResult | None = None
    beat_tracking: BeatTrackingResult | None = None
    timing: TimingFeedback | None = None
    pitch: PitchFeedback | None = None
    summary: str | None = None
    warnings: list[str] = Field(default_factory=list)
