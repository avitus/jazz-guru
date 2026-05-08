from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.registry import registry
from jazz_guru.actions.sandbox import resolve_in_workspace


class AudioAnalyzeInput(BaseModel):
    path: str = Field(..., description="WAV/FLAC/MP3 path.")
    features: list[str] = Field(
        default_factory=lambda: ["duration", "tempo", "key", "rms", "spectral_centroid"],
        description="Subset of: duration, tempo, key, rms, spectral_centroid, zcr, mfcc.",
    )
    sr: int = Field(22050, description="Resample rate.")


def _estimate_key(y: Any, sr: int) -> str:
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


@registry.register(
    "audio_analyze",
    description="Analyze an audio file with librosa: duration, tempo, key, rms, spectral_centroid, zcr, mfcc.",
    input_model=AudioAnalyzeInput,
    tags=("audio", "perception"),
)
async def audio_analyze(
    path: str,
    features: list[str] | None = None,
    sr: int = 22050,
) -> dict[str, Any]:
    import librosa  # type: ignore[import-untyped]
    import numpy as np

    p = resolve_in_workspace(path, current().session_id)
    feats = set(features or ["duration", "tempo", "key", "rms", "spectral_centroid"])
    y, sr_loaded = librosa.load(str(p), sr=sr, mono=True)
    sr = int(sr_loaded)

    out: dict[str, Any] = {"path": str(p), "sample_rate": int(sr)}
    if "duration" in feats:
        out["duration_sec"] = float(librosa.get_duration(y=y, sr=sr))
    if "tempo" in feats:
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        out["tempo_bpm"] = float(np.atleast_1d(tempo)[0])
    if "rms" in feats:
        out["rms_mean"] = float(np.mean(librosa.feature.rms(y=y)))
    if "spectral_centroid" in feats:
        out["spectral_centroid_mean"] = float(np.mean(librosa.feature.spectral_centroid(y=y, sr=sr)))
    if "zcr" in feats:
        out["zcr_mean"] = float(np.mean(librosa.feature.zero_crossing_rate(y=y)))
    if "mfcc" in feats:
        mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
        out["mfcc_means"] = [float(x) for x in mfcc.mean(axis=1).tolist()]
    if "key" in feats:
        out["key_estimate"] = _estimate_key(y, sr)
    return out
