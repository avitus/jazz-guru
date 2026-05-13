"""Backend selection from :mod:`jazz_guru.config` settings.

The four ``MUSIC_*_BACKEND`` env vars (mirrored on
:class:`~jazz_guru.config.Settings`) drive which adapter the
orchestrator picks for each role. ``"none"`` is always a valid value
and disables that role; ``"librosa"`` is the always-available default
for beat / understanding roles.

Selection is **string-driven** rather than class-import-driven so that
unset/missing optional packages never break import. The actual
adapters are imported lazily inside each selector.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from jazz_guru.config import get_settings
from jazz_guru.music.errors import BackendUnavailableError

if TYPE_CHECKING:
    from jazz_guru.music.interfaces import (
        BeatTrackingBackend,
        ChordAnalysisBackend,
        MusicGenerationBackend,
        MusicUnderstandingBackend,
        TranscriptionBackend,
    )


def _normalise(name: str | None) -> str:
    return (name or "none").strip().lower()


# ---- transcription ---------------------------------------------------------


def get_transcription_backend(name: str | None = None) -> TranscriptionBackend | None:
    """Resolve the transcription backend name -> instance, or None if disabled.

    Unknown / unavailable backends raise :class:`BackendUnavailableError`
    so the orchestrator can decide whether to swallow it as a warning.
    """
    chosen = _normalise(name if name is not None else get_settings().music_transcription_backend)
    if chosen in {"none", ""}:
        return None
    if chosen == "basic_pitch":
        from jazz_guru.music.analysis.basic_pitch_backend import BasicPitchBackend

        return BasicPitchBackend()
    if chosen == "mt3":
        from jazz_guru.music.analysis.mt3_backend import MT3Backend

        return MT3Backend()
    raise BackendUnavailableError(
        chosen, "no transcription backend with that name", install_hint=None
    )


# ---- chord analysis --------------------------------------------------------


def get_chord_analysis_backend(name: str | None = None) -> ChordAnalysisBackend | None:
    chosen = _normalise(name if name is not None else get_settings().music_analysis_backend)
    if chosen in {"none", ""}:
        return None
    if chosen == "omnizart":
        from jazz_guru.music.analysis.omnizart_backend import OmnizartBackend

        return OmnizartBackend()
    if chosen == "librosa":
        # Librosa baseline can answer the protocol but only with a "not
        # supported" warning. Returning it keeps the interface honest;
        # the orchestrator surfaces the warning verbatim.
        from jazz_guru.music.analysis.librosa_backend import LibrosaAnalysisBackend

        return LibrosaAnalysisBackend()
    raise BackendUnavailableError(
        chosen, "no chord-analysis backend with that name", install_hint=None
    )


# ---- beat tracking --------------------------------------------------------


def get_beat_tracking_backend(name: str | None = None) -> BeatTrackingBackend | None:
    chosen = _normalise(name if name is not None else get_settings().music_analysis_backend)
    if chosen in {"none", ""}:
        return None
    if chosen == "librosa":
        from jazz_guru.music.analysis.librosa_backend import LibrosaAnalysisBackend

        return LibrosaAnalysisBackend()
    if chosen == "omnizart":
        from jazz_guru.music.analysis.omnizart_backend import OmnizartBackend

        return OmnizartBackend()
    raise BackendUnavailableError(
        chosen, "no beat-tracking backend with that name", install_hint=None
    )


# ---- music understanding ---------------------------------------------------


def get_understanding_backend(name: str | None = None) -> MusicUnderstandingBackend | None:
    chosen = _normalise(name if name is not None else get_settings().music_understanding_backend)
    if chosen in {"none", ""}:
        return None
    if chosen == "librosa":
        from jazz_guru.music.analysis.librosa_backend import LibrosaAnalysisBackend

        return LibrosaAnalysisBackend()
    if chosen == "music_flamingo":
        from jazz_guru.music.analysis.music_flamingo_backend import MusicFlamingoBackend

        return MusicFlamingoBackend()
    raise BackendUnavailableError(
        chosen, "no music-understanding backend with that name", install_hint=None
    )


# ---- generation -----------------------------------------------------------


def get_generation_backend(name: str | None = None) -> MusicGenerationBackend | None:
    chosen = _normalise(name if name is not None else get_settings().music_generation_backend)
    if chosen in {"none", ""}:
        return None
    if chosen == "magenta_rt":
        from jazz_guru.music.generation.magenta_rt_backend import MagentaRealtimeBackend

        return MagentaRealtimeBackend()
    if chosen == "elevenlabs_music":
        from jazz_guru.music.generation.elevenlabs_music_backend import ElevenLabsMusicBackend

        return ElevenLabsMusicBackend()
    raise BackendUnavailableError(
        chosen, "no music-generation backend with that name", install_hint=None
    )


def available_backends() -> dict[str, dict[str, bool | str | None]]:
    """Return a snapshot of every known backend and whether its dep is loadable.

    Useful for diagnostic CLIs (``jazz-guru info``, the analyze-take
    output, etc.) and tests. Does not actually invoke a backend.
    """
    from jazz_guru.music.analysis.basic_pitch_backend import BasicPitchBackend
    from jazz_guru.music.analysis.librosa_backend import LibrosaAnalysisBackend
    from jazz_guru.music.analysis.mt3_backend import MT3Backend
    from jazz_guru.music.analysis.music_flamingo_backend import MusicFlamingoBackend
    from jazz_guru.music.analysis.omnizart_backend import OmnizartBackend
    from jazz_guru.music.generation.elevenlabs_music_backend import ElevenLabsMusicBackend
    from jazz_guru.music.generation.magenta_rt_backend import MagentaRealtimeBackend

    rows: dict[str, dict[str, bool | str | None]] = {}
    for cls in (
        LibrosaAnalysisBackend,
        BasicPitchBackend,
        OmnizartBackend,
        MT3Backend,
        MusicFlamingoBackend,
        MagentaRealtimeBackend,
        ElevenLabsMusicBackend,
    ):
        rows[cls.name] = {
            "available": cls.is_available(),
            "install_hint": cls.install_hint,
        }
    return rows
