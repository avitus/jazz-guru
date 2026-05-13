"""Always-available beat-tracking + key-estimation via ``librosa``.

``librosa`` is already a hard dependency (see ``pyproject.toml``), so
this backend never raises :class:`BackendUnavailableError`. It is the
default analysis backend and provides a useful baseline even when no
specialised music model is installed.

Only beat tracking, tempo, and a chroma-template key estimate are
exposed here — anything heavier (full chord ASR, structure parsing,
music-LLM-style summarisation) should go through Omnizart / MT3 /
Music Flamingo backends in the same package.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jazz_guru.music.interfaces import BaseBackend
from jazz_guru.music.models import (
    BeatTrackingResult,
    ChordAnalysisResult,
    MusicAnalysis,
    MusicContext,
)


def _estimate_key(y: Any, sr: int) -> str:
    """Krumhansl-Schmuckler-style chroma-template key estimate."""
    import librosa  # type: ignore[import-untyped]
    import numpy as np

    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    profile = chroma.mean(axis=1)
    pitches = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    major = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    minor = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
    major /= np.linalg.norm(major)
    minor /= np.linalg.norm(minor)
    profile = profile / (np.linalg.norm(profile) + 1e-9)
    best = ("?", -1.0)
    for i in range(12):
        for mode_name, mode_profile in (("major", major), ("minor", minor)):
            score = float(np.dot(profile, np.roll(mode_profile, i)))
            if score > best[1]:
                best = (f"{pitches[i]} {mode_name}", score)
    return best[0]


class LibrosaAnalysisBackend(BaseBackend):
    """Default analysis backend. Provides tempo, beats, and a key guess.

    Implements :class:`~jazz_guru.music.interfaces.BeatTrackingBackend`
    and :class:`~jazz_guru.music.interfaces.MusicUnderstandingBackend`.
    The "music-understanding" output is intentionally modest: it fills
    in tempo + detected key only. Higher-level summaries (sections,
    style, free text) should come from Music Flamingo or similar.
    """

    name: str = "librosa"
    install_hint: str | None = None  # always available

    @classmethod
    def _probe(cls) -> None:
        import librosa  # noqa: F401  (import-only probe)

    def _load(self, audio_path: Path, sr: int = 22050) -> tuple[Any, int]:
        import librosa  # type: ignore[import-untyped]

        y, sr_loaded = librosa.load(str(audio_path), sr=sr, mono=True)
        return y, int(sr_loaded)

    def track_beats(self, audio_path: Path) -> BeatTrackingResult:
        import librosa  # type: ignore[import-untyped]
        import numpy as np

        y, sr = self._load(audio_path)
        tempo, beats = librosa.beat.beat_track(y=y, sr=sr)
        beats_sec = librosa.frames_to_time(beats, sr=sr).tolist()
        return BeatTrackingResult(
            backend=self.name,
            tempo_bpm=float(np.atleast_1d(tempo)[0]) if len(np.atleast_1d(tempo)) else None,
            beats_sec=[float(b) for b in beats_sec],
        )

    def analyze_audio(
        self, audio_path: Path, *, context: MusicContext | None = None
    ) -> MusicAnalysis:
        import librosa  # type: ignore[import-untyped]
        import numpy as np

        y, sr = self._load(audio_path)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        return MusicAnalysis(
            backend=self.name,
            detected_key=_estimate_key(y, sr),
            tempo_bpm=float(np.atleast_1d(tempo)[0]) if len(np.atleast_1d(tempo)) else None,
        )

    def analyze_chords(self, audio_path: Path) -> ChordAnalysisResult:
        """Not supported by the librosa baseline."""
        return ChordAnalysisResult(
            backend=self.name,
            warnings=[
                "librosa baseline does not perform chord ASR; "
                "configure MUSIC_ANALYSIS_BACKEND=omnizart for chord changes."
            ],
        )
