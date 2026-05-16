"""High-level practice-take orchestrator.

``analyze_practice_take`` calls every configured backend in turn,
collects partial results, and assembles a :class:`PracticeFeedback`
bundle for the LLM. The pipeline degrades gracefully:

* Any backend that is set to ``"none"`` (or to a value whose optional
  dependency is missing) is skipped, and the reason lands in
  ``warnings``.
* A backend that raises :class:`BackendUnavailableError` is treated as
  a soft failure: caught, logged into ``warnings``, and the orchestrator
  moves on. Any other unexpected exception is also caught and turned
  into a warning so that a misbehaving optional model can never crash
  the agent loop.
* When a lead sheet is supplied, its chord changes feed
  :func:`_timing_feedback` so the orchestrator can produce coarse pitch
  / timing notes even with the libros baseline alone.

The orchestrator is async because the agent loop calls tools as
coroutines; CPU-bound backend calls are pushed to threads via
``asyncio.to_thread`` so they don't block the event loop.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from jazz_guru.music.errors import BackendUnavailableError
from jazz_guru.music.feedback import compute_pitch_feedback, compute_timing_feedback
from jazz_guru.music.models import (
    BeatTrackingResult,
    ChordAnalysisResult,
    MusicAnalysis,
    MusicContext,
    PitchFeedback,
    PracticeFeedback,
    TimingFeedback,
    TranscriptionResult,
)
from jazz_guru.music.notation.leadsheet import LeadSheet, load_leadsheet
from jazz_guru.music.registry import (
    get_beat_tracking_backend,
    get_chord_analysis_backend,
    get_transcription_backend,
    get_understanding_backend,
)

log = logging.getLogger(__name__)


async def _run[T](
    label: str,
    fn: Callable[[], T],
    warnings: list[str],
) -> T | None:
    """Invoke ``fn`` in a thread; capture BackendUnavailable/other failures.

    All Phase-1 backends are synchronous, so ``fn`` is unconditionally
    pushed to :func:`asyncio.to_thread` to keep the event loop
    responsive. If a future backend returns an awaitable, the thread
    will produce that awaitable and we await it here. Errors are pushed
    onto ``warnings`` with the backend label so the orchestrator can
    surface them in the final :class:`PracticeFeedback`.
    """
    try:
        result = await asyncio.to_thread(fn)
        if asyncio.iscoroutine(result):
            result = await result
        return result
    except BackendUnavailableError as exc:
        warnings.append(f"{label}: {exc}")
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("music backend %s raised", label)
        warnings.append(f"{label}: unexpected error: {exc}")
    return None


def _timing_feedback(
    beats: BeatTrackingResult | None,
    context: MusicContext,
) -> TimingFeedback | None:
    """Produce a coarse timing summary from the beat grid.

    We don't have onset-level pitch tracking in Phase 1, so the "drift"
    metric here is intentionally simple: it summarises how stable the
    detected beat spacing is relative to the user-declared tempo (if
    any), and emits qualitative notes.
    """
    if beats is None:
        return None
    notes: list[str] = []
    mean_drift_ms: float | None = None

    if beats.tempo_bpm is None:
        notes.append("no tempo detected; cannot summarise timing")
        return TimingFeedback(notes=notes)

    if context.expected_tempo_bpm:
        delta_bpm = beats.tempo_bpm - context.expected_tempo_bpm
        # convert to per-beat ms drift at the *expected* tempo
        expected_period_ms = 60_000.0 / context.expected_tempo_bpm
        actual_period_ms = 60_000.0 / max(1e-6, beats.tempo_bpm)
        mean_drift_ms = actual_period_ms - expected_period_ms
        if delta_bpm > 2:
            notes.append(
                f"average tempo {beats.tempo_bpm:.1f} BPM is {abs(delta_bpm):.1f} BPM faster than"
                f" the chart's {context.expected_tempo_bpm:.0f} BPM."
            )
        elif delta_bpm < -2:
            notes.append(
                f"average tempo {beats.tempo_bpm:.1f} BPM is {abs(delta_bpm):.1f} BPM slower than"
                f" the chart's {context.expected_tempo_bpm:.0f} BPM."
            )
        else:
            notes.append(
                f"tempo {beats.tempo_bpm:.1f} BPM is within 2 BPM of the chart target."
            )
    else:
        notes.append(f"detected tempo: {beats.tempo_bpm:.1f} BPM")

    return TimingFeedback(mean_drift_ms=mean_drift_ms, notes=notes)


def _pitch_feedback(
    analysis: MusicAnalysis | None,
    leadsheet: LeadSheet | None,
    context: MusicContext,
) -> PitchFeedback | None:
    """Produce a coarse pitch summary from the detected key and a chart."""
    if analysis is None and leadsheet is None and context.expected_key is None:
        return None
    notes: list[str] = []
    detected = analysis.detected_key if analysis else None
    expected = context.expected_key or (leadsheet.key if leadsheet else None)

    if detected:
        notes.append(f"detected key: {detected}")
    if expected and detected:
        # Normalize "C major" / "c MAJOR" / "C  major" to compare on pitch+mode
        # equality. Substring matching falsely flagged e.g. "C" inside "C# minor".
        expected_norm = " ".join(expected.strip().lower().split()[:2])
        detected_norm = " ".join(detected.strip().lower().split()[:2])
        if expected_norm != detected_norm:
            notes.append(
                f"detected key '{detected}' does not match the chart's expected key '{expected}'."
            )
        else:
            notes.append(f"detected key matches the chart's '{expected}'.")

    return PitchFeedback(detected_key=detected, notes=notes)


def _summarise(feedback: PracticeFeedback) -> str:
    """Stitch a one-paragraph summary the LLM can paste verbatim."""
    parts: list[str] = []
    if feedback.context.chart:
        parts.append(f"Chart: {feedback.context.chart}.")
    if feedback.context.instrument:
        parts.append(f"Instrument: {feedback.context.instrument}.")
    if feedback.beat_tracking and feedback.beat_tracking.tempo_bpm:
        parts.append(f"Detected tempo {feedback.beat_tracking.tempo_bpm:.1f} BPM.")
    if feedback.analysis and feedback.analysis.detected_key:
        parts.append(f"Detected key {feedback.analysis.detected_key}.")
    if feedback.transcription and feedback.transcription.midi_path:
        parts.append(f"Transcription MIDI: {feedback.transcription.midi_path}.")
    if feedback.timing and feedback.timing.notes:
        parts.append(" ".join(feedback.timing.notes))
    if feedback.pitch and feedback.pitch.notes:
        parts.append(" ".join(feedback.pitch.notes))
    if feedback.warnings:
        parts.append(f"({len(feedback.warnings)} backend warning(s); see `warnings`.)")
    return " ".join(parts).strip() or "No analysis results available."


async def analyze_practice_take(
    audio_path: Path,
    *,
    chart: str | None = None,
    instrument: str | None = None,
    lead_sheet_path: Path | None = None,
    expected_key: str | None = None,
    expected_tempo_bpm: float | None = None,
    chord_changes: list[str] | None = None,
    transcription_backend: str | None = None,
    analysis_backend: str | None = None,
    understanding_backend: str | None = None,
) -> PracticeFeedback:
    """Orchestrate available music backends against one practice take.

    All keyword args are optional; defaults come from the
    :class:`~jazz_guru.config.Settings` fields named
    ``music_*_backend``. The function never raises — every backend
    error becomes a ``warnings`` entry instead. This is intentional so
    that the LLM agent can rely on the tool always returning a
    :class:`PracticeFeedback` and explain the missing pieces to the
    user verbally.
    """
    audio_path = Path(audio_path)
    context = MusicContext(
        chart=chart,
        instrument=instrument,
        lead_sheet_path=Path(lead_sheet_path) if lead_sheet_path else None,
        expected_key=expected_key,
        expected_tempo_bpm=expected_tempo_bpm,
        chord_changes=chord_changes,
    )
    warnings: list[str] = []

    if not audio_path.exists():
        warnings.append(f"audio file not found: {audio_path}")
        return PracticeFeedback(
            audio_path=audio_path,
            context=context,
            warnings=warnings,
            summary=f"audio file not found: {audio_path}",
        )

    # ---- resolve backends; missing config = warning, never an exception
    transcription = _safe_select(
        "transcription",
        lambda: get_transcription_backend(transcription_backend),
        warnings,
    )
    chord_be = _safe_select(
        "chord analysis",
        lambda: get_chord_analysis_backend(analysis_backend),
        warnings,
    )
    beats_be = _safe_select(
        "beat tracking",
        lambda: get_beat_tracking_backend(analysis_backend),
        warnings,
    )
    understanding_be = _safe_select(
        "music understanding",
        lambda: get_understanding_backend(understanding_backend),
        warnings,
    )

    # ---- run them, collecting partial results
    transcription_result: TranscriptionResult | None = None
    if transcription is not None:
        transcription_result = await _run(
            f"transcription[{transcription.name}]",
            lambda: transcription.transcribe_to_midi(audio_path, instrument=instrument),
            warnings,
        )

    chord_result: ChordAnalysisResult | None = None
    if chord_be is not None:
        chord_result = await _run(
            f"chord[{chord_be.name}]",
            lambda: chord_be.analyze_chords(audio_path),
            warnings,
        )
        if chord_result and chord_result.warnings:
            warnings.extend(f"chord[{chord_be.name}]: {w}" for w in chord_result.warnings)

    beats_result: BeatTrackingResult | None = None
    if beats_be is not None:
        beats_result = await _run(
            f"beats[{beats_be.name}]",
            lambda: beats_be.track_beats(audio_path),
            warnings,
        )

    analysis_result: MusicAnalysis | None = None
    if understanding_be is not None:
        analysis_result = await _run(
            f"understanding[{understanding_be.name}]",
            lambda: understanding_be.analyze_audio(audio_path, context=context),
            warnings,
        )

    # The librosa baseline's analyze_audio is the easiest way to land a
    # detected_key when no specialised understanding backend is set. We
    # only run it as a fallback so a configured backend is never
    # silently shadowed.
    if analysis_result is None and beats_be is not None and hasattr(beats_be, "analyze_audio"):
        analysis_result = await _run(
            f"understanding[{beats_be.name} fallback]",
            lambda: beats_be.analyze_audio(audio_path, context=context),  # type: ignore[union-attr]
            warnings,
        )

    leadsheet: LeadSheet | None = None
    if context.lead_sheet_path is not None:
        try:
            leadsheet = await asyncio.to_thread(load_leadsheet, context.lead_sheet_path)
        except Exception as exc:  # pragma: no cover - load_leadsheet swallows most errors
            warnings.append(f"lead sheet: {exc}")
    if chord_changes and leadsheet is None:
        leadsheet = LeadSheet(chord_changes=list(chord_changes))

    # Coarse timing/pitch summaries (always run, even without a transcription).
    coarse_timing = _timing_feedback(beats_result, context)
    coarse_pitch = _pitch_feedback(analysis_result, leadsheet, context)

    # Deeper, per-note feedback when a transcription MIDI is available.
    transcription_midi = (
        transcription_result.midi_path if transcription_result else None
    )
    deep_timing: TimingFeedback | None = None
    deep_pitch: PitchFeedback | None = None
    if transcription_midi is not None:
        deep_timing = compute_timing_feedback(
            transcription_midi, beats_result, context=context
        )
        # Prefer chord changes from the user > the loaded leadsheet > detected key.
        chord_changes_for_pitch: list[str] | None = chord_changes
        if (
            chord_changes_for_pitch is None
            and leadsheet is not None
            and leadsheet.chord_changes
        ):
            chord_changes_for_pitch = list(leadsheet.chord_changes)
        detected_key = analysis_result.detected_key if analysis_result else None
        deep_pitch = compute_pitch_feedback(
            transcription_midi,
            detected_key=detected_key,
            chord_changes=chord_changes_for_pitch,
            context=context,
        )

    feedback = PracticeFeedback(
        audio_path=audio_path,
        context=context,
        transcription=transcription_result,
        analysis=analysis_result,
        chord_analysis=chord_result,
        beat_tracking=beats_result,
        timing=deep_timing or coarse_timing,
        pitch=deep_pitch or coarse_pitch,
        warnings=warnings,
    )
    feedback.summary = _summarise(feedback)
    return feedback


def _safe_select[T](label: str, fn: Callable[[], T | None], warnings: list[str]) -> T | None:
    """Resolve a backend slot; downgrade BackendUnavailable to a warning."""
    try:
        return fn()
    except BackendUnavailableError as exc:
        warnings.append(f"{label}: {exc}")
        return None
