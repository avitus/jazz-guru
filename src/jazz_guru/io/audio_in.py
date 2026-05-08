from __future__ import annotations

from pathlib import Path
from typing import Any


def load_waveform(path: str | Path, sr: int = 22050) -> tuple[Any, int]:
    """Load a WAV/FLAC/MP3 file as a mono numpy waveform via librosa."""
    import librosa  # type: ignore[import-untyped]

    y, sr_actual = librosa.load(str(path), sr=sr, mono=True)
    return y, int(sr_actual)


def basic_features(path: str | Path, sr: int = 22050) -> dict[str, Any]:
    import librosa  # type: ignore[import-untyped]
    import numpy as np

    y, sr_actual = load_waveform(path, sr=sr)
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr_actual)
    return {
        "duration_sec": float(librosa.get_duration(y=y, sr=sr_actual)),
        "tempo_bpm": float(np.atleast_1d(tempo)[0]),
        "rms_mean": float(np.mean(librosa.feature.rms(y=y))),
    }
