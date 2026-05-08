from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf


def write_wav(path: str | Path, y: np.ndarray, sr: int = 44100, subtype: str = "PCM_16") -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(p), y, sr, subtype=subtype)
    return str(p)


def write_flac(path: str | Path, y: np.ndarray, sr: int = 44100) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(p), y, sr, format="FLAC")
    return str(p)
